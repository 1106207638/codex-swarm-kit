"""Declared resource coordination and frozen evidence for outline work.

Scope declarations and host slot counts are caller-supplied, not OS controls.
Command reports require independent controller checks; their text is not proof
that a command actually ran. File hashes establish the inspected bytes only.
"""
import copy
import hashlib
import re
from pathlib import Path

from task_state import StateError, _need, _evidence, _nonempty


def paths(value,label):
    _need(isinstance(value,list),'missing list: '+label)
    _need(all(isinstance(p,str) and Path(p).is_absolute() for p in value),'absolute paths required: '+label)
    normalized=[str(Path(p).resolve()).casefold() for p in value]
    _need(len(set(normalized))==len(value),'duplicate paths: '+label)
    return value


def covers(roots,path):
    return any(Path(path).resolve().is_relative_to(Path(r).resolve()) for r in roots)


def validate_task(task):
    if task.get('mode')!='outline':return
    resources=task.get('resources')
    _need(isinstance(resources,list) and all(isinstance(r,str) and r.strip()==r and r for r in resources),'resources list required')
    _need(len({r.casefold() for r in resources})==len(resources),'duplicate resource ids')
    reads=paths(task.get('read_paths'),'read_paths');artifacts=paths(task.get('artifacts'),'artifacts')
    _need(all(covers(task['ownership'],p) for p in artifacts),'artifact must be inside ownership')
    frozen=task.get('snapshot_files');_need(isinstance(frozen,dict),'snapshot_files map required')
    paths(list(frozen),'snapshot_files')
    for p,h in frozen.items():
        _need(isinstance(h,str) and re.fullmatch('[0-9a-f]{64}',h),'invalid snapshot hash')
        _need(covers(reads+task['ownership'],p),'snapshot file outside declared read scope')


def check_hashes(files,label):
    for p,h in files.items():
        f=Path(p)
        _need(f.is_file() and hashlib.sha256(f.read_bytes()).hexdigest()==h,label+' changed/missing: '+p)


def canonical(path):
    return str(Path(path).resolve()).casefold()


def dependency_artifacts(store,state,task):
    from outline_state import receipt_for
    deps=store._dependency_versions(state,task)
    latest={}
    # Module order defines intentional successive versions of a shared file.
    for module in state['outline']['spec']['modules']:
        for work in module['work']:
            tid=work['task_id']
            if tid not in deps:continue
            _,direct,r=receipt_for(state,tid)
            h=r.get('handoff') if direct else (r.get('report') or {}).get('handoff')
            _need(isinstance(h,dict),'dependency lacks a signed artifact handoff')
            latest.update({canonical(p):digest for p,digest in h['artifacts'].items()})
    return latest


def prepare_checks(store,state,task,decision,p):
    if task.get('mode')!='outline' or decision['action']=='blocked':return
    validate_task(task)
    readonly=decision['role'] in ('sol_planner','sol_architect') and decision['action']=='delegate'
    if task['features']['stage']=='review':
        _need(bool(task['snapshot_files']),'review needs frozen snapshot files')
        expected=dependency_artifacts(store,state,task)
        frozen={canonical(path):digest for path,digest in task['snapshot_files'].items()}
        _need(set(expected) <= set(frozen),'review snapshots must cover dependency artifacts')
        _need(all(frozen[path]==digest for path,digest in expected.items()),'review snapshot differs from signed artifact version')
    if decision['action']=='delegate':
        slots=p.get('host_slots_available')
        _need(type(slots) is int and 0<=slots<=2,'host_slots_available must be a current native slot count (0..2)')
        _need(slots>0,'host agent slots full; queue without dispatch')
        reserved=sum(a.get('agent_id') is None for a in store._active(state))
        _need(slots>reserved,'remaining native slots are already reserved by unbound dispatches')
    active=[]
    for a in store._active(state):active.append((a['task_snapshot'],a['read_only']))
    for tid,r in state['tasks'].items():
        if r.get('direct_pending'):active.append((r['versions'][-1]['task'],False))
    # snapshot_files are always reads, even if not separately repeated in read_paths.
    reads=task['read_paths']+list(task['snapshot_files'])
    def overlap(a,b):
        return store._overlap([str(Path(x).resolve()).casefold() for x in a],
                              [str(Path(x).resolve()).casefold() for x in b])
    for other,other_readonly in active:
        _need(not {r.casefold() for r in task['resources']} & {r.casefold() for r in other.get('resources',[])},'shared resource is already occupied')
        other_reads=other.get('read_paths',[])+list(other.get('snapshot_files',{}))
        if not other_readonly:
            _need(not overlap(reads,other['ownership']),'read scope overlaps an active writer')
            if readonly:_need(not overlap(task['ownership'],other['ownership']),'review scope overlaps an active writer')
        if not readonly:
            _need(not overlap(task['ownership'],other_reads+(other['ownership'] if other_readonly else [])),
                  'write scope overlaps an active reader')
    # Read bytes only after all registered read/write conflicts are excluded.
    check_hashes(task['snapshot_files'],'input snapshot')


def handoff(task,value,*,accept=False):
    _need(isinstance(value,dict) and set(value)=={'artifacts','checks','remaining','risks','scope_evidence'},'structured handoff required')
    for k in ('remaining','risks'):
        _need(isinstance(value[k],list) and all(isinstance(s,str) and s.strip() for s in value[k]),'invalid handoff '+k)
    _evidence(value['scope_evidence'],'scope evidence (controller must inspect tool trace)')
    outputs=value['artifacts'];_need(isinstance(outputs,dict),'handoff artifacts must be path/hash map')
    _need(set(outputs)<=set(task['artifacts']),'handoff includes undeclared artifacts')
    for h in outputs.values():_need(isinstance(h,str) and re.fullmatch('[0-9a-f]{64}',h),'invalid artifact hash')
    checks=value['checks'];_need(isinstance(checks,list) and checks,'handoff must contain actual checks')
    for c in checks:
        _need(isinstance(c,dict) and set(c)=={'command','exit_code','expected_exit_code','evidence_ref'},'invalid handoff check')
        for k in ('command','evidence_ref'):_nonempty(c[k],k)
        _need(type(c['exit_code']) is int and type(c['expected_exit_code']) is int,'check exit codes must be integers')
    if accept:
        _need(not value['remaining'],'unverified required work remains')
        _need(all(c['exit_code']==c['expected_exit_code'] for c in checks),'reported validation has not passed')
        _need(set(outputs)==set(task['artifacts']),'missing required artifact evidence')
        check_hashes(task['snapshot_files'],'input snapshot')
        check_hashes(outputs,'delivered artifact')
    return copy.deepcopy(value)


def accepted_handoff(task,receipt,direct):
    value=receipt.get('handoff') if direct else (receipt.get('report') or {}).get('handoff')
    return handoff(task,value,accept=True)


def final_artifacts(state,outline):
    from outline_state import receipt_for
    latest={}
    for m in outline['spec']['modules']:
        module_outputs={};module_inputs={}
        for w in m['work']:
            _,direct,r=receipt_for(state,w['task_id'])
            h=r.get('handoff') if direct else r['report'].get('handoff')
            _need(isinstance(h,dict),'missing retained handoff')
            task=state['tasks'][w['task_id']]['versions'][-1]['task']
            for path,digest in task['snapshot_files'].items():
                key=canonical(path)
                _need(key not in module_inputs or module_inputs[key][1]==digest,'module frozen inputs disagree')
                module_inputs[key]=(path,digest)
            for path,digest in h['artifacts'].items():module_outputs[canonical(path)]=(path,digest)
        for key,(path,digest) in module_inputs.items():
            expected=module_outputs.get(key,latest.get(key))
            _need(expected is None or expected[1]==digest,'frozen input changed without a signed output replacement')
            latest[key]=(path,digest)
        latest.update(module_outputs)
    files={path:digest for path,digest in latest.values()}
    check_hashes(files,'final evidence')
    return files
