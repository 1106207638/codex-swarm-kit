import copy
import hashlib
import json
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

from task_state import Store, StateError
from test_task_state import make_task, prepare


class OutlineStateTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix='outline-router-'))
        self.store = Store(self.root/'state'); self.owner = 'controller'
        self.store.init(self.owner)
        self.plan = self.root/'plan.md'; self.plan.write_text('Approved two-module task', encoding='utf-8')
        self.spec = {'ref': str(self.plan), 'revision': 'v1',
                     'sha256': hashlib.sha256(self.plan.read_bytes()).hexdigest(), 'source': 'user approved outline',
                     'controller_pins': {}, 'override': None,
                     'delegation': {'authorized': True, 'prohibited': False, 'source': 'user'},
                     'modules': [self.module('parse'), self.module('cli')], 'final_acceptance_ids': ['integrated']}

    def module(self, mid):
        return {'id': mid, 'requirement_ids': [mid+'-r'], 'acceptance_ids': [mid+'-a'],
                'work': [{'task_id': mid, 'operation': 'business', 'stage': 'execution',
                          'requirement_ids': [mid+'-r'], 'acceptance_ids': [mid+'-a']},
                         {'task_id': mid+'-review', 'operation': 'business', 'stage': 'review',
                          'requirement_ids': [mid+'-r'], 'acceptance_ids': [mid+'-a']}]}

    def call(self, op, **kw):
        if op in ('report','complete_direct') and 'handoff' not in kw:
            s=self.store.status(self.owner,1)
            t=(s['attempts'][kw['attempt_id']]['task_snapshot'] if op=='report'
               else s['tasks'][kw['task_id']]['versions'][-1]['task'])
            if t.get('mode')=='outline':kw['handoff']=self.handoff(t)
        return self.store.apply(op, self.owner, 1, **kw)

    def handoff(self,t):
        return {'artifacts':{p:hashlib.sha256(Path(p).read_bytes()).hexdigest() for p in t['artifacts']},
                'checks':[{'command':'synthetic unit fixture','exit_code':0,'expected_exit_code':0,'evidence_ref':'unit assertion'}],
                'remaining':[],'risks':[],'scope_evidence':['unit test fixture only']}

    def register(self): return self.call('outline_register', outline=self.spec)

    def item(self, mid, review=False):
        t = make_task(self.root, mid+'-review' if review else mid, stage='review' if review else 'execution',
                      deps=[mid] if review else [], path=self.root/mid/'output.py')
        t.update(mode='outline', operation='business', outline_ref=str(self.plan), outline_revision=self.spec['revision'],
                 module_id=mid, requirement_ids=[mid+'-r'], acceptance_ids=[mid+'-a'], delegation=self.spec['delegation'])
        path=self.root/mid/'output.py';path.parent.mkdir(exist_ok=True)
        if not path.exists():path.write_text('synthetic test artifact',encoding='utf-8')
        t.update(resources=[],read_paths=[str(path)],artifacts=[] if review else [str(path)],
                 snapshot_files={str(path):hashlib.sha256(path.read_bytes()).hexdigest()} if review else {})
        return t

    def run_item(self, t):
        self.call('add', task=t)
        r = prepare(self.store, self.owner, 1, t['task_id'])
        self.call('bind', attempt_id=r['attempt_id'], agent_id=t['task_id']+'-agent')
        self.call('report', attempt_id=r['attempt_id'], agent_id=t['task_id']+'-agent', input_digest=r['input_digest'],
                  summary='synthetic unit test', evidence=['unit fixture'], host_stopped_evidence='synthetic stopped')
        self.call('verify', attempt_id=r['attempt_id'], input_digest=r['input_digest'],
                  verifier_context_id=self.owner, evidence=['unit fixture acceptance'])
        return r

    def accept(self, mid):
        return self.call('outline_accept', module_id=mid, acceptance_evidence={mid+'-a': ['runtime check']},
                         runtime_evidence=['executed acceptance'], verifier_context_id=self.owner)

    def finish_module(self, mid):
        self.run_item(self.item(mid)); self.run_item(self.item(mid, True)); return self.accept(mid)

    def test_explicit_registration_only_and_old_state_readable(self):
        self.assertEqual(self.store.status(self.owner, 1)['schema_version'], 1)
        self.register()
        s = Store(self.root/'state').status(self.owner, 1)
        self.assertEqual(s['schema_version'], 2)
        self.assertEqual(s['outline']['cursor'], 0)
        with self.assertRaises(StateError): self.register()

    def test_future_module_and_missing_or_wrong_binding_blocked(self):
        self.register()
        for patch in ({'mode': 'normal'}, {'outline_ref': 'another'}, {'outline_revision': 'v0'},
                      {'requirement_ids': []}, {'task_id': 'rename-to-reset-budget'}):
            t = self.item('parse'); t.update(patch)
            with self.assertRaises(StateError): self.call('add', task=t)
        self.call('add', task=self.item('cli'))
        with self.assertRaisesRegex(StateError, 'current module'): prepare(self.store, self.owner, 1, 'cli')
        with self.assertRaises(StateError): self.call('add', task=make_task(self.root, 'unrelated'))

    def test_review_cannot_skip_executor_dependency(self):
        self.register(); t=self.item('parse', True); t['depends_on']=[]
        with self.assertRaisesRegex(StateError, 'review dependencies'): self.call('add', task=t)

    def test_module_gate_requires_all_work_review_and_runtime_evidence(self):
        self.register(); self.run_item(self.item('parse'))
        with self.assertRaises(StateError): self.accept('parse')
        self.run_item(self.item('parse', True))
        with self.assertRaises(StateError): self.call('outline_accept', module_id='parse', acceptance_evidence={}, runtime_evidence=['x'], verifier_context_id=self.owner)
        self.assertEqual(self.accept('parse')['next_module'], 'cli')
        self.assertEqual(self.finish_module('cli')['next_module'], None)
        with self.assertRaises(StateError): self.call('outline_complete', evidence={}, verifier_context_id=self.owner)
        self.call('outline_complete', evidence={'integrated': ['actual integration result']}, verifier_context_id=self.owner)
        self.assertTrue(self.store.status(self.owner, 1)['outline']['completion'])

    def test_input_update_reopens_module_and_invalidates_later_acceptance(self):
        self.register(); self.finish_module('parse'); self.finish_module('cli')
        self.call('outline_complete', evidence={'integrated': ['check']}, verifier_context_id=self.owner)
        t=self.item('parse'); t['input']='changed behavior'; self.call('update', task=t)
        s=self.store.status(self.owner, 1)
        self.assertEqual(s['outline']['cursor'], 0); self.assertIsNone(s['outline']['completion'])
        self.assertFalse(s['tasks']['cli']['verified']); self.assertFalse(s['tasks']['parse-review']['verified'])
        self.assertEqual(len(s['attempts']), 4)
        with self.assertRaises(StateError): prepare(self.store, self.owner, 1, 'cli')

    def test_plan_edit_requires_revision_and_old_input_stays_invalid(self):
        self.register(); self.finish_module('parse')
        self.plan.write_text('Revised approved outline', encoding='utf-8')
        with self.assertRaisesRegex(StateError, 'plan content'): self.call('add', task=self.item('cli'))
        self.spec['revision']='v2'; self.spec['sha256']=hashlib.sha256(self.plan.read_bytes()).hexdigest()
        self.call('outline_revise', outline=self.spec, from_module='cli', reason='user changed CLI requirement')
        s=self.store.status(self.owner, 1)
        self.assertEqual(s['outline']['cursor'], 1); self.assertTrue(s['tasks']['parse']['verified'])
        old=self.item('cli'); old['outline_revision']='v1'
        with self.assertRaises(StateError): self.call('add', task=old)
        self.call('add', task=self.item('cli'))

    def test_active_or_unknown_work_cannot_be_revised_or_advanced(self):
        self.register(); self.call('add', task=self.item('parse')); prepare(self.store,self.owner,1,'parse')
        new=copy.deepcopy(self.spec); new['revision']='v2'
        with self.assertRaises(StateError): self.call('outline_revise', outline=new, from_module='parse',reason='change')
        with self.assertRaises(StateError): self.accept('parse')

    def test_task_cannot_drop_registered_user_restrictions(self):
        self.spec['controller_pins']={'model':'gpt-6-astra','source':'user'}; self.register()
        with self.assertRaisesRegex(StateError, 'controller_pins'): self.call('add', task=self.item('parse'))

    def test_packet_contains_only_current_module_reference(self):
        self.register(); self.call('add', task=self.item('parse'))
        r=prepare(self.store,self.owner,1,'parse'); body=json.loads(r['spawn_args']['message'].split('\n',1)[1])
        self.assertEqual(body['module_id'],'parse'); self.assertEqual(body['outline_revision'],'v1')
        self.assertNotIn('cli-review',r['spawn_args']['message'])

    def test_same_review_context_as_executor_is_rejected(self):
        self.register(); self.run_item(self.item('parse')); self.call('add',task=self.item('parse',True))
        r=prepare(self.store,self.owner,1,'parse-review')
        with self.assertRaises(StateError):self.call('bind',attempt_id=r['attempt_id'],agent_id='parse-agent')

    def test_update_cannot_invalidate_active_next_module(self):
        self.register();self.finish_module('parse');self.call('add',task=self.item('cli'))
        prepare(self.store,self.owner,1,'cli')
        t=self.item('parse');t['input']='new'
        with self.assertRaises(StateError):self.call('update',task=t)
        self.assertEqual(self.store.status(self.owner,1)['outline']['cursor'],1)

    def test_revision_preserves_task_budget_identity_and_untouched_work(self):
        self.register();self.finish_module('parse')
        revision=copy.deepcopy(self.spec);revision['revision']='v2'
        revision['modules'][0]['work'][0]['task_id']='new-parse'
        with self.assertRaises(StateError):self.call('outline_revise',outline=revision,from_module='parse',reason='reset')
        self.assertTrue(self.store.status(self.owner,1)['tasks']['parse']['verified'])

    def test_version_cannot_be_recycled_after_intervening_revision(self):
        self.register(); new=copy.deepcopy(self.spec);new['revision']='v2'
        self.call('outline_revise',outline=new,from_module='parse',reason='change')
        with self.assertRaisesRegex(StateError,'reused'):
            self.call('outline_revise',outline=self.spec,from_module='parse',reason='restore old version label')

    def test_execution_waits_for_resolved_plan(self):
        self.register();t=self.item('parse');t['features']['plan_ready']=False;self.call('add',task=t)
        with self.assertRaisesRegex(StateError,'resolved plan'):prepare(self.store,self.owner,1,'parse')
        self.assertFalse(self.store.status(self.owner,1)['attempts'])

    def test_stopped_unverified_legacy_receipt_must_be_closed_before_registration(self):
        t=make_task(self.root,'legacy');self.call('add',task=t);r=prepare(self.store,self.owner,1,'legacy')
        self.call('bind',attempt_id=r['attempt_id'],agent_id='legacy-agent')
        self.call('report',attempt_id=r['attempt_id'],agent_id='legacy-agent',input_digest=r['input_digest'],
                  summary='legacy result',evidence=['check'],host_stopped_evidence='stopped')
        with self.assertRaisesRegex(StateError,'unclosed'):self.register()
        self.assertEqual(self.store.status(self.owner,1)['schema_version'],1)
        self.call('verify',attempt_id=r['attempt_id'],input_digest=r['input_digest'],verifier_context_id=self.owner,evidence=['checked'])
        self.register()

    def test_actual_review_receipt_must_not_be_controller_decision(self):
        self.spec['delegation']['prohibited']=True;self.register()
        def direct(t,decision=None,ack=False):
            self.call('add',task=t)
            def perform():
                r=prepare(self.store,self.owner,1,t['task_id'])
                self.call('complete_direct',task_id=t['task_id'],direct_id=r['direct_id'],input_digest=r['input_digest'],
                          execution_evidence=['performed'],acceptance_evidence=['checked'],
                          verifier_context_id=self.owner,limitation_acknowledged=ack)
            if decision:
                # Retain a receipt generated by the prior policy, to exercise
                # acceptance validation independently of the new prepare gate.
                with patch('router_policy.decide',return_value=decision):perform()
            else:perform()
        direct(self.item('parse'))
        fake={'action':'direct','role':'astra_controller','model':'gpt-6-astra','effort':'high',
              'independence_required':False,'reason_codes':['controller_required'],'limitations':[]}
        direct(self.item('parse',True),fake)
        with self.assertRaisesRegex(StateError,'review receipt'):self.accept('parse')

    def test_prohibited_delegation_review_keeps_explicit_limitation(self):
        self.spec['delegation']['prohibited']=True;self.register()
        for review in (False,True):
            t=self.item('parse',review);self.call('add',task=t);r=prepare(self.store,self.owner,1,t['task_id'])
            self.call('complete_direct',task_id=t['task_id'],direct_id=r['direct_id'],input_digest=r['input_digest'],
                      execution_evidence=['performed'],acceptance_evidence=['checked'],
                      verifier_context_id=self.owner,limitation_acknowledged=review)
        self.assertEqual(self.accept('parse')['next_module'],'cli')

    def test_legacy_planner_receipt_is_not_execution_acceptance(self):
        from router_policy import decide
        from test_task_state import CATALOG
        self.register();planning=make_task(self.root,stage='planning')
        with patch('router_policy.decide',return_value=decide(planning,CATALOG)):
            self.run_item(self.item('parse'))
        self.run_item(self.item('parse',True))
        with self.assertRaisesRegex(StateError,'execution receipt'):self.accept('parse')

    def test_future_module_retry_authorization_stays_blocked(self):
        self.register();self.finish_module('parse');self.call('add',task=self.item('cli'))
        r=prepare(self.store,self.owner,1,'cli');self.call('bind',attempt_id=r['attempt_id'],agent_id='cli-agent')
        self.call('fail',attempt_id=r['attempt_id'],agent_id='cli-agent',category='environment',evidence=['failure'],host_stopped_evidence='stopped')
        t=self.item('parse');t['input']='changed';self.call('update',task=t)
        with self.assertRaisesRegex(StateError,'current module'):
            self.call('retry',attempt_id=r['attempt_id'],controller_reason='environment repaired',human_authorized=False)

    def test_authorized_completed_legacy_direct_recovery_allows_registration(self):
        t=make_task(self.root,'legacy');self.call('add',task=t);r=prepare(self.store,self.owner,1,'legacy')
        self.call('fail',attempt_id=r['attempt_id'],category='model',evidence=['model unavailable'],
                  host_no_spawn_evidence='native rejected before creation')
        with self.assertRaisesRegex(StateError,'model failures'):self.register()
        self.call('retry',attempt_id=r['attempt_id'],controller_reason='user authorized deterministic direct recovery',human_authorized=True)
        t['features'].update(risk='low',deterministic=True,complexity='simple');self.call('update',task=t)
        d=prepare(self.store,self.owner,1,'legacy')
        self.call('complete_direct',task_id='legacy',direct_id=d['direct_id'],input_digest=d['input_digest'],
                  execution_evidence=['recovered'],acceptance_evidence=['checked'],verifier_context_id=self.owner)
        self.register()


if __name__ == '__main__': unittest.main()
