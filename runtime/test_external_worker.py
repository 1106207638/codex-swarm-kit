import copy
import json
from pathlib import Path
import tempfile
import unittest

from external_contract import check_result
import external_worker as external
from router_policy import decide
from routing import run
from task_state import Store, StateError
from test_task_state import make_task, CATALOG, PROFILES, provenance
import test_outline_state as outline_tests


MODEL = 'qwen3-fixture'
CAPABILITIES = {'protocol': 'local-worker/v1', 'configured_model': MODEL,
                'queue_concurrency': 1, 'kinds': ['classify', 'extract', 'translate', 'summarize'],
                'max_items': 24, 'max_request_bytes': 196608, 'max_input_characters': 18000}


def selection():
    return {'protocol':'local-worker/v1', 'model':MODEL, 'reason':'controller chose bounded classification',
            'task':{'kind':'classify', 'instruction':'Classify each record.', 'labels':['bug','question'],
                    'items':[{'id':'a','text':'app crashes when pressing save'}]},
            'standard':{'criteria':['Preserve id. Classify crashes as bug.'],
                        'checks':[{'id':'fixed-fixture','item_id':'a','op':'equals','expected':'bug'}]}}


def finish_job(prepared, value='bug', status='succeeded'):
    request = prepared['external_call']['arguments']['request']
    job = {'protocol':'local-worker/v1','task_id':request['task_id'],'id':'a'*32,'status':status}
    if status == 'succeeded':
        output = {'items':[{'id':'a','value':value}]}
        job.update(model_used=MODEL, result=output, acceptance={
            'status':'automated_checks_passed', 'checks':check_result(request['standard'],request['task'],output),
            'parent_review_required':True})
    elif status == 'failed':
        job.update(error={'kind':'acceptance_failed','detail':'synthetic fixture'})
    return job


class ExternalWorkerTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.store = Store(self.root/'ledger')
        self.owner = 'controller'
        self.store.init(self.owner)
        self.item = make_task(self.root)
        self.item['external_worker'] = selection()
        self.item['features']['available_tools'] = list(external.TOOLS.values())

    def call(self, op, **p):
        return self.store.apply(op, self.owner, 1, **p)

    def prepare(self, item=None, **p):
        item = item or self.item
        if item['task_id'] not in self.store.status(self.owner,1)['tasks']:
            self.call('add',task=item)
        return self.call('prepare',task_id=item['task_id'],catalog=CATALOG,catalog_provenance=provenance(),
                         profiles_dir=PROFILES,external_capabilities=CAPABILITIES,host_slots_available=2,**p)

    def bind(self, p, **fields):
        return self.call('external_bind',attempt_id=p['attempt_id'],input_digest=p['input_digest'],
                         receipt=finish_job(p,status='queued'),**fields)

    def test_switched_url_with_same_model_blocks_resume_and_freezes_submit_argument(self):
        caps={**CAPABILITIES,'backend_ref':'a'*64}
        self.call('add',task=self.item)
        p=self.call('prepare',task_id=self.item['task_id'],catalog=CATALOG,catalog_provenance=provenance(),
                    external_capabilities=caps,host_slots_available=0)
        self.assertEqual(p['external_call']['arguments']['backend_ref'],caps['backend_ref'])
        with self.assertRaisesRegex(StateError,'external capabilities changed'):
            self.call('external_resume',attempt_id=p['attempt_id'],input_digest=p['input_digest'],
                      external_capabilities={**caps,'backend_ref':'b'*64},recovery_evidence=['same queue'])

    def poll(self,p,job=None,**fields):
        return self.call('external_poll',attempt_id=p['attempt_id'],input_digest=p['input_digest'],
                         receipt=job or finish_job(p),summary='candidate received',evidence=['MCP fixture'],**fields)

    def test_external_lifecycle_does_not_approve_result_before_controller(self):
        pending = self.prepare()
        self.assertNotIn('spawn_args',pending)
        self.assertEqual(pending['external_call']['tool'],external.TOOLS['submit'])
        self.assertEqual(self.bind(pending)['status'],'bound')
        self.assertEqual(self.poll(pending,finish_job(pending,status='running'))['external_status'],'running')
        self.assertEqual(self.poll(pending)['status'],'reported')
        self.assertFalse(self.store.status(self.owner,1)['tasks']['task']['verified'])
        result = self.call('verify',attempt_id=pending['attempt_id'],input_digest=pending['input_digest'],
                           verifier_context_id=self.owner,evidence=['controller checked crash classification'])
        self.assertEqual(result['status'],'verified')

    def test_fast_finished_submit_receipt_can_bind_then_get_full_result(self):
        p=self.prepare()
        receipt=finish_job(p,status='queued')
        receipt['status']='succeeded'
        self.call('external_bind',attempt_id=p['attempt_id'],input_digest=p['input_digest'],receipt=receipt)
        self.assertEqual(self.poll(p)['status'],'reported')

    def test_unknown_submission_recovers_original_request(self):
        pending=self.prepare()
        reopened=Store(self.root/'ledger')
        same=reopened.apply('external_resume',self.owner,1,attempt_id=pending['attempt_id'],input_digest=pending['input_digest'],
                            external_capabilities=CAPABILITIES,recovery_evidence=['same service and persisted idempotency fixture'])
        self.assertEqual(same['external_call'],pending['external_call'])
        with self.assertRaisesRegex(StateError,'active or unknown'):
            self.prepare()
        self.bind(pending)
        resumed=self.call('external_resume',attempt_id=pending['attempt_id'],input_digest=pending['input_digest'])
        self.assertEqual(resumed['external_call'],{'tool':external.TOOLS['get'],'arguments':{'job_id':'a'*32}})
        self.assertEqual(len(self.store.status(self.owner,1)['attempts']),1)

    def test_service_failure_preserves_human_only_model_recovery(self):
        p=self.prepare()
        self.bind(p)
        job=finish_job(p,status='failed')
        job['error']['kind']='connection'
        failed=self.poll(p,job)
        self.assertEqual((failed['status'],failed['category']),('failed','model'))
        with self.assertRaisesRegex(StateError,'human-only'):
            self.call('retry',attempt_id=p['attempt_id'],controller_reason='no automatic fallback',human_authorized=False)

    def test_service_invalid_output_can_mean_backend_model_mismatch(self):
        p=self.prepare()
        self.bind(p)
        job=finish_job(p,status='failed')
        job['error']={'kind':'invalid_output','detail':'Provider returned a different model'}
        self.assertEqual(self.poll(p,job)['category'],'model')
        with self.assertRaisesRegex(StateError,'human-only'):
            self.call('retry',attempt_id=p['attempt_id'],controller_reason='service model mismatch',human_authorized=False)

    def test_pending_failure_can_poll_to_termination_without_becoming_green(self):
        p=self.prepare()
        self.bind(p)
        failed=self.call('fail',attempt_id=p['attempt_id'],agent_id='local_worker:'+'a'*32,
                         category='environment',evidence=['polling connection lost'])
        self.assertEqual(failed['status'],'failure_reported')
        query=self.call('external_resume',attempt_id=p['attempt_id'],input_digest=p['input_digest'])
        self.assertEqual(query['external_call']['tool'],external.TOOLS['get'])
        waiting=self.poll(p,finish_job(p,status='running'))
        self.assertEqual(waiting['status'],'failure_reported')
        terminal=self.poll(p)
        self.assertEqual(terminal['status'],'failed')
        self.assertFalse(self.store.status(self.owner,1)['tasks']['task']['verified'])
        with self.assertRaises(StateError):
            self.call('verify',attempt_id=p['attempt_id'],input_digest=p['input_digest'],
                      verifier_context_id=self.owner,evidence=['candidate after failure'])

    def test_automated_failure_needs_recorded_root_cause_before_retry(self):
        p=self.prepare()
        self.bind(p)
        self.assertEqual(self.poll(p,finish_job(p,status='failed'))['category'],'validation')
        with self.assertRaisesRegex(StateError,'retry authorization'):
            self.prepare()
        self.call('retry',attempt_id=p['attempt_id'],controller_reason='clarified fixture wording',human_authorized=False)
        next_attempt=self.prepare()
        self.assertNotEqual(p['external_call']['arguments']['request']['task_id'],
                            next_attempt['external_call']['arguments']['request']['task_id'])

    def test_wrong_job_cannot_be_reported(self):
        p=self.prepare()
        self.bind(p)
        for change in ({'id':'b'*32},{'task_id':'unrelated'}):
            with self.subTest(change=change), self.assertRaises(StateError):
                self.poll(p,dict(finish_job(p),**change))
        self.assertEqual(self.store.status(self.owner,1)['attempts'][p['attempt_id']]['status'],'bound')

    def test_mismatching_model_is_persisted_as_human_only_failure(self):
        p=self.prepare()
        self.bind(p)
        result=self.poll(p,dict(finish_job(p),model_used='other'))
        self.assertEqual((result['status'],result['category']),('failed','model'))
        self.store=Store(self.root/'ledger')
        with self.assertRaisesRegex(StateError,'human-only'):
            self.call('retry',attempt_id=p['attempt_id'],controller_reason='mismatch',human_authorized=False)
        with self.assertRaises(StateError):
            self.prepare()
        self.assertFalse(self.store.status(self.owner,1)['tasks']['task']['verified'])

    def test_successful_receipt_failing_frozen_checks_records_failure(self):
        p=self.prepare()
        self.bind(p)
        result=self.poll(p,finish_job(p,value='question'))
        self.assertEqual((result['status'],result['category']),('failed','validation'))

    def test_resume_requires_current_capabilities_and_recovery_evidence(self):
        p=self.prepare()
        for extra in ({},{'external_capabilities':CAPABILITIES},
                      {'external_capabilities':dict(CAPABILITIES,configured_model='changed'),'recovery_evidence':['fixture']}):
            with self.subTest(extra=extra),self.assertRaises(StateError):
                self.call('external_resume',attempt_id=p['attempt_id'],input_digest=p['input_digest'],**extra)

    def test_generic_native_operations_cannot_bypass_external_receipt(self):
        p=self.prepare()
        with self.assertRaisesRegex(StateError,'external_bind'):
            self.call('bind',attempt_id=p['attempt_id'],agent_id='forged')
        self.bind(p)
        with self.assertRaisesRegex(StateError,'external_poll'):
            self.call('report',attempt_id=p['attempt_id'])
        with self.assertRaises(StateError):
            self.call('verify',attempt_id=p['attempt_id'],input_digest=p['input_digest'],
                      verifier_context_id=self.owner,evidence=['unverified result'])

    def test_one_external_slot_is_separate_from_two_native_slots(self):
        self.prepare()
        other=copy.deepcopy(self.item)
        other['task_id']='other'
        with self.assertRaisesRegex(StateError,'external queue already'):
            self.prepare(other)
        for i in range(2):
            native=make_task(self.root,'native-'+str(i),path=self.root/str(i))
            prepared=self.prepare(native)
            self.assertIn('spawn_args',prepared)
        blocked=make_task(self.root,'third-native',path=self.root/'third')
        with self.assertRaisesRegex(StateError,'active subagent limit'):
            self.prepare(blocked)

    def test_controller_selection_preserves_restrictions_and_scope(self):
        for mutate,reason in [
            (lambda t:t['features'].update(stage='review'),'blocked_external_scope'),
            (lambda t:t['features'].update(stage='planning'),'blocked_external_scope'),
            (lambda t:t['features'].update(risk='high'),'blocked_external_scope'),
            (lambda t:t['features'].update(complexity='complex'),'blocked_external_scope'),
            (lambda t:t['features'].update(ambiguity='unknown'),'blocked_external_scope'),
            (lambda t:t['features'].update(plan_ready=False),'blocked_external_scope'),
            (lambda t:t['features'].update(available_tools=[]),'blocked_environment'),
            (lambda t:t['delegation'].update(prohibited=True),'blocked_delegation_authority'),
            (lambda t:t.update(override={'scope':'all_tasks','model':'gpt-6-astra','source':'user'}),'blocked_external_restriction'),
        ]:
            item=copy.deepcopy(self.item)
            mutate(item)
            with self.subTest(reason=reason):
                self.assertEqual(decide(item,CATALOG)['reason_codes'],[reason])

    def test_changed_capability_or_request_cannot_dispatch(self):
        self.call('add',task=self.item)
        with self.assertRaisesRegex(StateError,'protocol or model changed'):
            self.call('prepare',task_id='task',catalog=CATALOG,catalog_provenance=provenance(),
                      external_capabilities=dict(CAPABILITIES,configured_model='changed'))
        self.assertEqual(self.store.status(self.owner,1)['attempts'],{})

    def test_logs_track_external_lifecycle_without_text_or_full_model(self):
        request={'state_dir':str(self.store.root),'owner':self.owner,'epoch':1}
        response,code=run({**request,'op':'add','task':self.item})
        self.assertEqual(code,0,response)
        response,code=run({**request,'op':'prepare','task_id':'task','catalog':CATALOG,
                           'catalog_provenance':provenance(),'external_capabilities':CAPABILITIES})
        self.assertEqual(code,0,response)
        p=response['result']
        for op,receipt in [('external_bind',finish_job(p,status='queued')),('external_poll',finish_job(p))]:
            response,code=run({**request,'op':op,'attempt_id':p['attempt_id'],'input_digest':p['input_digest'],
                               'receipt':receipt,'summary':'candidate','evidence':['fixture']})
            self.assertEqual(code,0,response)
        response,code=run({**request,'op':'call_log','attempt_id':p['attempt_id']})
        self.assertEqual(code,0,response)
        rows=response['result']['records']
        self.assertEqual([r['op'] for r in rows if r['phase']=='finish'],['prepare','external_bind','external_poll'])
        raw=(self.store.root/'calls.jsonl').read_text()
        self.assertNotIn('app crashes',raw)
        self.assertNotIn(MODEL,raw)
        self.assertTrue(rows[-1]['external_model_ref'])

    def test_external_gpt_shaped_model_is_logged_only_as_digest(self):
        model='gpt-6.2-sol'
        self.item['external_worker']['model']=model
        base={'state_dir':str(self.store.root),'owner':self.owner,'epoch':1}
        self.assertEqual(run({**base,'op':'add','task':self.item})[1],0)
        response,code=run({**base,'op':'prepare','task_id':'task','catalog':CATALOG,
            'catalog_provenance':provenance(),'external_capabilities':dict(CAPABILITIES,configured_model=model)})
        self.assertEqual(code,0,response)
        response,code=run({**base,'op':'call_log','attempt_id':response['result']['attempt_id']})
        self.assertEqual(code,0,response)
        self.assertEqual(response['result']['records'][-1]['requested_model'],'unknown')
        self.assertNotIn(model,(self.store.root/'calls.jsonl').read_text())

    def test_outline_executor_result_requires_review_before_module_acceptance(self):
        fixture=outline_tests.OutlineStateTests()
        fixture.setUp()
        fixture.register()
        t=fixture.item('parse')
        t['external_worker']=selection()
        t['features']['available_tools']=list(external.TOOLS.values())
        fixture.call('add',task=t)
        p=fixture.call('prepare',task_id='parse',catalog=CATALOG,catalog_provenance=provenance(),
                       external_capabilities=CAPABILITIES,host_slots_available=0)
        fixture.call('external_bind',attempt_id=p['attempt_id'],input_digest=p['input_digest'],
                     receipt=finish_job(p,status='queued'))
        fixture.call('external_poll',attempt_id=p['attempt_id'],input_digest=p['input_digest'],
                     receipt=finish_job(p),summary='candidate',evidence=['fixture'],handoff=fixture.handoff(t))
        fixture.call('verify',attempt_id=p['attempt_id'],input_digest=p['input_digest'],
                     verifier_context_id=fixture.owner,evidence=['controller reviewed'])
        with self.assertRaises(StateError):
            fixture.accept('parse')
        fixture.run_item(fixture.item('parse',True))
        self.assertEqual(fixture.accept('parse')['status'],'accepted')

    def test_outline_resume_rejects_changed_plan_but_bound_query_remains_available(self):
        fixture=outline_tests.OutlineStateTests()
        fixture.setUp()
        fixture.register()
        t=fixture.item('parse')
        t['external_worker']=selection()
        t['features']['available_tools']=list(external.TOOLS.values())
        fixture.call('add',task=t)
        p=fixture.call('prepare',task_id='parse',catalog=CATALOG,catalog_provenance=provenance(),
                       external_capabilities=CAPABILITIES,host_slots_available=0)
        fixture.plan.write_text('changed plan',encoding='utf-8')
        with self.assertRaisesRegex(StateError,'plan content changed'):
            fixture.call('external_resume',attempt_id=p['attempt_id'],input_digest=p['input_digest'],
                         external_capabilities=CAPABILITIES,recovery_evidence=['same queue fixture'])
        fixture.call('external_bind',attempt_id=p['attempt_id'],input_digest=p['input_digest'],
                     receipt=finish_job(p,status='queued'))
        r=fixture.call('external_resume',attempt_id=p['attempt_id'],input_digest=p['input_digest'])
        self.assertEqual(r['external_call']['tool'],external.TOOLS['get'])


if __name__=='__main__':
    unittest.main()
