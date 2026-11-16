"""Goal source contract through the original atomic switch API."""
import copy
import json
import unittest
import uuid
from datetime import datetime, timezone

from task_state import StateError
import test_main_switch as legacy
from main_switch import GOAL_VERIFICATION_ANSWER, GOAL_VERIFICATION_QUESTION, _grant, _user_authorizes


class GoalSwitchTests(unittest.TestCase):
    def test_goal_activation_is_not_a_message_grant(self):
        grant=dict(message_id='grant',answer=GOAL_VERIFICATION_ANSWER,purpose='goal_verification',max_sends=2,reviewed_through='grant')
        self.assertEqual(_grant(grant),grant)
        content='<send_user_message_question_reply>\n'+json.dumps([{'question':GOAL_VERIFICATION_QUESTION,'answer':GOAL_VERIFICATION_ANSWER}],ensure_ascii=False)+'\n</send_user_message_question_reply>'
        item=dict(type='UserMessage',client_id='real-ui',content=[dict(type='text',text=content)])
        self.assertTrue(_user_authorizes(item,grant))
        self.assertFalse(_user_authorizes(dict(item,client_id=None),grant))
        self.assertFalse(_user_authorizes(dict(item,content=[dict(type='text',text='允许创建Goal并切换模型')]),grant))
        self.assertFalse(_user_authorizes(dict(item,content=[dict(type='text',text=content.replace(GOAL_VERIFICATION_ANSWER,'暂不允许'))]),grant))
        with self.assertRaises(StateError):_grant(dict(grant,max_sends=3))

    def fixture(self, budget=1):
        f=legacy.MainSwitchTests();f.setUp();self.addCleanup(f.doCleanups)
        f.goal_root=f.old
        rows=[json.loads(line) for line in f.rollout.read_text(encoding='utf-8').splitlines()]
        for row in rows:
            p=row['payload']
            if row['type']=='event_msg' and p.get('type')=='task_started':
                p.update(root_turn_id=f.old,turn_attribution=dict(turn_id=f.old,turn_trigger='composer',
                    parent_turn_id=None,root_turn_id=f.old,initiating_agent_path=None))
            if budget==2 and p.get('item',{}).get('type')=='UserMessage':
                p['item']['content'][0]['text']='允许在本聊天为当前任务自动续接2次'
        f.rollout.write_text(''.join(json.dumps(x,ensure_ascii=False)+'\n' for x in rows),encoding='utf-8')
        f.goal=dict(threadId=f.thread,status='active',createdAt=12345,objective='Complete the same authorized task')
        f.authorization=dict(message_id='grant',answer=f'允许在本聊天为当前任务自动续接{budget}次',
                             purpose='task_continuation',max_sends=budget,reviewed_through='grant')
        return f

    def prepared(self,f):return f.prepare_switch(next_source='goal',goal_state=f.goal,authorization=f.authorization)

    def verification_fixture(self):
        f=self.fixture(2)
        f.current={'model':'gpt-6-astra','effort':'high'};f.item['current']=f.current
        f.item['features'].update(stage='planning',complexity='complex')
        f.call('update',task=f.item)
        rows=[json.loads(x) for x in f.rollout.read_text(encoding='utf-8').splitlines()]
        for x in rows:
            p=x['payload']
            if x['type']=='turn_context':p.update(f.current)
            if p.get('type')=='thread_settings_applied':p['thread_settings']={'model':f.current['model'],'reasoning_effort':f.current['effort']}
            if p.get('item',{}).get('type')=='UserMessage':
                p['item']['content'][0]['text']='<send_user_message_question_reply>'+json.dumps([
                    {'question':GOAL_VERIFICATION_QUESTION,'answer':GOAL_VERIFICATION_ANSWER}])+'</send_user_message_question_reply>'
        f.rollout.write_text(''.join(json.dumps(x)+'\n' for x in rows),encoding='utf-8')
        f.authorization=dict(message_id='grant',answer=GOAL_VERIFICATION_ANSWER,purpose='goal_verification',max_sends=2,reviewed_through='grant')
        return f

    def test_scoped_verification_round_trip(self):
        f=self.verification_fixture();r=self.stage(f);new=self.successor(f,r['target'])
        self.assertEqual(f.step('confirm',goal_state=f.goal)['status'],'confirmed')
        f.old=new;f.current=r['target'];f.item['current']=f.current
        f.item['features'].update(stage='strategy',complexity='normal');f.call('update',task=f.item)
        r=f.prepare_switch(next_source='goal',goal_state=f.goal,authorization=f.authorization,phase='return')
        self.claimed(f);f.apply_settings(r);f.accepted();self.successor(f,r['target'])
        self.assertEqual(f.step('confirm',goal_state=f.goal)['status'],'confirmed')

    def test_scoped_verification_rejects_wrong_first_target_and_effort(self):
        for features in [dict(stage='execution',deterministic=True,complexity='normal'),dict(stage='planning',complexity='normal')]:
            f=self.verification_fixture();f.item['features'].update(features);f.call('update',task=f.item)
            with self.assertRaises(StateError):self.prepared(f)

    def test_scoped_verification_return_requires_same_goal_and_target(self):
        for change in ('goal','target'):
            f=self.verification_fixture();r=self.stage(f);new=self.successor(f,r['target'])
            self.assertEqual(f.step('confirm',goal_state=f.goal)['status'],'confirmed')
            f.old=new;f.current=r['target'];f.item['current']=f.current
            f.item['features'].update(stage='strategy',complexity='normal')
            if change=='goal':f.goal['createdAt']+=1
            else:f.item['features'].update(stage='execution',deterministic=True)
            f.call('update',task=f.item)
            with self.assertRaises(StateError):f.prepare_switch(next_source='goal',goal_state=f.goal,authorization=f.authorization,phase='return')

    def test_scoped_verification_does_not_reuse_a_cancelled_claim(self):
        f=self.verification_fixture();self.prepared(f);self.claimed(f)
        f.step('cancel',send_permit=f.permit,outcome='explicit no send')
        f.old=self.successor(f,f.current)
        with self.assertRaises(StateError):f.prepare_switch(next_source='goal',goal_state=f.goal,authorization=f.authorization,phase='again')

    def test_settings_changed_and_restored_before_claim_or_check(self):
        for phase in ('claim','check'):
            f=self.fixture();self.prepared(f)
            if phase=='check':self.claimed(f)
            for setting in ({'model':'gpt-6.2-luna','effort':'medium'},f.current):
                f.write('event_msg',type='thread_settings_applied',thread_id=f.thread,
                        thread_settings={'model':setting['model'],'reasoning_effort':setting['effort']})
            with self.assertRaises(StateError):f.step(phase,goal_state=f.goal,send_permit=getattr(f,'permit',None))

    def test_settings_changed_and_restored_during_confirmation(self):
        for position in ('before_marker','after_marker','after_start'):
            f=self.fixture();r=self.prepared(f);self.claimed(f)
            def conflict():
                for setting in ({'model':'gpt-6.2-luna','effort':'medium'},r['target']):
                    f.write('event_msg',type='thread_settings_applied',thread_id=f.thread,
                            thread_settings={'model':setting['model'],'reasoning_effort':setting['effort']})
            if position=='before_marker':conflict()
            f.apply_settings(r);f.accepted()
            if position=='after_marker':conflict()
            self.successor(f,r['target'])
            if position=='after_start':conflict()
            self.assertEqual(f.step('confirm',goal_state=f.goal)['status'],'mismatch')
    def claimed(self,f):
        v=f.step('claim',goal_state=f.goal);f.permit=v['send_permit'];return v
    def stage(self,f):
        r=self.prepared(f);self.claimed(f);f.apply_settings(r);f.accepted()
        self.assertEqual(f.step('confirm',goal_state=f.goal)['status'],'staged');return r
    def successor(self,f,settings,patch=None,context=True,work=True):
        f.event('task_complete',f.old);new=str(uuid.uuid4())
        attr=dict(turn_id=new,turn_trigger='goal',parent_turn_id=f.old,root_turn_id=f.goal_root,initiating_agent_path=None)
        attr.update(patch or {})
        f.event('task_started',new,root_turn_id=f.goal_root,turn_attribution=attr)
        if context:f.write('turn_context',turn_id=new,**settings)
        if work:f.event('item_completed',new,item={'type':'Reasoning','id':'work'})
        return new

    def test_round_trip_preserves_goal_ancestor_with_its_prior_marker(self):
        f=self.fixture(2);r=self.stage(f);new=self.successor(f,r['target'])
        a=f.step('confirm',goal_state=f.goal);self.assertEqual(a['status'],'confirmed')
        self.assertEqual(a['host_evidence']['continuation_source'],'goal')
        f.old=new;f.current=r['target'];f.item['current']=f.current
        f.item['features'].update(stage='planning',complexity='complex')
        f.call('update',task=f.item)
        r=f.prepare_switch(next_source='goal',goal_state=f.goal,authorization=f.authorization,phase='return-planning')
        self.claimed(f);f.apply_settings(r);f.accepted();self.successor(f,r['target'])
        self.assertEqual(f.step('confirm',goal_state=f.goal)['status'],'confirmed')

    def test_first_context_and_work_can_arrive_late(self):
        f=self.fixture();r=self.stage(f);new=self.successor(f,r['target'],context=False,work=False)
        self.assertEqual(f.step('confirm',goal_state=f.goal)['status'],'staged')
        f.write('turn_context',turn_id=new,**r['target'])
        f.event('item_completed',new,item={'type':'ContextCompaction','id':'compact'})
        self.assertEqual(f.step('confirm',goal_state=f.goal)['status'],'staged')
        f.event('item_completed',new,item={'type':'Reasoning','id':'work'})
        self.assertEqual(f.step('confirm',goal_state=f.goal)['status'],'confirmed')

    def test_wrong_attribution_does_not_skip_to_a_later_good_turn(self):
        for change in [dict(parent_turn_id=str(uuid.uuid4())),dict(root_turn_id=str(uuid.uuid4())),
                       dict(turn_id=str(uuid.uuid4())),dict(initiating_agent_path='/root/child'),
                       dict(turn_trigger='composer')]:
            with self.subTest(change=change):
                f=self.fixture();r=self.stage(f);self.successor(f,r['target'],change)
                self.assertEqual(f.step('confirm',goal_state=f.goal)['status'],'mismatch')
                self.assertEqual(f.step('confirm',goal_state=f.goal)['status'],'mismatch')

    def test_missing_explicit_null_agent_path_rejected(self):
        f=self.fixture();r=self.stage(f);self.successor(f,r['target'])
        rows=[json.loads(x) for x in f.rollout.read_text(encoding='utf-8').splitlines()]
        for x in rows:
            if x['payload'].get('type')=='task_started' and x['payload']['turn_id']!=f.old:
                x['payload']['turn_attribution'].pop('initiating_agent_path')
        f.rollout.write_text(''.join(json.dumps(x)+'\n' for x in rows),encoding='utf-8')
        self.assertEqual(f.step('confirm',goal_state=f.goal)['status'],'mismatch')

    def test_human_or_native_input_preempts_goal_acceptance(self):
        for human in (True,False):
            f=self.fixture();r=self.stage(f);new=self.successor(f,r['target'])
            if human:f.human(new,'User intervened','intervention')
            else:f.native_input(new,'unrelated external input')
            self.assertEqual(f.step('confirm',goal_state=f.goal)['status'],'mismatch')

    def test_interrupted_parent_or_successor_rejected(self):
        for which in ('parent','child'):
            f=self.fixture();r=self.stage(f)
            if which=='parent':f.event('turn_aborted',f.old)
            new=self.successor(f,r['target'])
            if which=='child':f.event('turn_aborted',new)
            self.assertEqual(f.step('confirm',goal_state=f.goal)['status'],'mismatch')

    def test_goal_pause_identity_or_objective_change_before_claim(self):
        for change in [dict(status='paused'),dict(createdAt=999),dict(threadId=str(uuid.uuid4())),dict(objective='other')]:
            f=self.fixture();self.prepared(f)
            with self.assertRaises(StateError):f.step('claim',goal_state=dict(f.goal,**change))
            self.assertEqual(f.step('get')['status'],'prepared')

    def test_goal_must_remain_same_at_check_and_confirmation(self):
        f=self.fixture();r=self.prepared(f);self.claimed(f)
        with self.assertRaises(StateError):f.step('check',send_permit=f.permit,goal_state=dict(f.goal,status='paused'))
        f.apply_settings(r);f.accepted();self.successor(f,r['target'])
        with self.assertRaises(StateError):f.step('confirm',goal_state=dict(f.goal,objective='other'))
        self.assertEqual(f.step('get')['status'],'accepted')
        self.assertEqual(f.step('confirm',goal_state=f.goal)['status'],'confirmed')

    def test_new_input_even_with_reused_id_blocks_last_check(self):
        f=self.fixture();self.prepared(f);self.claimed(f)
        f.human(f.old,'changed instruction','grant')
        with self.assertRaises(StateError):f.step('check',send_permit=f.permit,goal_state=f.goal)

    def test_actual_model_effort_mismatch_or_unrequested_settings_freezes(self):
        for field,value in [('model','gpt-6.2-luna'),('effort','max')]:
            f=self.fixture();r=self.stage(f);self.successor(f,dict(r['target'],**{field:value}))
            self.assertEqual(f.step('confirm',goal_state=f.goal)['status'],'mismatch')
        f=self.fixture();r=self.stage(f)
        f.write('event_msg',type='thread_settings_applied',thread_id=f.thread,
                thread_settings={'model':'gpt-6.2-luna','reasoning_effort':'medium'})
        self.successor(f,{'model':'gpt-6.2-luna','effort':'medium'})
        self.assertEqual(f.step('confirm',goal_state=f.goal)['status'],'mismatch')

    def test_default_human_contract_still_rejects_goal(self):
        f=self.fixture();r=f.prepare_switch();f.claim();f.apply_settings(r);f.accepted();self.successor(f,r['target'])
        self.assertEqual(f.step('confirm')['status'],'mismatch')

    def test_equal_timestamps_use_event_order(self):
        f=self.fixture();r=self.prepared(f);self.claimed(f)
        f._event_time=datetime.now(timezone.utc)
        f.apply_settings(r);f.accepted();self.successor(f,r['target'])
        self.assertEqual(f.step('confirm',goal_state=f.goal)['status'],'confirmed')
        f=self.fixture();r=self.stage(f);self.successor(f,r['target'])
        rows=[json.loads(x) for x in f.rollout.read_text(encoding='utf-8').splitlines()]
        end=next(i for i,x in enumerate(rows) if x['payload'].get('type')=='task_complete')
        rows[end],rows[end+1]=rows[end+1],rows[end]
        f.rollout.write_text(''.join(json.dumps(x)+'\n' for x in rows),encoding='utf-8')
        self.assertEqual(f.step('confirm',goal_state=f.goal)['status'],'mismatch')

    def test_unknown_send_is_never_reinterpreted_as_success(self):
        f=self.fixture();r=self.prepared(f);self.claimed(f);f.apply_settings(r)
        f.step('receipt',outcome='unknown',send_permit=f.permit);self.successor(f,r['target'])
        self.assertEqual(f.step('confirm',goal_state=f.goal)['status'],'unknown')

if __name__=='__main__':unittest.main()
