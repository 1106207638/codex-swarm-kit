"""Outline gates in the existing atomic ledger, never a second scheduler.

The caller authenticates user authority and evidence. These checks coordinate
registered actions only; they are not an OS sandbox or a semantic plan judge.
"""
import copy
import hashlib
import re
from pathlib import Path

from task_state import StateError, ACTIVE, _need, _nonempty, _evidence, _now
from router_policy import validate_pins, PolicyError


def strings(value, name):
    _evidence(value, name)
    _need(len(value) == len(set(value)), name + ' must be unique')
    return set(value)


def validate_spec(spec):
    _need(isinstance(spec, dict) and set(spec) == {'ref', 'revision', 'sha256', 'source', 'modules',
          'controller_pins', 'override', 'delegation', 'final_acceptance_ids'}, 'invalid outline registration contract')
    for k in ('ref', 'revision', 'source'): _nonempty(spec[k], 'outline.'+k)
    _need(Path(spec['ref']).is_absolute(), 'outline ref must be absolute')
    _need(isinstance(spec['sha256'], str) and re.fullmatch('[0-9a-f]{64}', spec['sha256']), 'outline sha256 invalid')
    try: validate_pins(spec['controller_pins'])
    except PolicyError as exc: raise StateError(str(exc)) from exc
    d=spec['delegation']
    _need(isinstance(d,dict) and set(d)=={'authorized','prohibited','source'} and
          type(d['authorized']) is bool and type(d['prohibited']) is bool, 'outline delegation invalid')
    _nonempty(d['source'], 'outline delegation source')
    whole=spec['override']
    if whole is not None:
        _need(isinstance(whole,dict) and {'scope','model','source'} <= set(whole) <= {'scope','model','source','effort'} and
              whole['scope']=='all_tasks', 'outline override requires explicit whole-task scope')
        for k in ('model','source'): _nonempty(whole[k], 'override.'+k)
        if 'effort' in whole: _nonempty(whole['effort'], 'override.effort')
        for k in ('model','effort'):
            _need(k not in whole or k not in spec['controller_pins'] or whole[k]==spec['controller_pins'][k],
                  'outline has conflicting explicit model restrictions')
    modules=spec['modules']; _need(isinstance(modules,list) and modules, 'outline needs substantive modules')
    ids=set(); tasks=set()
    for m in modules:
        _need(isinstance(m,dict) and set(m)=={'id','requirement_ids','acceptance_ids','work'}, 'invalid module contract')
        mid=_nonempty(m['id'],'module id'); _need(mid not in ids,'duplicate module');ids.add(mid)
        req=strings(m['requirement_ids'],'module requirements'); acc=strings(m['acceptance_ids'],'module acceptance')
        _need(isinstance(m['work'],list) and m['work'],'module work required')
        covered_req=set();covered_acc=set(); reviews=[]
        for w in m['work']:
            _need(isinstance(w,dict) and set(w)=={'task_id','operation','stage','requirement_ids','acceptance_ids'}, 'invalid work binding')
            tid=_nonempty(w['task_id'],'work task_id');_need(tid not in tasks,'duplicate work identity'); tasks.add(tid)
            _need(w['operation'] in ('business','controller') and w['stage'] in ('strategy','planning','execution','review','research'), 'invalid work role')
            _need(w['operation']!='controller' or w['stage']=='strategy', 'controller work must be strategy')
            wr=strings(w['requirement_ids'],'work requirements');wa=strings(w['acceptance_ids'],'work acceptance')
            _need(wr <= req and wa <= acc,'work references unknown requirements or acceptance')
            covered_req.update(wr);covered_acc.update(wa)
            if w['stage']=='review': reviews.append(tid)
        _need(covered_req==req and covered_acc==acc,'module coverage incomplete')
        _need(reviews,'each outline module requires a review work item; explicit user prohibition uses existing direct-review exception')
    strings(spec['final_acceptance_ids'],'final acceptance ids')


def check_plan(spec):
    p=Path(spec['ref'])
    _need(p.is_file() and hashlib.sha256(p.read_bytes()).hexdigest()==spec['sha256'],
          'plan content changed or missing; register a reviewed outline revision')


def validate_state(state):
    o=state.get('outline')
    if o is None:
        _need(state['schema_version']==1,'schema 2 requires outline state'); return
    _need(state['schema_version']==2,'outline requires schema 2')
    validate_spec(o['spec'])
    n=len(o['spec']['modules'])
    _need(type(o['cursor']) is int and 0<=o['cursor']<=n and len(o['modules'])==n,'invalid outline cursor')
    _need(isinstance(o['history'],list),'invalid outline history')
    for i,m in enumerate(o['modules']):
        _need(m['id']==o['spec']['modules'][i]['id'] and type(m['generation']) is int and m['generation']>0,'invalid module state')
        _need((m['acceptance'] is not None)==(i<o['cursor']),'module acceptance/cursor mismatch')
    _need(o['completion'] is None or o['cursor']==n,'completion requires all modules')


def find_work(o, task_id):
    for i,m in enumerate(o['spec']['modules']):
        for w in m['work']:
            if w['task_id']==task_id:return i,m,w
    raise StateError('task is not a registered outline work item; no alternate task identity')


def binding(o, task):
    spec=o['spec'];i,m,w=find_work(o,task['task_id'])
    _need(task.get('mode')=='outline','registered outline work cannot omit mode')
    for k,v in [('outline_ref',spec['ref']),('outline_revision',spec['revision']),('module_id',m['id']),
                ('operation',w['operation']),('requirement_ids',w['requirement_ids']),('acceptance_ids',w['acceptance_ids'])]:
        _need(task.get(k)==v,'outline binding mismatch: '+k)
    _need(task['features']['stage']==w['stage'],'work stage differs from registered role')
    for k,default in [('controller_pins',{}),('override',None),('delegation',{})]:
        _need(task.get(k,default)==spec[k],'registered restriction mismatch: '+k)
    if w['stage']=='review':
        required={x['task_id'] for x in m['work'] if x['stage']!='review'}
        _need(required <= set(task.get('depends_on',[])), 'review dependencies must include all module non-review work')
    # Edges cannot point into a later module even if it once passed.
    for dep in task.get('depends_on',[]):
        di,_,_=find_work(o,dep);_need(di<=i,'dependency points into a later module')
    return i


def idle(store,state):
    _need(not store._active(state) and not any(t.get('direct_pending') for t in state['tasks'].values()),
          'outline transition requires all actions stopped, including unknown dispatches')
    from main_switch import pending_switch
    _need(pending_switch(state) is None,'resolve main-thread switch before outline transition')


def invalidate(store,state,start,reason):
    o=state['outline']; affected={w['task_id'] for m in o['spec']['modules'][start:] for w in m['work']}
    for tid in affected:
        r=state['tasks'].get(tid)
        if r:
            _need(not store._active_for_task(state,tid) and r.get('direct_pending') is None,
                  'cannot invalidate outline while affected work may still be active')
    o['history'].append({'at':_now(),'reason':reason,'cursor':o['cursor'],
                         'modules':copy.deepcopy(o['modules']),'completion':copy.deepcopy(o['completion'])})
    for m in o['modules'][start:]:m['acceptance']=None;m['generation']+=1
    for tid in affected:
        if tid in state['tasks']:
            state['tasks'][tid]['verified']=False
            state['tasks'][tid]['validation_generation']+=1
    o['completion']=None;o['cursor']=min(o['cursor'],start)


def guard(store,state,op,p):
    o=state.get('outline')
    if not o:
        if op in ('add','update') and isinstance(p.get('task'),dict):
            _need(p['task'].get('mode')!='outline','outline must be registered before adding work')
        elif op in ('prepare','complete_direct','verify','retry','main_switch_prepare'):
            tid=p.get('task_id')
            if op in ('verify','retry') and 'direct_id' not in p:tid=state['attempts'].get(p.get('attempt_id'),{}).get('task_id')
            r=state['tasks'].get(tid)
            _need(not r or r['versions'][-1]['task'].get('mode')!='outline',
                  'outline work requires explicit registration, including records written by older tools')
        return
    if op in ('add','update'):
        check_plan(o['spec']);i=binding(o,p['task'])
        artifact_paths={str(Path(x).resolve()).casefold() for x in p['task'].get('artifacts',[])}
        for w in o['spec']['modules'][i]['work']:
            other=state['tasks'].get(w['task_id'])
            if other and w['task_id']!=p['task']['task_id']:
                other_paths={str(Path(x).resolve()).casefold() for x in other['versions'][-1]['task'].get('artifacts',[])}
                _need(not artifact_paths & other_paths,'each module artifact has one executor')
        if op=='update':invalidate(store,state,i,'task input update: '+p['task']['task_id'])
    elif op in ('prepare','complete_direct','verify','retry') or op=='main_switch_prepare':
        check_plan(o['spec'])
        tid=p.get('task_id')
        if op in ('verify','retry') and 'direct_id' not in p:tid=state['attempts'].get(p.get('attempt_id'),{}).get('task_id')
        record=state['tasks'].get(tid);_need(record is not None,'unknown outline task')
        # A replay cannot advance anything: the original receipt validation
        # still checks versions, generations, identity and identical evidence.
        if record['verified'] and (op=='complete_direct' or
                (op=='verify' and state['attempts'][p['attempt_id']]['status']=='verified')):
            return
        task=record['versions'][-1]['task'];i=binding(o,task)
        _need(i==o['cursor'],'only the current module may execute or be accepted')
        if op=='prepare':
            f=task['features']
            if f['stage']=='execution':
                _need(f['plan_ready'] and f['ambiguity']=='clear', 'execution work requires a resolved plan; register planning separately')
            if f['stage']=='review':
                _need(f['ambiguity']!='conflict','resolve controller conflict before independent review')


def receipt_for(state, tid):
    r=state['tasks'][tid]
    receipts=[(a['decision'], False, a) for a in state['attempts'].values()
              if a['task_id']==tid and a['status']=='verified' and a['task_version']==r['current_version'] and
              a['validation_generation']==r['validation_generation']]
    receipts.extend((d['decision'], True, d) for d in r['direct_completions']
                    if d['task_version']==r['current_version'] and d['validation_generation']==r['validation_generation'])
    _need(len(receipts)==1,'work needs exactly one current verified receipt')
    return receipts[0]


def check_receipt(state, work):
    tid=work['task_id'];decision,direct,receipt=receipt_for(state,tid)
    task=state['tasks'][tid]['versions'][-1]['task']
    from work_evidence import accepted_handoff
    accepted_handoff(task,receipt,direct)
    if work['stage']=='review':
        _need(decision['role']=='sol_architect' and decision['independence_required'],
              'review receipt must represent independent review, not a controller decision')
        if direct:
            _need(task['delegation']['prohibited'] and receipt['independence_limitation_acknowledged'],
                  'direct review receipt requires explicit prohibition and acknowledged limitation')
    if work['stage']=='execution':
        allowed={'luna_worker','luna_general','luna_deep','direct_tool','local_worker'}
        if direct and task['delegation']['prohibited']:allowed.add('astra_controller')
        _need(decision['role'] in allowed and task['features']['plan_ready'] and task['features']['ambiguity']=='clear',
              'execution receipt cannot be replaced with planning or unresolved decisions')


def evidence_map(value, required):
    _need(isinstance(value,dict) and set(value)==set(required),'acceptance evidence must cover exactly all required ids')
    for k,v in value.items():_evidence(v,'evidence for '+k)
    return copy.deepcopy(value)


def work_status(store,state,tid,current):
    r=state['tasks'].get(tid)
    if not r:return 'not_added'
    if r['verified']:return 'verified'
    if store._active_for_task(state,tid) or r.get('direct_pending'):return 'active_or_unknown'
    from check_protocol import retry_gate, initialize_history
    initialize_history(state,tid)
    try: retry_gate(r)
    except StateError as exc:
        return 'human_decision_required' if 'human' in str(exc) else 'retry_decision_required'
    attempts=sorted((a for a in state['attempts'].values() if a['task_id']==tid),key=lambda a:a['attempt_number'])
    if attempts and attempts[-1]['status']=='reported':return 'awaiting_acceptance'
    auth=r.get('retry_authorization') or {}
    unresolved=r.get('model_failure_attempt_id') != r.get('resolved_model_failure_attempt_id')
    if unresolved and not (auth.get('human_authorized') and auth.get('previous_attempt_id')==r.get('model_failure_attempt_id')):
        return 'human_decision_required'
    from task_state import MAX_ATTEMPTS
    if len(attempts)>=MAX_ATTEMPTS:return 'attempt_budget_exhausted'
    if attempts and attempts[-1]['status']!='verified' and auth.get('previous_attempt_id')!=attempts[-1]['attempt_id']:
        return 'retry_decision_required'
    if not current:return 'module_queued'
    if any(not state['tasks'][d]['verified'] for d in r['versions'][-1]['task'].get('depends_on',[])):
        return 'dependencies_pending'
    return 'ready_for_prepare'


def transition(store,state,op,owner,p):
    if op=='outline_status':
        o=state.get('outline');_need(o is not None,'no outline registered')
        try:check_plan(o['spec']);plan_current=True
        except (StateError,OSError):plan_current=False
        modules=[]
        for i,m in enumerate(o['spec']['modules']):
            works=[]
            for w in m['work']:
                status=work_status(store,state,w['task_id'],i==o['cursor'])
                works.append({'task_id':w['task_id'],'stage':w['stage'],'status':status})
            modules.append({'module_id':m['id'],'status':'accepted' if i<o['cursor'] else ('current' if i==o['cursor'] else 'queued'),'work':works})
        return {'revision':o['spec']['revision'],'plan_current':plan_current,'modules':modules,'complete':o['completion'] is not None,
                'usage':'unknown','scope':'this ledger only; no OS isolation or native slot inventory'}
    if op=='outline_register':
        _need(state.get('outline') is None,'outline already registered; use revision without resetting task identities')
        idle(store,state);spec=copy.deepcopy(p.get('outline'));validate_spec(spec);check_plan(spec)
        _need(not any(a['status'] in ACTIVE for a in state['attempts'].values()),
              'close unclosed legacy receipts before registering outline mode')
        _need(not any(not r['verified'] and r.get('model_failure_attempt_id') != r.get('resolved_model_failure_attempt_id')
                      for r in state['tasks'].values()), 'resolve legacy model failures before registering outline mode')
        new_ids={w['task_id'] for m in spec['modules'] for w in m['work']}
        _need(not new_ids & set(state['tasks']),'registration cannot adopt old task identities implicitly')
        state['schema_version']=2
        state['outline']={'spec':spec,'cursor':0,'modules':[{'id':m['id'],'generation':1,'acceptance':None} for m in spec['modules']],
                          'completion':None,'history':[]}
        store._event(state,op,owner,{'revision':spec['revision'],'sha256':spec['sha256']})
        return {'status':'registered','current_module':spec['modules'][0]['id']}
    o=state.get('outline');_need(o is not None,'no outline registered');idle(store,state)
    if op=='outline_revise':
        spec=copy.deepcopy(p.get('outline'));validate_spec(spec);check_plan(spec)
        old=o['spec'];used={old['revision']} | {h['prior_spec']['revision'] for h in o['history'] if 'prior_spec' in h}
        _need(spec['revision'] not in used,'outline revision labels cannot be reused')
        reason=_nonempty(p.get('reason'),'revision reason')
        oldids=[m['id'] for m in old['modules']];newids=[m['id'] for m in spec['modules']]
        _need(newids[:len(oldids)]==oldids,'revision must preserve module identity/order; no reset via renaming')
        start=oldids.index(p.get('from_module')) if p.get('from_module') in oldids else len(oldids)
        _need(start<len(oldids),'from_module must identify the first affected existing module')
        _need(spec['modules'][:start]==old['modules'][:start],'revision changes earlier module contract')
        for i,m in enumerate(old['modules']):
            previous={w['task_id'] for w in m['work']};current={w['task_id'] for w in spec['modules'][i]['work']}
            _need(previous<=current,'revision cannot discard/rename prior work identities or their budgets')
        restrictions=any(spec[k]!=old[k] for k in ('controller_pins','override','delegation'))
        _need(not restrictions or start==0,'changed user restrictions must revalidate all modules')
        invalidate(store,state,start,reason)
        o['history'][-1]['prior_spec']=copy.deepcopy(old)
        o['spec']=spec
        o['modules'].extend({'id':m['id'],'generation':1,'acceptance':None} for m in spec['modules'][len(oldids):])
        store._event(state,op,owner,{'revision':spec['revision'],'from_module':p['from_module'],'reason':reason})
        return {'status':'revised','current_module':spec['modules'][o['cursor']]['id']}
    check_plan(o['spec'])
    _need(p.get('verifier_context_id')==owner,'only current coordinator accepts outline modules')
    if op=='outline_accept':
        i=o['cursor'];_need(i<len(o['modules']),'all modules already accepted')
        m=o['spec']['modules'][i];_need(p.get('module_id')==m['id'],'only the current module can be accepted')
        versions={}
        for w in m['work']:
            record=state['tasks'].get(w['task_id'])
            _need(record is not None and record['verified'],'module required work is not verified: '+w['task_id'])
            binding(o,record['versions'][-1]['task'])
            check_receipt(state,w)
            versions[w['task_id']]={'version':record['current_version'],'generation':record['validation_generation'],
                                    'digest':record['versions'][-1]['input_digest']}
        accepted={'at':_now(),'revision':o['spec']['revision'],'versions':versions,
                  'evidence':evidence_map(p.get('acceptance_evidence'),m['acceptance_ids']),
                  'runtime_evidence':_evidence(p.get('runtime_evidence'),'runtime evidence'),'verifier':owner}
        o['modules'][i]['acceptance']=accepted;o['cursor']+=1
        store._event(state,op,owner,{'module_id':m['id'],'acceptance':accepted})
        return {'status':'accepted','next_module':o['modules'][o['cursor']]['id'] if o['cursor']<len(o['modules']) else None}
    if op=='outline_complete':
        _need(o['cursor']==len(o['modules']),'all modules must pass before final integration acceptance')
        for ms in o['modules']:
            for tid,v in ms['acceptance']['versions'].items():
                r=state['tasks'][tid]
                _need(r['verified'] and r['current_version']==v['version'] and r['validation_generation']==v['generation'],
                      'module evidence is stale')
        from work_evidence import final_artifacts
        artifacts=final_artifacts(state,o)
        o['completion']={'at':_now(),'revision':o['spec']['revision'],'evidence_files':artifacts,
                         'evidence':evidence_map(p.get('evidence'),o['spec']['final_acceptance_ids']),'verifier':owner}
        store._event(state,op,owner,o['completion']);return {'status':'complete','completion':copy.deepcopy(o['completion'])}
    raise StateError('unknown outline operation: '+op)
