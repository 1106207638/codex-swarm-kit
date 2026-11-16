import copy
import tempfile
import unittest
from pathlib import Path

from check_protocol import classify, validate_plan
from task_state import Store, StateError
from test_task_state import make_task, prepare


def spec(cid='quality', kind='acceptance', codes=None, signal=''):
    return dict(id=cid, command='python check.py', kind=kind,
                expected_exit_codes=[0] if codes is None else codes, expected_signal=signal)


def result(cid='quality', code=0, error=None, signal=''):
    return dict(id=cid, exit_code=code, error_kind=error, observed_signal=signal,
                evidence_ref='host-trace:command-1')


class ClassifiedCheckTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name); self.store = Store(self.root/'ledger')
        self.owner = 'controller'; self.store.init(self.owner)

    def start(self, plan=None, direct=False):
        self.task = make_task(self.root, stage='strategy' if direct else 'execution')
        if plan is not None: self.task['check_plan'] = plan
        self.store.apply('add', self.owner, 1, task=self.task)
        self.p = prepare(self.store, self.owner, 1)
        self.direct = direct
        if not direct: self.store.apply('bind', self.owner, 1, attempt_id=self.p['attempt_id'], agent_id='worker')

    def call(self, op, **kw):
        common = dict(input_digest=self.p['input_digest'])
        if self.direct: common.update(task_id='task', direct_id=self.p['direct_id'])
        else: common.update(attempt_id=self.p['attempt_id'], agent_id='worker')
        return self.store.apply(op, self.owner, 1, **common, **kw)

    def report(self, results=None):
        args = dict(summary='done', evidence=['host result'], host_stopped_evidence='host stopped',
                    host_reported_model=self.p['decision']['model'], host_reported_effort=self.p['decision']['effort'])
        if results is not None: args['check_results'] = results
        return self.call('report', **args)

    def verify(self):
        return self.call('verify', verifier_context_id='independent', evidence=['checked host trace'])

    def test_probe_missing_file_does_not_fail_or_replace_acceptance(self):
        self.start([spec('exists','probe',[0,1],'not-yet-created'), spec()])
        self.call('record_check', check_results=[result('exists',1,signal='not-yet-created')])
        self.assertEqual(self.store.status(self.owner,1)['attempts'][self.p['attempt_id']]['status'],'bound')
        self.report([result()]); self.assertEqual(self.verify()['status'],'verified')

    def test_expected_negative_requires_distinguishing_signal(self):
        self.start([spec(kind='expected_failure',codes=[1],signal='INVALID_QUOTE')])
        self.report([result(code=1,signal='INVALID_QUOTE row 3')])
        self.assertEqual(self.verify()['status'],'verified')

    def test_checker_error_even_with_expected_exit_is_not_negative_success(self):
        self.start([spec(kind='expected_failure',codes=[1],signal='INVALID_QUOTE')])
        r=self.report([result(code=1,error='checker',signal='INVALID_QUOTE')])
        self.assertEqual((r['status'],r['category']),('failed','checker'))

    def test_bad_signature_is_unknown_and_not_product_defect(self):
        verdict=classify(spec(kind='expected_failure',codes=[1],signal='BAD_INPUT'),result(code=1,signal='Traceback'))
        self.assertEqual((verdict['outcome'],verdict['category']),('unknown','checker'))

    def test_csv_failure_is_persisted_and_cannot_be_replaced_by_green(self):
        self.start([spec()]); r=self.report([result(code=1,signal='unquoted-quote failed')])
        self.assertEqual(r['status'],'failed')
        with self.assertRaises(StateError): self.report([result()])
        with self.assertRaises(StateError): self.verify()
        a=self.store.status(self.owner,1)['attempts'][self.p['attempt_id']]
        self.assertEqual(a['check_records']['quality']['result']['exit_code'],1)

    def test_failure_without_stop_retains_resource_reservation(self):
        self.start([spec()]); self.call('record_check',check_results=[result(code=1)])
        a=self.store.status(self.owner,1)['attempts'][self.p['attempt_id']]
        self.assertEqual(a['status'],'failure_reported');self.assertFalse(a['worker_stopped'])
        with self.assertRaises(StateError): self.call('retry',human_authorized=False,root_cause_and_fix='repair')

    def test_failure_with_missing_other_result_is_still_persisted(self):
        self.start([spec(),spec('second')]);self.report([result(code=1)])
        self.assertEqual(self.store.status(self.owner,1)['attempts'][self.p['attempt_id']]['status'],'failed')

    def test_cannot_omit_plan_results_or_change_expected_codes_in_report(self):
        self.start([spec()])
        with self.assertRaises(StateError):self.report()
        row=result();row['expected_exit_codes']=[1]
        with self.assertRaises(StateError):self.report([row])
        self.assertEqual(self.store.status(self.owner,1)['attempts'][self.p['attempt_id']]['status'],'bound')

    def test_probe_only_is_not_acceptance(self):
        self.start([spec(kind='probe')])
        with self.assertRaisesRegex(StateError,'probes alone'):self.report([result()])

    def test_legacy_calls_remain_explicitly_unclassified(self):
        self.start();self.assertEqual(self.p['check_contract'],'unclassified_legacy')
        self.report();self.assertEqual(self.verify()['status'],'verified')

    def test_two_business_failures_still_need_human(self):
        self.start([spec()]);self.report([result(code=1)])
        self.call('retry',human_authorized=False,root_cause_and_fix='root fixes quote handling')
        self.p=prepare(self.store,self.owner,1)
        self.store.apply('bind',self.owner,1,attempt_id=self.p['attempt_id'],agent_id='worker-2')
        self.store.apply('record_check',self.owner,1,attempt_id=self.p['attempt_id'],agent_id='worker-2',
                         input_digest=self.p['input_digest'],check_results=[result(code=1)],host_stopped_evidence='stopped')
        self.call('retry',human_authorized=False,root_cause_and_fix='second correction')
        with self.assertRaisesRegex(StateError,'human authorization'):prepare(self.store,self.owner,1)

    def test_model_error_keeps_human_lock_across_input_change(self):
        self.start([spec()]);self.report([result(code=None,error='model')])
        self.task['input_version']='v2';self.store.apply('update',self.owner,1,task=self.task)
        with self.assertRaisesRegex(StateError,'human-only'):self.call('retry',human_authorized=False,root_cause_and_fix='not authorized')

    def test_direct_failure_survives_reconcile_and_cannot_complete(self):
        self.start([spec()],direct=True)
        r=self.call('complete_direct',check_results=[result(code=1)])
        self.assertTrue(r['requires_controller'])
        self.assertTrue(self.call('complete_direct',check_results=[result()])['requires_controller'])
        self.call('reconcile_direct',action='stopped',evidence=['root stopped'],controller_reason='Correct parser then retry original acceptance',human_authorized=False)
        h=self.store.status(self.owner,1)['tasks']['task']['direct_history'][-1]
        self.assertEqual(h['receipt']['check_records']['quality']['result']['exit_code'],1)

    def test_invalid_plan_bool_exit_code_and_blank_nonzero_signature_rejected(self):
        for p in [[spec(codes=[True])],[spec(kind='probe',codes=[1])],[]]:
            with self.assertRaises(StateError):validate_plan(p)

    def test_direct_model_error_stops_without_permission_but_retry_requires_user(self):
        self.start([spec()],direct=True)
        self.call('record_check',check_results=[result(code=None,error='model')])
        with self.assertRaises(StateError): self.store.apply('update',self.owner,1,task=self.task)
        self.call('reconcile_direct',action='stopped',evidence=['host stopped'])
        with self.assertRaises(StateError):prepare(self.store,self.owner,1)
        with self.assertRaisesRegex(StateError,'human-only'):
            self.call('retry',controller_reason='model rejected',human_authorized=False)
        self.call('retry',controller_reason='user explicitly selected retry',human_authorized=True)
        self.assertEqual(prepare(self.store,self.owner,1)['status'],'direct_pending')

    def test_immutable_success_observation_and_probe_failure_classification(self):
        self.start([spec()]);self.call('record_check',check_results=[result()])
        self.assertTrue(self.call('record_check',check_results=[result(code=1)])['requires_controller'])
        with self.assertRaises(StateError):self.report()
        v=classify(spec(kind='probe',codes=[0,1],signal='no match'),result(code=2,signal='permission denied'))
        self.assertEqual(v['category'],'checker')

    def test_valid_failure_survives_malformed_sibling_and_bad_stop_metadata(self):
        self.start([spec()])
        r=self.call('report',check_results=[None, result(code=1), {'id':'unknown'}],host_stopped_evidence=123)
        self.assertEqual(r['status'],'failure_reported')
        a=self.store.status(self.owner,1)['attempts'][self.p['attempt_id']]
        self.assertEqual(a['check_records']['quality']['result']['exit_code'],1)
        self.assertFalse(a['worker_stopped']);self.assertTrue(a['check_protocol_errors'])
        with self.assertRaises(StateError):self.report([result()])

    def test_same_batch_green_then_red_retains_failure_and_first_observation(self):
        self.start([spec()]);self.report([result(),result(code=1)])
        a=self.store.status(self.owner,1)['attempts'][self.p['attempt_id']]
        self.assertEqual(a['status'],'failed');self.assertEqual(a['check_records']['quality']['result']['exit_code'],0)
        self.assertEqual(len(a['check_observations']),2)

    def test_probe_both_normal_branches(self):
        s=spec(kind='probe',codes=[0,1],signal='no match')
        for r in [result(code=0,signal='found foo'),result(code=1,signal='no match')]:
            self.assertFalse(classify(s,r)['requires_controller'])

    def test_completed_direct_replay_cannot_ignore_conflicting_result(self):
        self.start([spec()],direct=True)
        args=dict(execution_evidence=['executed'],acceptance_evidence=['reviewed'],verifier_context_id='root')
        self.call('complete_direct',check_results=[result()],**args)
        self.assertTrue(self.call('complete_direct',check_results=[result()],**args)['idempotent'])
        with self.assertRaisesRegex(StateError,'conflicting completed'):
            self.call('complete_direct',check_results=[result(code=1)],**args)

    def test_cross_direct_delegate_failures_cannot_reset_two_failure_gate(self):
        self.start([spec()],direct=True)
        self.call('record_check',check_results=[result(code=1)])
        self.call('reconcile_direct',action='stopped',evidence=['stopped'])
        self.call('retry',controller_reason='first root correction',human_authorized=False)
        self.task['features']['stage']='execution';self.task['input_version']='v2'
        self.store.apply('update',self.owner,1,task=self.task)
        self.p=prepare(self.store,self.owner,1);self.direct=False
        self.store.apply('bind',self.owner,1,attempt_id=self.p['attempt_id'],agent_id='worker')
        self.report([result(code=1)])
        self.call('retry',controller_reason='second correction',human_authorized=False)
        with self.assertRaisesRegex(StateError,'human authorization'):prepare(self.store,self.owner,1)
        self.call('retry',controller_reason='user authorized next try',human_authorized=True)
        self.assertEqual(prepare(self.store,self.owner,1)['status'],'dispatch_pending')

    def test_direct_then_public_fail_shares_stop_point(self):
        self.start([spec()],direct=True)
        self.call('record_check',check_results=[result(code=1)])
        self.call('reconcile_direct',action='stopped',evidence=['stopped'])
        self.call('retry',controller_reason='fix first failure',human_authorized=False)
        self.task['features']['stage']='execution';self.task['input_version']='v2'
        self.store.apply('update',self.owner,1,task=self.task)
        self.p=prepare(self.store,self.owner,1);self.direct=False
        self.store.apply('bind',self.owner,1,attempt_id=self.p['attempt_id'],agent_id='worker')
        self.call('fail',category='validation',evidence=['legacy public failure'],host_stopped_evidence='stopped')
        self.call('retry',controller_reason='second fix',human_authorized=False)
        with self.assertRaisesRegex(StateError,'human authorization'):prepare(self.store,self.owner,1)

    def test_public_fail_then_direct_shares_stop_point(self):
        self.start([spec()])
        self.call('fail',category='validation',evidence=['public failure'],host_stopped_evidence='stopped')
        self.call('retry',controller_reason='fix first failure',human_authorized=False)
        self.task['features']['stage']='strategy';self.task['input_version']='v2'
        self.store.apply('update',self.owner,1,task=self.task)
        self.p=prepare(self.store,self.owner,1);self.direct=True
        self.call('record_check',check_results=[result(code=1)])
        self.call('reconcile_direct',action='stopped',evidence=['stopped'])
        self.call('retry',controller_reason='second fix',human_authorized=False)
        with self.assertRaisesRegex(StateError,'human authorization'):prepare(self.store,self.owner,1)

    def test_late_model_observation_strengthens_lock_without_losing_first_failure(self):
        self.start([spec(),spec('api')])
        self.call('record_check',check_results=[result(code=1)])
        self.assertTrue(self.call('record_check',check_results=[result('api')])['requires_controller'])
        self.call('record_check',check_results=[result('api'),result('api',None,'model')])
        a=self.store.status(self.owner,1)['attempts'][self.p['attempt_id']]
        self.assertEqual(a['failure_category'],'model');self.assertFalse(a['worker_stopped'])
        self.assertEqual(a['failure_observations'][0]['category'],'validation')
        self.call('fail',category='environment',evidence=['later weaker report'])
        self.assertEqual(self.store.status(self.owner,1)['attempts'][self.p['attempt_id']]['failure_category'],'model')
        self.call('reconcile',action='stopped',evidence=['host stopped'])
        with self.assertRaisesRegex(StateError,'human-only'):self.call('retry',controller_reason='ordinary fix',human_authorized=False)

    def test_public_late_model_failure_is_recorded(self):
        self.start()
        self.call('fail',category='validation',evidence=['first'])
        self.call('fail',category='model',evidence=['late model refusal'],host_stopped_evidence=False)
        a=self.store.status(self.owner,1)['attempts'][self.p['attempt_id']]
        self.assertEqual(a['failure_category'],'model')
        self.assertEqual(a['failure_evidence'],['first'])

    def test_success_breaks_consecutive_failures_but_keeps_history(self):
        self.start([spec()],direct=True)
        self.call('record_check',check_results=[result(code=1)])
        self.call('reconcile_direct',action='stopped',evidence=['stopped'])
        self.call('retry',controller_reason='repair',human_authorized=False)
        self.p=prepare(self.store,self.owner,1)
        self.call('complete_direct',check_results=[result()],execution_evidence=['executed'],acceptance_evidence=['accepted'],verifier_context_id='root')
        self.task['input_version']='new-input';self.store.apply('update',self.owner,1,task=self.task)
        self.p=prepare(self.store,self.owner,1)
        self.call('record_check',check_results=[result(code=1)])
        self.call('reconcile_direct',action='stopped',evidence=['stopped'])
        self.call('retry',controller_reason='first failure after success',human_authorized=False)
        self.assertEqual(prepare(self.store,self.owner,1)['status'],'direct_pending')
        history=self.store.status(self.owner,1)['tasks']['task']['check_failure_history']
        self.assertEqual(len(history),2);self.assertNotEqual(history[0]['success_generation'],history[1]['success_generation'])

    def test_completed_replay_revalidates_exit_type(self):
        self.start([spec()],direct=True)
        args=dict(execution_evidence=['executed'],acceptance_evidence=['accepted'],verifier_context_id='root')
        self.call('complete_direct',check_results=[result()],**args)
        for code in [False,0.0]:
            with self.assertRaisesRegex(StateError,'exit_code'):
                self.call('complete_direct',check_results=[result(code=code)],**args)

    def test_legacy_failure_history_is_seeded_from_event_order(self):
        self.start();self.call('fail',category='validation',evidence=['first'],host_stopped_evidence='stopped')
        state=self.store.status(self.owner,1)
        for key in ['check_history_initialized','check_failure_history']:
            state['tasks']['task'].pop(key,None)
        import json
        self.store.path.write_text(json.dumps(state),encoding='utf-8')
        self.call('retry',controller_reason='fix',human_authorized=False)
        self.p=prepare(self.store,self.owner,1)
        self.store.apply('bind',self.owner,1,attempt_id=self.p['attempt_id'],agent_id='second')
        self.store.apply('fail',self.owner,1,attempt_id=self.p['attempt_id'],agent_id='second',category='validation',evidence=['second'],host_stopped_evidence='stopped')
        self.call('retry',controller_reason='second fix',human_authorized=False)
        with self.assertRaisesRegex(StateError,'human authorization'):prepare(self.store,self.owner,1)

    def strip_classified_state(self):
        """Recreate fields absent in 3.4; original events and receipts remain."""
        import json
        state=self.store.status(self.owner,1)
        for row in list(state['tasks'].values())+list(state['attempts'].values()):
            for key in list(row):
                if key.startswith('check_') or key in ('resolved_check_failure','failure_observations'):
                    row.pop(key)
        self.store.path.write_text(json.dumps(state),encoding='utf-8')

    def test_formal_late_report_records_model_but_rejects_green_completion(self):
        self.start([spec(),spec('api')])
        self.call('record_check',check_results=[result(code=1)])
        with self.assertRaisesRegex(StateError,'new attributable failure'):
            self.report([result('api')])
        r=self.report([result('api'),result('api',None,'model')])
        self.assertEqual((r['status'],r['category']),('failed','model'))
        with self.assertRaisesRegex(StateError,'human-only'):
            self.call('retry',controller_reason='ordinary retry',human_authorized=False)

    def test_legacy_matching_pending_retry_reused_without_new_decision(self):
        self.start();self.call('fail',category='validation',evidence=['failed'],host_stopped_evidence='stopped')
        self.call('retry',controller_reason='retained decision',human_authorized=False)
        self.strip_classified_state()
        self.assertEqual(prepare(self.store,self.owner,1)['status'],'dispatch_pending')

    def test_legacy_model_pending_human_retry_reused(self):
        self.start();self.call('fail',category='model',evidence=['model refusal'],host_stopped_evidence='stopped')
        self.call('retry',controller_reason='explicit user retry',human_authorized=True)
        self.strip_classified_state()
        self.assertEqual(prepare(self.store,self.owner,1)['status'],'dispatch_pending')

    def test_legacy_consumed_retry_before_unspawned_successor_is_not_a_deadlock(self):
        self.start();self.call('fail',category='validation',evidence=['failed'],host_stopped_evidence='stopped')
        self.call('retry',controller_reason='repair',human_authorized=False)
        self.p=prepare(self.store,self.owner,1)
        self.call('reconcile',action='no_spawn',evidence=['host proves not spawned'])
        self.call('retry',controller_reason='safe no-spawn retry',human_authorized=False)
        self.strip_classified_state()
        self.assertEqual(prepare(self.store,self.owner,1)['status'],'dispatch_pending')

    def test_legacy_consumed_retry_before_stopped_successor_is_not_a_deadlock(self):
        self.start();self.call('fail',category='validation',evidence=['failed'],host_stopped_evidence='stopped')
        self.call('retry',controller_reason='repair',human_authorized=False)
        self.p=prepare(self.store,self.owner,1)
        self.store.apply('bind',self.owner,1,attempt_id=self.p['attempt_id'],agent_id='second')
        self.call('reconcile',action='stopped',evidence=['host confirms stopped without verdict'])
        self.call('retry',controller_reason='safe stopped retry',human_authorized=False)
        self.strip_classified_state()
        self.assertEqual(prepare(self.store,self.owner,1)['status'],'dispatch_pending')


if __name__=='__main__':unittest.main()
