import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from packet_transport import read_packet
from task_state import Store, StateError
from test_task_state import make_task, prepare
from test_check_protocol import spec, result


class PacketTransportTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)/"中文 space ' quote";self.root.mkdir()
        self.store=Store(self.root/'ledger');self.owner='root';self.store.init(self.owner)

    def start(self, checks=False):
        self.task=make_task(self.root,stage='review')
        self.task['packet_transport']='file'
        self.task['input']='第一行\n第二行，literal \\n，路径 C:\\sample\\file.csv，"引号"'
        if checks:self.task['check_plan']=[spec()]
        self.store.apply('add',self.owner,1,task=self.task)
        self.p=prepare(self.store,self.owner,1)
        self.a=self.store.status(self.owner,1)['attempts'][self.p['attempt_id']]
        self.t=self.a['packet']['transport']
        self.store.apply('bind',self.owner,1,attempt_id=self.p['attempt_id'],agent_id='reviewer')

    def report(self, digest=None, **kwargs):
        return self.store.apply('report',self.owner,1,attempt_id=self.p['attempt_id'],agent_id='reviewer',
            input_digest=self.p['input_digest'],summary='done',evidence=['real evidence required outside fixtures'],
            host_stopped_evidence='synthetic stop',packet_sha256=digest,**kwargs)

    def verify(self):
        return self.store.apply('verify',self.owner,1,attempt_id=self.p['attempt_id'],
             input_digest=self.p['input_digest'],verifier_context_id='root',evidence=['synthetic check'])

    def test_full_packet_roundtrip_preserves_special_characters_and_role(self):
        self.start()
        loaded=read_packet(self.t['path'],self.t['sha256'],self.p['attempt_id'])
        self.assertEqual(loaded['body']['input'],self.task['input'])
        self.assertEqual(loaded['body']['ownership'],self.task['ownership'])
        self.assertTrue(loaded['body']['read_only'])
        self.assertIn('不修改文件',loaded['body']['role_developer_instructions'])
        self.assertIn('FromBase64String' if os.name=='nt' else "'\"'\"'",self.p['spawn_args']['message'])
        self.assertNotIn(self.task['input'],self.p['spawn_args']['message'])
        self.assertEqual(loaded['body']['bootstrap_read_paths'],[self.t['path']])
        self.report(self.t['sha256']);self.assertEqual(self.verify()['status'],'verified')

    def test_loader_rejects_tamper_and_wrong_identity(self):
        self.start()
        with self.assertRaisesRegex(StateError,'identity'):
            read_packet(self.t['path'],self.t['sha256'],'wrong-attempt')
        Path(self.t['path']).write_bytes(Path(self.t['path']).read_bytes()+b' ')
        with self.assertRaisesRegex(StateError,'changed'):
            read_packet(self.t['path'],self.t['sha256'],self.p['attempt_id'])

    def test_success_needs_receipt_and_retained_packet_bytes(self):
        self.start()
        with self.assertRaisesRegex(StateError,'packet_sha256'):self.report()
        self.report(self.t['sha256'])
        Path(self.t['path']).write_bytes(b'changed')
        with self.assertRaisesRegex(StateError,'changed'):self.verify()

    def test_known_failure_is_recorded_even_without_packet_ack(self):
        self.start(checks=True)
        r=self.report(check_results=[result(code=1)])
        self.assertEqual((r['status'],r['category']),('failed','validation'))

    def test_cli_reads_packet_only_not_whole_ledger(self):
        self.start()
        cmd=[sys.executable,'-B',str(Path(__file__).with_name('routing.py')),'--packet',self.t['path'],
             '--sha256',self.t['sha256'],'--attempt-id',self.p['attempt_id']]
        p=subprocess.run(cmd,capture_output=True,text=True,encoding='utf-8')
        self.assertEqual(p.returncode,0,p.stderr+p.stdout)
        value=json.loads(p.stdout)['result']
        self.assertEqual(value['packet_sha256'],self.t['sha256'])
        self.assertEqual(value['body']['task_id'],'task')
        self.assertNotIn('coordinator_id',value)

    def test_unknown_dispatch_cannot_be_reprepared_for_another_packet(self):
        task=make_task(self.root);task['packet_transport']='file'
        self.store.apply('add',self.owner,1,task=task);p=prepare(self.store,self.owner,1)
        with self.assertRaisesRegex(StateError,'active or unknown'):prepare(self.store,self.owner,1)
        self.assertEqual(len(self.store.status(self.owner,1)['attempts']),1)
        self.assertEqual(len(list((self.store.root/'packets').glob('*.json'))),1)

    def test_bad_readonly_options_never_consume_a_writable_request(self):
        target=self.root/'must-not-exist'
        payload=json.dumps({'op':'init','state_dir':str(target),'owner':'unauthorized-via-read-mode'})
        request_path=self.root/'request.json';request_path.write_text(payload,encoding='utf-8')
        for extra in [['--packet='],['--packet=','--request',str(request_path)],
                      ['--sha256','0'*64],['--attempt-id','wrong'],
                      ['--packet',str(self.root/'missing.json')]]:
            p=subprocess.run([sys.executable,'-B',str(Path(__file__).with_name('routing.py')),*extra],
                             input=payload,capture_output=True,text=True,encoding='utf-8',timeout=10)
            self.assertEqual(p.returncode,2,p.stdout+p.stderr)
            self.assertFalse(json.loads(p.stdout)['ok'])
            self.assertFalse(target.exists(),'read-only options reached ledger creation')


if __name__=='__main__':unittest.main()
