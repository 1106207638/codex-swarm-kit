from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from threading import Event
import unittest
from unittest.mock import patch

import call_log
from routing import run
from task_state import Store
from test_router_policy import CATALOG, task


ROOT = Path(__file__).resolve().parent
PRIVATE = 'fixture private data\n下一行'


class CallLogTests(unittest.TestCase):
    def setUp(self):
        scratch = self.enterContext(tempfile.TemporaryDirectory())
        self.root = Path(scratch) / 'ledger'
        self.log = self.root / call_log.FILE
        self.state_file = self.root / 'state.json'
        self.base = {'state_dir': str(self.root), 'owner': 'journal-owner', 'epoch': 1}
        self.item = task(task_id=PRIVATE, goal=PRIVATE, input=PRIVATE,
                         workspace=scratch, ownership=[str(Path(scratch) / 'input.txt')])
        self.cli('init')

    def cli(self, op, exit_code=0, scoped=True, **payload):
        request = {**(self.base if scoped else {}), 'op': op, **payload}
        process = subprocess.run([sys.executable, '-B', '-X', 'utf8', str(ROOT / 'routing.py')],
                                 input=json.dumps(request), text=True, encoding='utf-8',
                                 capture_output=True, timeout=15)
        self.assertEqual(process.returncode, exit_code, process.stdout + process.stderr)
        self.assertEqual(process.stderr, '')
        return json.loads(process.stdout)

    def prepare(self, exit_code=0, op='prepare'):
        return self.cli(op, exit_code, task_id=PRIVATE, catalog=CATALOG,
                        catalog_provenance={'source': 'runtime-tool', 'captured_at': 'fixture',
                                            'entrypoint': 'synthetic only'}, profiles_dir=str(ROOT / 'agents'))

    def query(self, **filters):
        return self.cli('call_log', **filters)['result']

    def test_task_lifecycle_route_decisions_and_rejection_with_privacy(self):
        self.cli('add', task=self.item)
        prepared = self.prepare()['result']
        before = self.state_file.read_bytes()
        rejected = self.prepare(2)
        denied = self.cli('update', 2, task=self.item, epoch=5)
        self.assertEqual(before, self.state_file.read_bytes())
        receipt = {'attempt_id': prepared['attempt_id'], 'agent_id': PRIVATE}
        self.cli('bind', **receipt)
        self.cli('report', **receipt, input_digest=prepared['input_digest'], summary=PRIVATE,
                 evidence=[PRIVATE], host_stopped_evidence=PRIVATE,
                 host_reported_model=prepared['decision']['model'], host_reported_effort=prepared['decision']['effort'])
        self.cli('verify', attempt_id=prepared['attempt_id'], input_digest=prepared['input_digest'],
                 evidence=[PRIVATE], verifier_context_id='journal-owner')
        data = self.log.read_text(encoding='utf-8')
        for value in (PRIVATE.splitlines()[0], 'journal-owner', str(self.root), prepared['attempt_id']):
            self.assertNotIn(value, data)
        before = self.state_file.read_bytes()
        result = self.query(task_id=PRIVATE)
        self.assertEqual(result['skipped_lines'], 0)
        finish = [row for row in result['records'] if row['phase'] == 'finish']
        self.assertEqual([row['op'] for row in finish], ['add', 'prepare', 'prepare', 'update', 'bind', 'report', 'verify'])
        self.assertEqual((finish[1]['action'], finish[1]['result_status']), ('delegate', 'dispatch_pending'))
        self.assertEqual(finish[1]['reason_codes'], ['planned_execution'])
        self.assertEqual(finish[2]['call_id'], rejected['call_log']['id'])
        self.assertEqual((finish[2]['outcome'], finish[2]['exit_code']), ('rejected', 2))
        self.assertEqual(finish[3]['call_id'], denied['call_log']['id'])
        self.assertEqual(finish[3]['error_code'], 'coordinator_mismatch')
        self.assertEqual(finish[5]['reported_model'], finish[1]['requested_model'])
        self.assertEqual(finish[-1]['result_status'], 'verified')
        self.assertEqual(before, self.state_file.read_bytes())
        attempt = self.query(attempt_id=prepared['attempt_id'])['records']
        self.assertEqual([row['phase'] for row in attempt[:2]], ['start', 'finish'])
        self.assertEqual(attempt[0]['call_id'], finish[1]['call_id'])
        self.assertEqual(attempt[-1], finish[-1])
        self.assertEqual(len(self.query(call_id=rejected['call_log']['id'])['records']), 2)

    def test_blocked_decision_preview_and_legacy_status(self):
        before = self.state_file.read_bytes()
        self.assertEqual(self.query()['records'], [])
        self.cli('route', scoped=False, task=self.item, catalog=CATALOG)
        self.assertFalse(self.log.exists())
        self.cli('route', 2, task=self.item, catalog=CATALOG, owner='unrelated')
        self.assertFalse(self.log.exists())
        blocked_task = {**self.item, 'approval': {'required': True, 'granted': False}}
        self.cli('route', task=blocked_task, catalog=CATALOG)
        self.cli('main_recommend', task=blocked_task, catalog=CATALOG, substantial=True)
        rows = self.query()['records']
        self.assertEqual([row['reason_codes'] for row in rows if row['phase'] == 'finish'],
                         [['blocked_approval'], ['approval_required']])
        self.assertEqual(rows[-1]['outcome'], 'blocked')
        self.assertEqual(self.cli('status')['result']['schema_version'], 1)
        self.assertEqual(before, self.state_file.read_bytes())

    def test_concurrent_cli_writers_and_pagination(self):
        with ThreadPoolExecutor(max_workers=4) as callers:
            calls = list(callers.map(lambda _: self.cli('route', task=self.item, catalog=CATALOG), range(8)))
        first = self.query(limit=7)
        second = self.query(after_line=first['next_after_line'])
        rows = first['records'] + second['records']
        self.assertEqual(len(rows), 16)
        self.assertEqual(second['skipped_lines'], 0)
        for response in calls:
            identity = response['call_log']['id']
            self.assertEqual([r['phase'] for r in rows if r['call_id'] == identity], ['start', 'finish'])

    def test_concurrent_association_does_not_skip_pages(self):
        self.cli('add', task=self.item)
        snapshot_taken = Event()
        release_reader = Event()
        writer_trying = Event()
        write_finished = Event()
        status, append = Store.status, call_log.append

        def pause_reader(store, owner, epoch):
            snapshot = status(store, owner, epoch)
            snapshot_taken.set()
            self.assertTrue(release_reader.wait(5))
            return snapshot

        def observe_writer(root, row):
            writer_trying.set()
            append(root, row)
            write_finished.set()

        def writer():
            pending, code = run({**self.base, 'op': 'prepare', 'task_id': PRIVATE, 'catalog': CATALOG,
                'catalog_provenance': {'source':'runtime-tool', 'captured_at':'fixture','entrypoint':'test'},
                'profiles_dir': str(ROOT/'agents')})
            self.assertEqual(code, 0, pending)
            bound, code = run({**self.base, 'op': 'bind', 'attempt_id': pending['result']['attempt_id'],
                               'agent_id':'synthetic host'})
            self.assertEqual(code, 0, bound)

        with patch.object(Store,'status',pause_reader), patch.object(call_log,'append',observe_writer):
            with ThreadPoolExecutor(max_workers=2) as workers:
                reader = workers.submit(run, {**self.base, 'op':'call_log', 'task_id':PRIVATE})
                try:
                    self.assertTrue(snapshot_taken.wait(5))
                    writing = workers.submit(writer)
                    self.assertTrue(writer_trying.wait(5))
                    self.assertFalse(write_finished.wait(0.25))
                finally:
                    release_reader.set()
                first, code = reader.result(timeout=10)
                self.assertEqual(code, 0, first)
                writing.result(timeout=10)
        second = self.query(task_id=PRIVATE, after_line=first['result']['next_after_line'])
        self.assertEqual([(r['op'],r['phase']) for r in second['records']],
                         [('prepare','start'),('prepare','finish'),('bind','start'),('bind','finish')])

    def test_interruption_and_malformed_line(self):
        self.cli('add', task=self.item)
        invocation = call_log.Call(self.root, {**self.base, 'op': 'prepare', 'task_id': PRIVATE})
        invocation.write('start')
        with self.log.open('ab') as stream:
            stream.write(b'{' + b'x' * (call_log.MAX_LINE+2))
        self.prepare()
        rows = self.query()
        self.assertEqual(rows['skipped_lines'], 1)
        self.assertEqual(rows['records'][-1]['result_status'], 'dispatch_pending')
        self.assertEqual([r['phase'] for r in self.query(call_id=invocation.call_id)['records']], ['start'])

    def test_query_excludes_unexpected_fields(self):
        self.cli('add', task=self.item)
        row = self.query()['records'][0]
        with self.log.open('ab') as stream:
            stream.write((json.dumps(dict(row, goal=PRIVATE)) + '\n').encode())
        result = self.query()
        self.assertEqual(result['skipped_lines'], 1)
        self.assertEqual(len(result['records']), 2)

    def test_accepted_operation_with_spaces_is_logged(self):
        self.cli('add',task=self.item)
        prepared = self.prepare(op=' prepare ')['result']
        rows = self.query(attempt_id=prepared['attempt_id'])['records']
        self.assertEqual([(row['op'],row['phase']) for row in rows],[('prepare','start'),('prepare','finish')])

    def test_end_without_newline_stays_available_on_next_page(self):
        self.cli('add',task=self.item)
        marker = call_log.Call(self.root, {**self.base, 'op': 'prepare', 'task_id': PRIVATE})
        marker.write('start')
        with self.log.open('r+b') as stream:
            stream.seek(-1,os.SEEK_END)
            self.assertEqual(stream.read(),b'\n')
            stream.truncate(stream.tell()-1)
        first = self.query()
        self.assertEqual((first['next_after_line'],first['skipped_lines']),(2,1))
        self.prepare()
        second = self.query(after_line=first['next_after_line'])
        self.assertEqual(second['records'][0]['call_id'],marker.call_id)

    def test_start_write_failure_cannot_change_ledger(self):
        before = self.state_file.read_bytes()
        self.log.mkdir()
        response = self.cli('add', 2, task=self.item)
        self.assertEqual(response['call_log']['status'], 'start_write_failed')
        self.assertEqual(before, self.state_file.read_bytes())

    def test_hard_link_to_another_file_is_rejected(self):
        original = self.root.parent / 'other-file.txt'
        original.write_bytes(b'unrelated data')
        try:
            os.link(original,self.log)
        except (NotImplementedError, OSError) as exc:
            self.skipTest(f'hard links unavailable: {type(exc).__name__}')
        before = self.state_file.read_bytes()
        response = self.cli('add', 2, task=self.item)
        self.assertEqual(response['call_log']['status'], 'start_write_failed')
        self.assertEqual(before,self.state_file.read_bytes())
        self.assertEqual(original.read_bytes(),b'unrelated data')
        self.cli('call_log',2)

    def test_finish_write_failure_preserves_committed_receipt(self):
        self.cli('add',task=self.item)
        append = call_log.append

        def fail_finish(root, row):
            if row['phase'] == 'finish':
                raise OSError('test interruption after commit')
            append(root, row)

        with patch.object(call_log, 'append', side_effect=fail_finish):
            response, exit_code = run({**self.base, 'op': 'prepare', 'task_id': PRIVATE, 'catalog': CATALOG,
                'catalog_provenance': {'source':'runtime-tool', 'captured_at':'fixture','entrypoint':'test'},
                'profiles_dir': str(ROOT/'agents')})
        self.assertEqual(exit_code, 0)
        self.assertTrue(response['ok'])
        self.assertEqual(response['call_log']['status'], 'finish_write_failed')
        identity = response['result']['attempt_id']
        self.assertIn(identity,self.cli('status')['result']['attempts'])
        self.assertEqual([r['phase'] for r in self.query(call_id=response['call_log']['id'])['records']], ['start'])
        self.assertEqual([r['phase'] for r in self.query(attempt_id=identity)['records']], ['start'])
        self.cli('bind',attempt_id=identity,agent_id='synthetic host')
        self.assertEqual([r['phase'] for r in self.query(attempt_id=identity)['records']], ['start','start','finish'])


if __name__ == '__main__':
    unittest.main()
