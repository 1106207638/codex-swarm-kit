import copy
import hashlib
import unittest
from unittest.mock import patch
from pathlib import Path
import test_outline_state as fixtures
from test_task_state import prepare, CATALOG, PROFILES, provenance
from task_state import StateError


class WorkEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.f=fixtures.OutlineStateTests();self.f.setUp()

    def second(self):
        f=self.f
        work=copy.deepcopy(f.spec['modules'][0]['work'][0]);work['task_id']='second'
        f.spec['modules'][0]['work'].insert(1,work)
        t=f.item('parse');t['task_id']='second'
        p=f.root/'second.py';p.write_text('other synthetic artifact',encoding='utf-8')
        t.update(ownership=[str(p)],artifacts=[str(p)],read_paths=[str(p)])
        return t

    def prep(self,tid,slots=2):
        f=self.f
        return f.store.apply('prepare',f.owner,1,task_id=tid,catalog=CATALOG,
                             catalog_provenance=provenance(),profiles_dir=PROFILES,host_slots_available=slots)

    def test_resource_claim_blocks_even_with_different_files(self):
        f=self.f;a=f.item('parse');b=self.second();a['resources']=['device:shared'];b['resources']=['DEVICE:SHARED']
        f.register();f.call('add',task=a);f.call('add',task=b);self.prep('parse')
        with self.assertRaisesRegex(StateError,'resource'):self.prep('second')
        self.assertEqual(len(f.store.status(f.owner,1)['attempts']),1)

    def test_unbound_dispatch_reserves_native_slot(self):
        f=self.f;a=f.item('parse');b=self.second();f.register();f.call('add',task=a);f.call('add',task=b)
        self.prep('parse',1)
        with self.assertRaisesRegex(StateError,'reserved'):self.prep('second',1)

    def test_zero_native_slots_queues_without_unknown_attempt(self):
        f=self.f;f.register();f.call('add',task=f.item('parse'))
        with self.assertRaisesRegex(StateError,'slots full'):self.prep('parse',0)
        self.assertFalse(f.store.status(f.owner,1)['attempts'])

    def test_old_writer_cannot_inject_outline_work_into_schema_one(self):
        f=self.f
        with patch('outline_state.guard'):
            f.call('add',task=f.item('parse'))
        with self.assertRaisesRegex(StateError,'explicit registration'):self.prep('parse')
        self.assertFalse(f.store.status(f.owner,1)['attempts'])

    def test_active_writer_blocks_declared_reader(self):
        f=self.f;a=f.item('parse');b=self.second();b['read_paths'].append(a['artifacts'][0])
        f.register();f.call('add',task=a);f.call('add',task=b);self.prep('parse')
        with self.assertRaisesRegex(StateError,'active writer'):self.prep('second')

    def test_conflict_rejected_before_snapshot_file_is_read(self):
        f=self.f;a=f.item('parse');b=self.second();p=a['artifacts'][0]
        b['read_paths'].append(p);b['snapshot_files']={p:hashlib.sha256(Path(p).read_bytes()).hexdigest()}
        f.register();f.call('add',task=a);f.call('add',task=b);self.prep('parse')
        with patch('work_evidence.check_hashes',side_effect=AssertionError('must not read active writer')):
            with self.assertRaisesRegex(StateError,'active writer'):self.prep('second')

    def test_active_reader_blocks_new_writer(self):
        f=self.f;a=f.item('parse');b=self.second();a['read_paths'].append(b['artifacts'][0])
        f.register();f.call('add',task=a);f.call('add',task=b);self.prep('parse')
        with self.assertRaisesRegex(StateError,'active reader'):self.prep('second')

    def test_direct_action_reserves_shared_resource(self):
        f=self.f;a=f.item('parse');b=self.second()
        f.spec['modules'][0]['work'][0].update(operation='controller',stage='strategy')
        a['operation']='controller';a['features']['stage']='strategy'
        a['resources']=b['resources']=['port:8765'];f.register()
        f.call('add',task=a);f.call('add',task=b)
        self.assertEqual(self.prep('parse')['status'],'direct_pending')
        with self.assertRaisesRegex(StateError,'resource'):self.prep('second')

    def test_review_requires_exact_dependency_snapshot(self):
        f=self.f;f.register();f.run_item(f.item('parse'))
        t=f.item('parse',True);t['snapshot_files']={str(f.plan):hashlib.sha256(f.plan.read_bytes()).hexdigest()};t['read_paths'].append(str(f.plan))
        f.call('add',task=t)
        with self.assertRaisesRegex(StateError,'dependency artifacts'):self.prep('parse-review')

    def test_review_cannot_accept_unverified_new_artifact_version(self):
        f=self.f;f.register();f.run_item(f.item('parse'))
        (f.root/'parse/output.py').write_text('changed after verification',encoding='utf-8')
        f.call('add',task=f.item('parse',True))
        with self.assertRaisesRegex(StateError,'signed artifact'):self.prep('parse-review')

    def report_ready(self):
        f=self.f;f.register();t=f.item('parse');f.call('add',task=t);r=self.prep('parse')
        f.call('bind',attempt_id=r['attempt_id'],agent_id='worker')
        return t,r

    def report(self,t,r,h):
        return self.f.call('report',attempt_id=r['attempt_id'],agent_id='worker',input_digest=r['input_digest'],
                           summary='fixture',evidence=['fixture'],host_stopped_evidence='stopped',handoff=h)

    def verify(self,r):
        return self.f.call('verify',attempt_id=r['attempt_id'],input_digest=r['input_digest'],verifier_context_id=self.f.owner,evidence=['checked'])

    def test_artifact_change_after_report_blocks_verification(self):
        t,r=self.report_ready();self.report(t,r,self.f.handoff(t))
        Path(t['artifacts'][0]).write_text('changed',encoding='utf-8')
        with self.assertRaisesRegex(StateError,'artifact'):self.verify(r)

    def test_failed_check_is_retained_but_cannot_pass(self):
        t,r=self.report_ready();h=self.f.handoff(t);h['checks'][0]['exit_code']=1
        self.report(t,r,h)
        with self.assertRaisesRegex(StateError,'not passed'):self.verify(r)
        self.assertEqual(self.f.store.status(self.f.owner,1)['attempts'][r['attempt_id']]['status'],'reported')

    def test_missing_handoff_or_unverified_work_cannot_pass(self):
        t,r=self.report_ready()
        with self.assertRaisesRegex(StateError,'handoff'):self.report(t,r,None)
        h=self.f.handoff(t);h['remaining']=['not tested'];self.report(t,r,h)
        with self.assertRaisesRegex(StateError,'unverified'):self.verify(r)

    def test_review_input_mutation_blocks_verification(self):
        f=self.f;f.register();f.run_item(f.item('parse'));t=f.item('parse',True);f.call('add',task=t)
        r=self.prep('parse-review');f.call('bind',attempt_id=r['attempt_id'],agent_id='worker')
        self.report(t,r,f.handoff(t));Path(next(iter(t['snapshot_files']))).write_text('changed',encoding='utf-8')
        with self.assertRaisesRegex(StateError,'snapshot'):self.verify(r)

    def test_mutation_after_verified_blocks_module_acceptance(self):
        f=self.f;f.register();f.run_item(f.item('parse'));f.run_item(f.item('parse',True))
        (f.root/'parse/output.py').write_text('changed',encoding='utf-8')
        with self.assertRaisesRegex(StateError,'artifact'):f.accept('parse')

    def test_status_reports_modules_and_unknown_attempt_without_advancing(self):
        f=self.f;f.register();f.call('add',task=f.item('parse'));self.prep('parse')
        s=f.call('outline_status')
        self.assertEqual(s['modules'][0]['work'][0]['status'],'active_or_unknown')
        self.assertEqual(s['modules'][1]['status'],'queued');self.assertFalse(s['complete'])

    def test_final_acceptance_rechecks_frozen_inputs(self):
        f=self.f;f.register();t=f.item('parse');p=f.root/'input.json';p.write_text('{}',encoding='utf-8')
        t['read_paths'].append(str(p));t['snapshot_files']={str(p):hashlib.sha256(p.read_bytes()).hexdigest()}
        f.run_item(t);f.run_item(f.item('parse',True));f.accept('parse');f.finish_module('cli')
        p.write_text('{"changed":true}',encoding='utf-8')
        with self.assertRaisesRegex(StateError,'final evidence'):
            f.call('outline_complete',evidence={'integrated':['check']},verifier_context_id=f.owner)

    def test_status_shows_model_failure_human_lock(self):
        f=self.f;f.register();f.call('add',task=f.item('parse'));r=self.prep('parse')
        f.call('fail',attempt_id=r['attempt_id'],category='model',evidence=['model unavailable'],host_no_spawn_evidence='not spawned')
        s=f.call('outline_status')
        self.assertEqual(s['modules'][0]['work'][0]['status'],'human_decision_required')

    def test_later_signed_output_replaces_prior_snapshot_by_canonical_path(self):
        f=self.f;f.register();f.finish_module('parse')
        path=f.root/'parse/output.py';path.write_text('intentional next-module output',encoding='utf-8')
        alias=str(path.parent/'..'/'parse'/'output.py')
        t=f.item('cli');t.update(ownership=[str(path)],artifacts=[alias],read_paths=[alias],depends_on=['parse'])
        f.run_item(t)
        review=f.item('cli',True);review.update(ownership=[str(path)],read_paths=[str(path)],
                     snapshot_files={str(path):hashlib.sha256(path.read_bytes()).hexdigest()})
        f.run_item(review);f.accept('cli')
        result=f.call('outline_complete',evidence={'integrated':['rechecked all behavior']},verifier_context_id=f.owner)
        self.assertEqual(result['status'],'complete')

    def test_reordered_work_list_does_not_change_actual_dependency_order(self):
        self.f.spec['modules'][1]['work'].reverse()
        self.test_later_signed_output_replaces_prior_snapshot_by_canonical_path()


if __name__=='__main__':unittest.main()
