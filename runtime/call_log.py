"""Per-ledger invocation diagnostics. State remains the source of truth."""

from datetime import datetime, timezone
import hashlib
import json
import math
import os
import re
import stat
import time
import uuid

from task_state import StateError, _file_lock, _need


FORMAT = 'codex-swarm-call-v1'
FILE = 'calls.jsonl'
MAX_LINE = 4096
PREVIEWS = {'route', 'main_recommend'}
LOGGED = {
    'add','update','prepare','complete_direct','bind','report','record_check','verify','fail','reconcile',
    'reconcile_direct','retry','handoff','outline_register','outline_revise','outline_accept','outline_complete',
    'main_switch_prepare','main_switch_claim','main_switch_check','main_switch_cancel','main_switch_receipt',
    'main_switch_confirm','main_switch_resolve','main_switch_reconcile_rejection',
    'external_bind','external_poll','external_resume','recap_note',
} | PREVIEWS
REASONS = set('''blocked_environment blocked_approval blocked_override_scope blocked_user_model
blocked_controller_constraint blocked_controller_settings blocked_controller_decision controller_required
independent_review planning_or_unknowns multi_source_research deterministic_simple_execution plan_required
complex_execution planned_execution conservative_default blocked_delegation_authority delegation_unavailable blocked_model'''.split())
REASONS.update({'bounded_external_text','blocked_external_restriction','blocked_external_scope'})
REASONS.update('''conflicting_explicit_pins ambiguous_whole_task_restriction approval_required required_tools_unavailable
fixed_settings_require_manual_selection explicit_no_switch_or_short_phase outline_controller strategic_or_high_risk
planning_or_analysis clear_execution technical_work already_suitable target_not_advertised'''.split())
ACTIONS = {'blocked','delegate','direct','keep','switch'}
STATUS = set('''dispatch_pending bound reported verified failed failure_reported no_spawn stopped direct_pending
direct_check_failed checks_recorded prepared ready sending accepted staged unknown mismatch confirmed cancelled
resolved rejected_before_start superseded complete reconciled registered revised handed_off'''.split())
FAILURES = {'model','input','environment','permission','validation','checker'}
ERROR_CODES = {
    'coordinator_mismatch', 'active_resource_conflict', 'unknown_or_active_dispatch', 'packet_prepare_failed',
    'packet_integrity', 'filesystem_error', 'state_rejected', 'request_or_policy_rejected',
}
EFFORTS = {'none','minimal','low','medium','high','xhigh','max','ultra','inherit','unknown'}
ROLES = {'astra_controller','sol_planner','sol_architect','luna_deep','luna_worker','luna_general','terra_general','direct_tool','spark_worker'}
ROLES.add('local_worker')
MODEL = re.compile(r'gpt-[0-9]+(?:\.[0-9]+)*(?:-(?:codex(?:-spark)?|astra|sol|luna|terra|nano|mini|pro))?')
REFS = {'task_id':'task_ref','attempt_id':'attempt_ref','direct_id':'direct_ref','request_id':'switch_ref','agent_id':'agent_ref'}
REFS['job_id']='job_ref'


def as_dict(value):
    return value if isinstance(value,dict) else {}


def pick(value, allowed):
    return value if isinstance(value,str) and value in allowed else None


def masked(value):
    if isinstance(value,str) and value.strip():
        return hashlib.sha256(value.strip().encode('utf-8','surrogatepass')).hexdigest()


def model_name(value):
    if isinstance(value,str) and len(value)<=48 and MODEL.fullmatch(value):
        return value
    return 'unknown'


def event_refs(payload):
    return {key:ref for field,key in REFS.items() if (ref:=masked(payload.get(field)))}


def failure_code(exc):
    message=str(exc)
    # Only these fixed labels are written. Original errors stay in the CLI envelope.
    if message in ('coordinator owner mismatch','stale coordinator epoch'):
        return 'coordinator_mismatch'
    if message.startswith(('active subagent limit reached','write ownership overlaps','direct write ownership overlaps')):
        return 'active_resource_conflict'
    if message.startswith(('task has an active or unknown','a main-thread switch is unresolved')):
        return 'unknown_or_active_dispatch'
    if message.startswith('could not build delegation packet:'):
        return 'packet_prepare_failed'
    if message in ('file task requires matching packet_sha256 receipt','packet bytes changed; do not execute'):
        return 'packet_integrity'
    if isinstance(exc,OSError):
        return 'filesystem_error'
    if isinstance(exc,StateError):
        return 'state_rejected'
    return 'request_or_policy_rejected'


def outcome(op, request, result):
    value=as_dict(result); decision=as_dict(value.get('decision'))
    receipt=as_dict(value.get('request')) if op=='main_switch_prepare' else value
    model=as_dict(decision.get('target',receipt.get('target')))
    result_status=pick(receipt.get('status'),STATUS)
    action=pick(decision.get('action'),ACTIONS)
    reason_codes=decision.get('reason_codes',[decision.get('reason')])
    details=event_refs(receipt)
    if decision.get('executor')=='external' or op.startswith('external_'):
        details['executor']='external'
        if decision.get('model'):
            details['external_model_ref']=masked(decision['model'])
        job=as_dict(request.get('receipt'))
        if job.get('id'):
            details['job_ref']=masked(job['id'])
    if action:
        details.update(action=action, role=pick(decision.get('role'),ROLES),
                       requested_model='unknown' if decision.get('executor')=='external' else model_name(decision.get('model')),
                       requested_effort=pick(decision.get('effort'),EFFORTS))
    if isinstance(reason_codes,list):
        safe=[reason for reason in reason_codes if pick(reason,REASONS)][:12]
        if safe:details['reason_codes']=safe
    if model:
        details.update(requested_model=model_name(model.get('model')),requested_effort=pick(model.get('effort'),EFFORTS))
    if op=='report' and result_status=='reported':
        details.update(reported_model=model_name(request.get('host_reported_model')),
                       reported_effort=pick(request.get('host_reported_effort'),EFFORTS))
    if op=='external_poll' and result_status=='reported':
        details['external_model_ref']=masked(as_dict(request.get('receipt')).get('model_used'))
    if result_status:details['result_status']=result_status
    category=pick(value.get('category',request.get('category') if op=='fail' else None),FAILURES)
    if category:details['failure_category']=category
    if value.get('idempotent') is True:details['idempotent']=True
    if action=='blocked':details['outcome']='blocked'
    elif result_status in {'failed','failure_reported','direct_check_failed','mismatch','unknown','rejected_before_start'}:
        details['outcome']='needs_attention'
    else:details['outcome']='returned'
    return details


def _check_file(info):
    _need(stat.S_ISREG(info.st_mode)
          and info.st_nlink == 1
          and not (getattr(info,'st_file_attributes',0)&0x400),
          'call log must be a regular file with no links')


def _regular(path):
    try:
        info=path.lstat()
    except FileNotFoundError:
        return False
    _check_file(info)
    return True


def append(root, row):
    path=root/FILE
    data=(json.dumps(row,ensure_ascii=True,separators=(',',':'))+'\n').encode('ascii')
    _need(len(data)<=MAX_LINE, 'call log record exceeds limit')
    lock=root/'calls.lock'
    _regular(lock)
    with _file_lock(lock):
        _regular(path)
        fd=os.open(path,os.O_RDWR|os.O_CREAT|os.O_APPEND|getattr(os,'O_NOFOLLOW',0)|getattr(os,'O_BINARY',0),0o600)
        with os.fdopen(fd,'r+b') as stream:
            _check_file(os.fstat(stream.fileno()))
            if stream.seek(0,os.SEEK_END):
                stream.seek(-1,os.SEEK_END)
                if stream.read(1)!=b'\n':
                    # Preserve an interrupted line, and keep the next record independently readable.
                    data=b'\n'+data
            stream.seek(0,os.SEEK_END)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())


class Call:
    def __init__(self, root, request):
        self.root=root
        self.request=request
        self.call_id=uuid.uuid4().hex
        self.start=time.monotonic()
        self.base={'format':FORMAT,'call_id':self.call_id,'op':request['op'],**event_refs(request)}
        if request['op'] in {'add', 'update', 'route', 'main_recommend'}:
            self.base.pop('task_ref',None)
            ref=masked(as_dict(request.get('task')).get('task_id'))
            if ref:self.base.update(task_ref=ref,nested_task_ref=ref)
        if type(request.get('epoch')) is int and 0<request['epoch']<=2**53-1:
            self.base['epoch']=request['epoch']

    def write(self, phase, **details):
        append(self.root,{**self.base,**details,'phase':phase,'at':datetime.now(timezone.utc).isoformat(timespec='milliseconds')})

    def finish(self, result=None, exc=None):
        details={'outcome':'rejected','error_code':failure_code(exc),'exit_code':2} if exc else outcome(self.base['op'],self.request,result)
        details.setdefault('exit_code',0)
        details['duration_ms']=round((time.monotonic()-self.start)*1000,1)
        self.write('finish',**details)


def _rows(stream):
    """Consume oversized input without allocating an unbounded physical line."""
    while (line:=stream.readline(MAX_LINE+1)):
        if len(line)>MAX_LINE:
            while line and not line.endswith(b'\n'):
                line=stream.readline(MAX_LINE+1)
            yield b'',line.endswith(b'\n')
        else:
            yield line,line.endswith(b'\n')


def _parse(line):
    if not line.endswith(b'\n'):
        raise ValueError('incomplete log line')
    row=json.loads(line)
    checks={
        'format':lambda v:v==FORMAT, 'op':lambda v:pick(v,LOGGED),
        'phase':lambda v:pick(v,{'start','finish'}),
        'call_id':lambda v:isinstance(v,str) and re.fullmatch('[0-9a-f]{32}',v),
        'at':lambda v:isinstance(v,str) and re.fullmatch(r'[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{3}\+00:00',v),
        'epoch':lambda v:type(v) is int and 0<v<=2**53-1,
        'executor':lambda v:v=='external',
        'external_model_ref':lambda v:isinstance(v,str) and re.fullmatch('[0-9a-f]{64}',v),
        'action':lambda v:pick(v,ACTIONS), 'role':lambda v:v is None or pick(v,ROLES),
        'requested_model':lambda v:v=='unknown' or model_name(v)!='unknown',
        'reported_model':lambda v:v=='unknown' or model_name(v)!='unknown',
        'requested_effort':lambda v:v is None or pick(v,EFFORTS),
        'reported_effort':lambda v:v is None or pick(v,EFFORTS),
        'reason_codes':lambda v:isinstance(v,list) and len(v)<=12 and all(pick(reason,REASONS) for reason in v),
        'result_status':lambda v:pick(v,STATUS), 'failure_category':lambda v:pick(v,FAILURES),
        'idempotent':lambda v:v is True,
        'outcome':lambda v:pick(v,{'returned','rejected','blocked','needs_attention'}),
        'error_code':lambda v:pick(v,ERROR_CODES), 'exit_code':lambda v:type(v) is int and v in (0,2),
        'duration_ms':lambda v:type(v) in (int,float) and 0<=v<=1e16 and math.isfinite(v),
    }
    for key in REFS.values():
        checks[key]=lambda v:isinstance(v,str) and re.fullmatch('[0-9a-f]{64}',v)
    checks['nested_task_ref']=checks['task_ref']
    if not isinstance(row,dict) or not {'format','op','phase','call_id','at'}<=row.keys():
        raise ValueError('missing log fields')
    if not all(key in checks and checks[key](value) for key,value in row.items()):
        raise ValueError('unexpected log fields')
    return row


def _task_refs(state, task_id):
    task=masked(task_id)
    related=set()
    for tid,record in state['tasks'].items():
        if masked(tid)!=task:continue
        matches=[record.get('direct_pending') or {},*record.get('direct_completions',[]),*record.get('direct_history',[])]
        related.update(('direct_ref',masked(v.get('direct_id'))) for v in matches if v.get('direct_id'))
    related.update(('attempt_ref',masked(a)) for a,v in state['attempts'].items() if masked(v.get('task_id'))==task)
    related.update(('switch_ref',masked(r)) for r,v in state.get('main_switches',{}).items() if masked(v.get('task_id'))==task)
    return related


def read(store, owner, epoch, *, task_id=None, attempt_id=None, call_id=None, after_line=0, limit=100):
    _need(type(after_line) is int and after_line>=0 and type(limit) is int and 1<=limit<=500,
          'call_log needs after_line >= 0 and limit from 1 to 500')
    for value in (task_id,attempt_id,call_id):
        _need(value is None or isinstance(value,str) and bool(value.strip()), 'log filters must be non-empty strings')
    _need(call_id is None or re.fullmatch('[0-9a-f]{32}',call_id), 'invalid call_id')
    task=masked(task_id)
    attempt=masked(attempt_id)
    log=store.root/FILE
    records=[]; skipped=0; cursor=after_line
    if _regular(log):
        lock=store.root/'calls.lock'
        _regular(lock)
        with _file_lock(lock), log.open('rb') as stream:
            _check_file(os.fstat(stream.fileno()))
            # Snapshot task associations after excluding new log appends. State
            # operations release the state lock before writing their finish row.
            state=store.status(owner,epoch)
            related=_task_refs(state,task_id) if task is not None else set()
            prepare_call=state['attempts'].get(attempt_id.strip(),{}).get('prepare_call_id') if attempt is not None else None
            for line_number,(line,complete) in enumerate(_rows(stream),1):
                if line_number<=after_line:continue
                if not complete:
                    skipped+=1
                    break
                cursor=line_number
                try:
                    row=_parse(line)
                except (ValueError,TypeError,RecursionError):
                    skipped+=1;continue
                if call_id is not None and row.get('call_id')!=call_id:continue
                if attempt is not None and row.get('attempt_ref')!=attempt and row['call_id']!=prepare_call:continue
                if task is not None and row.get('task_ref')!=task and not any((k,row.get(k)) in related for k in ('attempt_ref','direct_ref','switch_ref')):continue
                records.append(row)
                if len(records)>=limit:break
    else:
        store.status(owner,epoch)
    return {'format':FORMAT,'records':records,'next_after_line':cursor,'skipped_lines':skipped}
