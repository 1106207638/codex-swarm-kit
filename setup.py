"""Standard-library installer. No downloads, model calls, credentials or deletes."""
import sys
if sys.version_info < (3, 11):
    print('Python 3.11+ required. Install from https://www.python.org/downloads/')
    raise SystemExit(2)

import argparse
import base64
import contextlib
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import uuid

BEGIN = b'<!-- BEGIN CODEX-SWARM-KIT -->'
END = b'<!-- END CODEX-SWARM-KIT -->'
FORMAT = 'codex-swarm-package-v1'
ROOT = Path(__file__).resolve().parent
UNSPECIFIED = object()
REQUIRED = {'runtime/'+name for name in (
    'routing.py','router_policy.py','task_state.py','resolve_family_models.py','main_switch.py',
    'main_switch_host.js','outline_state.py','work_evidence.py','check_protocol.py',
    'packet_transport.py','audit_tools.py','platform_runtime.py')}
REQUIRED |= {'runtime/agents/'+name+'.toml' for name in ('sol-planner','sol-architect','luna-worker','luna-deep','luna-general','terra-general','spark-worker')}
REQUIRED |= {'docs/'+name+'.md' for name in ('RULES','RUNTIME','CONTRACT','OUTLINE')}


def required_files(version):
    if tuple(map(int,version.split('.'))) >= (1,0,3):
        return required_files('1.0.2') | {'runtime/swarm_recap.py'}
    if tuple(map(int,version.split('.'))) >= (1,0,2):
        return REQUIRED | {'runtime/call_log.py','runtime/external_worker.py','runtime/external_contract.py'}
    if tuple(map(int,version.split('.'))) >= (1,0,1):
        return REQUIRED | {'runtime/call_log.py'}
    return REQUIRED


class InstallError(Exception): pass


def need(value, message):
    if not value: raise InstallError(message)


def sha(data): return hashlib.sha256(data).hexdigest()


def encode(data): return None if data is None else base64.b64encode(data).decode('ascii')


def decode(data):
    return None if data is None else base64.b64decode(data, validate=True)


def json_bytes(value): return (json.dumps(value,ensure_ascii=True,indent=2)+'\n').encode('utf-8')


def unique(pairs):
    result={}
    for key,value in pairs:
        need(key not in result, 'Duplicate JSON key')
        result[key]=value
    return result


def parse(data):
    need(isinstance(data,bytes) and data,'Missing/empty JSON document')
    result=json.loads(data.decode('utf-8'),object_pairs_hook=unique)
    need(isinstance(result,dict),'JSON document must be an object')
    return result


def no_links(path):
    """Reject symlinks and Windows reparse points in every existing component."""
    path=Path(os.path.abspath(path))
    for item in [*reversed(path.parents),path]:
        try: info=item.lstat()
        except FileNotFoundError: continue
        need(not stat.S_ISLNK(info.st_mode) and not (getattr(info,'st_file_attributes',0)&0x400),
             'Linked/reparse path is not supported: '+str(item))
    return path


def read(path):
    no_links(path)
    if not path.exists(): return None
    need(path.is_file(), 'Expected regular file: '+str(path))
    return path.read_bytes()


def atomic(path,data,mode=None,expected=UNSPECIFIED):
    no_links(path)
    path.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
    if mode is None:mode=stat.S_IMODE(path.stat().st_mode) if path.exists() else 0o600
    temp=path.with_name(path.name+'.writing-'+uuid.uuid4().hex)
    fd=os.open(temp,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    with os.fdopen(fd,'wb') as stream:
        stream.write(data);stream.flush();os.fsync(stream.fileno())
    if os.name!='nt':os.chmod(temp,mode)
    # Detect saves during temporary-file preparation as well as preflight.
    # This comparison and replace are not a filesystem compare-and-swap.
    if expected is not UNSPECIFIED:
        need(read(path)==expected,'Target changed while preparing replacement; original preserved: '+str(path))
    else:no_links(path)
    os.replace(temp,path)
    if os.name!='nt':
        fd=os.open(path.parent,os.O_RDONLY)
        try:os.fsync(fd)
        finally:os.close(fd)


@contextlib.contextmanager
def locked(home):
    root=no_links(home/'swarm-kit');root.mkdir(mode=0o700,parents=True,exist_ok=True)
    path=root/'install.lock';no_links(path)
    with path.open('a+b') as stream:
        stream.seek(0,2)
        if stream.tell()==0:stream.write(b'0');stream.flush()
        stream.seek(0)
        try:
            if os.name=='nt':
                import msvcrt
                msvcrt.locking(stream.fileno(),msvcrt.LK_NBLCK,1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
        except OSError as exc:raise InstallError('Another installer is active; do not steal the lock') from exc
        try:yield
        finally:
            stream.seek(0)
            if os.name=='nt':msvcrt.locking(stream.fileno(),msvcrt.LK_UNLCK,1)
            else:fcntl.flock(stream.fileno(),fcntl.LOCK_UN)


def relative_path(value):
    need(isinstance(value,str) and value and '\\' not in value and ':' not in value, 'Invalid package path')
    p=PurePosixPath(value)
    need(p.parts and not p.is_absolute() and str(p)==value and all(part not in ('.','..') and
         re.fullmatch(r'[A-Za-z0-9_.-]+',part) and not part.endswith(('.', ' ')) and
         not re.fullmatch(r'(?i)(CON|PRN|AUX|NUL|COM[0-9]|LPT[0-9])(?:\..*)?',part) for part in p.parts),
         'Unsafe package path: '+value)
    need(p.parts[0] in ('runtime','docs') and p.suffix in ('.py','.js','.toml','.md'), 'File outside payload allowlist')
    return p


def package(root):
    raw=read(root/'manifest.json');need(raw is not None,'Missing manifest.json')
    manifest=parse(raw)
    need(set(manifest)=={'format','version','files'} and manifest['format']==FORMAT,'Unsupported manifest')
    need(isinstance(manifest['version'],str) and re.fullmatch(r'[0-9]+\.[0-9]+\.[0-9]+',manifest['version']),'Invalid version')
    files=manifest['files'];need(isinstance(files,dict) and files,'Empty payload')
    seen=set();prefixes={};payload={}
    for name,digest in files.items():
        p=relative_path(name);key=name.casefold()
        need(key not in seen,'Case-colliding package paths')
        need(not any(key.startswith(old+'/') or old.startswith(key+'/') for old in seen),'File/directory package collision')
        for count in range(1,len(p.parts)+1):
            prefix='/'.join(p.parts[:count]);folded=prefix.casefold()
            need(prefixes.get(folded,prefix)==prefix,'Case-alias package directory')
            prefixes[folded]=prefix
        seen.add(key)
        need(isinstance(digest,str) and re.fullmatch('[0-9a-f]{64}',digest),'Invalid file hash')
        data=read(root/str(p));need(data is not None and sha(data)==digest,'Package file changed/missing: '+name)
        payload[name]=data
    need(required_files(manifest['version'])<=set(files),'Incomplete package')
    return manifest,raw,payload


def section(data):
    data=data or b''
    need(b'CODEX-UNIVERSAL-ROUTER' not in data,'Existing universal router: explicit migration required; nothing replaced')
    need(data.count(BEGIN)==data.count(END)<=1,'Damaged/duplicate swarm rule markers')
    if BEGIN not in data:
        need(b'CODEX-SWARM-KIT' not in data,'Unrecognized swarm rule marker')
        return None
    start=data.index(BEGIN);end=data.index(END)
    need(end>start and (start==0 or data[start-1:start]==b'\n'),'Malformed rule block')
    end+=len(END)
    if data[end:end+2]==b'\r\n':end+=2
    elif data[end:end+1]==b'\n':end+=1
    else:need(end==len(data),'Rule end marker must end a line')
    return start,end,data[start:end]


def block(release):
    # JSON string keeps the path readable without permitting marker/newline injection.
    path=json.dumps(str(release),ensure_ascii=True).replace('<',r'\u003c').replace('>',r'\u003e')
    path=path.replace('CODEX-UNIVERSAL-ROUTER',r'\u0043ODEX-UNIVERSAL-ROUTER')
    return (BEGIN+b'\n'+('Use the installed Codex swarm workflow for applicable tasks. User/project approvals and explicit model restrictions remain in force.\nRelease directory: '+path+'\nRead docs/RULES.md and docs/RUNTIME.md in that directory before routing. Use runtime/routing.py and runtime/agents; inspect the current native tool/model catalog. Small tasks may be completed directly. A plan or file is not authorization.\n').encode('utf-8')+END+b'\n')


def pointer(home,raw=None):
    if raw is None:raw=read(home/'swarm-kit/active.json')
    if raw is None:return None,None
    value=parse(raw)
    need(value.get('format')=='codex-swarm-active-v1','Unknown active pointer')
    if value.get('active') is False:return value,raw
    name=value.get('release')
    need(isinstance(name,str) and re.fullmatch(r'[0-9]+\.[0-9]+\.[0-9]+-[0-9a-f]{16}',name),'Invalid active release')
    release=home/'swarm-kit/releases'/name
    manifest,manifest_raw,_=package(release)
    need(sha(manifest_raw)==value.get('manifest_sha256') and name==manifest['version']+'-'+sha(manifest_raw)[:16],'Active release changed')
    return value,raw


def pending(home):
    folder=no_links(home/'swarm-kit/transactions')
    if not folder.exists():return []
    result=[]
    for path in sorted(folder.glob('*/journal.json')):
        value=parse(read(path));need(value.get('status') in ('pending','complete'),'Unknown transaction state')
        if value['status']=='pending':result.append(path.parent.name)
    return result


def preflight(home):
    no_links(home)
    no_override(home)
    need(not pending(home),'Incomplete transaction; run resume with its transaction ID first')
    agents=read(home/'AGENTS.md');part=section(agents)
    active,raw=pointer(home)
    if active and active.get('active') is not False:
        expected=block(home/'swarm-kit/releases'/active['release'])
        need(part and part[2]==expected,'Managed rules changed; refusing overwrite')
    else:need(part is None,'Unowned managed block; refusing takeover')
    return agents,part,active,raw


def no_override(home):
    # lstat sees a dangling link as an occupied name; exists() does not.
    try:(home/'AGENTS.override.md').lstat()
    except FileNotFoundError:return
    raise InstallError('AGENTS.override.md requires manual workflow integration')


def pair_valid(home,agents,active_raw):
    part=section(agents)
    active=pointer(home,active_raw)[0] if active_raw is not None else None
    if active and active.get('active') is not False:
        need(part and part[2]==block(home/'swarm-kit/releases'/active['release']),'Planned rule/pointer mismatch')
    else:need(part is None,'Planned inactive pointer has active rules')


def journal_valid(home,tid,record):
    need(record.get('format')=='codex-swarm-transaction-v1' and record.get('id')==tid and record.get('home')==str(home),'Transaction identity mismatch')
    need(record.get('status') in ('pending','complete') and record.get('kind') in ('install','restore'),'Unknown transaction state/kind')
    rows=record.get('changes');need(isinstance(rows,list) and len(rows)==2 and all(isinstance(r,dict) for r in rows),'Invalid transaction')
    need(all(set(r)=={'path','before','after','before_sha256','after_sha256'} for r in rows),'Invalid transaction fields')
    need({r['path'] for r in rows}=={'AGENTS.md','swarm-kit/active.json'},'Invalid transaction paths')
    for row in rows:
        need((row['before'] is None or isinstance(row['before'],str)) and isinstance(row['after'],str),'Invalid transaction bytes')
        before,after=decode(row['before']),decode(row['after'])
        need(sha(after)==row['after_sha256'] and (None if before is None else sha(before))==row['before_sha256'],'Damaged transaction bytes')
    by_name={r['path']:r for r in rows}
    for when in ('before','after'):
        pair_valid(home,decode(by_name['AGENTS.md'][when]),decode(by_name['swarm-kit/active.json'][when]))
    return rows


def make_transaction(home,kind,changes,extra=None):
    tid=uuid.uuid4().hex;folder=home/'swarm-kit/transactions'/tid
    rows=[]
    for name,before,after in changes:
        need(name in ('AGENTS.md','swarm-kit/active.json'),'Invalid transaction target')
        need(read(home/name)==before,'External edit after preview; refusing to adopt changed bytes')
        rows.append({'path':name,'before':encode(before),'after':encode(after),'before_sha256':None if before is None else sha(before),'after_sha256':sha(after)})
    record={'format':'codex-swarm-transaction-v1','id':tid,'home':str(home),'kind':kind,'status':'pending','changes':rows,'extra':extra or {}}
    journal_valid(home,tid,record)
    atomic(folder/'journal.json',json_bytes(record),mode=0o600,expected=None)
    return tid


def resume_preview(home,tid):
    need(re.fullmatch('[0-9a-f]{32}',tid or ''),'Invalid transaction ID')
    no_override(home)
    raw=read(home/'swarm-kit/transactions'/tid/'journal.json');record=parse(raw)
    rows=journal_valid(home,tid,record)
    for row in rows:
        need(read(home/row['path']) in (decode(row['before']),decode(row['after'])),'External edit after transaction; manual reconciliation required: '+row['path'])
    return {'journal_sha256':sha(raw),'targets':{r['path']:encode(read(home/r['path'])) for r in rows}}


def resume_locked(home,tid,checkpoint=None,approved=None):
    need(re.fullmatch('[0-9a-f]{32}',tid or ''),'Invalid transaction ID')
    binding=resume_preview(home,tid)
    need(approved is None or binding==approved,'Resume target changed since confirmation')
    path=home/'swarm-kit/transactions'/tid/'journal.json';raw=read(path)
    need(sha(raw)==binding['journal_sha256'],'Journal changed during resume')
    record=parse(raw)
    rows=journal_valid(home,tid,record)
    # Preflight every target before writing any of them; never clobber a third value.
    for row in rows:
        before,after=decode(row['before']),decode(row['after'])
        need(after is not None and sha(after)==row['after_sha256'] and (None if before is None else sha(before))==row['before_sha256'],'Damaged transaction bytes')
        actual=read(home/row['path'])
        need(actual in (before,after),'External edit after transaction; manual reconciliation required: '+row['path'])
        if row['path']=='swarm-kit/active.json':pointer(home,after)
    if record['status']=='complete':
        need(all(read(home/r['path'])==decode(r['after']) for r in rows),'Completed transaction changed; do not replay it')
        return record
    for index,row in enumerate(rows):
        actual=read(home/row['path'])
        need(actual in (decode(row['before']),decode(row['after'])),'Target changed during commit; reconciliation required')
        if actual!=decode(row['after']):atomic(home/row['path'],decode(row['after']),expected=actual)
        if checkpoint:checkpoint(index) # Tests inject process interruption; never exposed as CLI code.
    record['status']='complete';atomic(path,json_bytes(record),mode=0o600,expected=raw)
    return record


def install(home,source=ROOT,preview=False,checkpoint=None,approved=None):
    manifest,raw,payload=package(source) # Verify source before touching destination.
    release_name=manifest['version']+'-'+sha(raw)[:16]
    release=home/'swarm-kit/releases'/release_name
    if preview:
        agents,part,active,active_raw=preflight(home)
        return {'action':'preview','home':str(home),'release':release_name,'files':len(payload),'managed_rules':'replace owned block' if part else 'prepend block', 'native_capabilities':'unknown; check in Codex','rule_before':part[2].decode('utf-8') if part else '', 'rule_after':block(release).decode('utf-8'),'binding':{'manifest':sha(raw),'home':str(home),'agents':None if agents is None else sha(agents),'active':None if active_raw is None else sha(active_raw),'block':sha(block(release))}}
    with locked(home):
        agents,part,active,active_raw=preflight(home)
        binding={'manifest':sha(raw),'home':str(home),'agents':None if agents is None else sha(agents),'active':None if active_raw is None else sha(active_raw),'block':sha(block(release))}
        need(approved is None or binding==approved,'Installation changed since confirmation; preview again')
        if active and active.get('active') is not False and active['release']==release_name:
            return {'action':'already-installed','release':release_name,'home':str(home)}
        # Immutable release; partial copies may resume only if existing bytes agree.
        for name,data in {**payload,'manifest.json':raw}.items():
            dest=release/name;before=read(dest)
            need(before is None or before==data,'Release file changed; refusing overwrite: '+name)
        for name,data in {**payload,'manifest.json':raw}.items():
            dest=release/name
            if read(dest) is None:atomic(dest,data,expected=None)
        old=agents or b'';new_block=block(release)
        new_agents=(old[:part[0]]+new_block+old[part[1]:]) if part else new_block+old
        new_pointer={'format':'codex-swarm-active-v1','active':True,'release':release_name,'manifest_sha256':sha(raw),'runtime':{'platform':'windows' if os.name=='nt' else 'posix','python':sys.executable,'codex_home':str(home)}}
        tid=make_transaction(home,'install',[('swarm-kit/active.json',active_raw,json_bytes(new_pointer)),('AGENTS.md',agents,new_agents)],{'release':release_name})
        result=resume_locked(home,tid,checkpoint)
        return {'action':'installed','transaction':tid,'release':release_name,'home':str(home),'status':result['status']}


def restore(home,tid,preview=False,checkpoint=None,approved=None):
    need(re.fullmatch('[0-9a-f]{32}',tid or ''),'Invalid transaction ID')
    def prepare():
        agents,part,active,active_raw=preflight(home)
        journal=home/'swarm-kit/transactions'/tid/'journal.json';journal_raw=read(journal);record=parse(journal_raw)
        journal_valid(home,tid,record)
        need(record.get('home')==str(home) and record.get('id')==tid and record.get('status')=='complete' and record.get('kind')=='install','Only completed local install transactions can be restored')
        by_name={r['path']:r for r in record['changes']}
        a=by_name['AGENTS.md'];p=by_name['swarm-kit/active.json']
        need(active_raw==decode(p['after']),'This install is not current; restore the newest install first')
        expected=section(decode(a['after']));prior=section(decode(a['before']))
        need(part and expected and part[2]==expected[2],'Rule block changed; refusing restore')
        restored=agents[:part[0]]+(prior[2] if prior else b'')+agents[part[1]:]
        before_pointer=decode(p['before']) or json_bytes({'format':'codex-swarm-active-v1','active':False})
        pointer(home,before_pointer)
        return restored,before_pointer,agents,active_raw,{'journal':sha(journal_raw),'agents':sha(agents),'active':sha(active_raw),'restored_block':sha(prior[2]) if prior else None}
    if preview:
        result=prepare();return {'action':'restore-preview','transaction':tid,'preserves':'outside rules, releases, ledgers, backups','binding':result[4]}
    with locked(home):
        agents,previous,current_agents,current_active,binding=prepare()
        need(approved is None or approved==binding,'Restore target changed since confirmation')
        rollback=make_transaction(home,'restore',[('AGENTS.md',current_agents,agents),('swarm-kit/active.json',current_active,previous)],{'restores':tid})
        resume_locked(home,rollback,checkpoint)
        return {'action':'restored','transaction':rollback,'restores':tid,'retained_releases':True}


def doctor(home):
    no_override(home)
    active,raw=pointer(home);agents=read(home/'AGENTS.md');part=section(agents)
    unresolved=pending(home)
    need(not unresolved,'Pending transactions: '+', '.join(unresolved))
    installed=bool(active and active.get('active') is not False)
    need((part is not None)==installed,'Rule/pointer mismatch')
    if installed:need(part[2]==block(home/'swarm-kit/releases'/active['release']),'Managed rule block changed')
    return {'installed':installed,'local_integrity':'passed','release':active.get('release') if installed else None,'home':str(home),'native_tools':'unknown; run docs/SELF-CHECK.md in Codex','models':'unknown; live catalog required','macOS_real_machine':'pending' if sys.platform=='darwin' else 'not tested by this command'}


def main(argv=None):
    parser=argparse.ArgumentParser(description='Codex Swarm Kit: preview/install/doctor/restore/resume; no network calls')
    parser.add_argument('action',choices=['preview','install','doctor','restore','resume'],nargs='?',default='install')
    parser.add_argument('--codex-home');parser.add_argument('--transaction');parser.add_argument('--yes',action='store_true')
    args=parser.parse_args(argv)
    raw=args.codex_home or os.environ.get('CODEX_HOME') or str(Path.home()/'.codex')
    home=Path(raw).expanduser()
    need(home.is_absolute() and not any(c in str(home) for c in '\r\n\0'),'Codex home must be an absolute single-line path')
    home=no_links(home).resolve()
    if args.action=='preview':return install(home,preview=True)
    if args.action=='doctor':return doctor(home)
    if args.action=='install':plan=install(home,preview=True)
    elif args.action=='restore':plan=restore(home,args.transaction,preview=True)
    else:plan={'action':'resume','home':str(home),'transaction':args.transaction,'binding':resume_preview(home,args.transaction)}
    if not args.yes:
        print(json.dumps({k:v for k,v in plan.items() if k!='binding'},ensure_ascii=False,indent=2))
        print('Only managed rules and swarm-kit files change. Keep ongoing tasks on their original release. No account/config.toml changes.')
        need(input('Continue? [y/N] ').strip().lower()=='y','Cancelled; no installation changes')
    if args.action=='install':return install(home,approved=plan['binding'])
    if args.action=='restore':return restore(home,args.transaction,approved=plan['binding'])
    with locked(home):return {'action':'resumed','status':resume_locked(home,args.transaction,approved=plan['binding'])['status'],'transaction':args.transaction}


if __name__=='__main__':
    try:
        result=main();print(json.dumps({'ok':True,'result':result},ensure_ascii=True))
    except (InstallError,OSError,ValueError,TypeError,KeyError,EOFError) as exc:
        print(json.dumps({'ok':False,'error':str(exc)},ensure_ascii=True));sys.exit(2)
