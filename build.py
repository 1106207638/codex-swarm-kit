"""Build a deterministic allowlisted archive. Does not publish anything."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import zipfile
import setup as installer

ROOT=Path(__file__).resolve().parent
RUNTIME_TESTS=('audit_tools','check_protocol','family_models','goal_switch','main_switch','outline_cli','outline_policy','outline_state','packet_transport','platform_runtime','router_policy','routing_cli','task_state','work_evidence')
DOCS=('RULES','RUNTIME','CONTRACT','OUTLINE','INSTALLATION','SELF-CHECK','ACCEPTANCE')
PAYLOAD=sorted(installer.REQUIRED | {'runtime/test_'+name+'.py' for name in RUNTIME_TESTS} | {'runtime/test_main_switch_host.js'} | {'docs/'+name+'.md' for name in DOCS})
ROOT_FILES=['Install-Windows.cmd','Install-macOS.command','setup.py','build.py','verify.py','VERSION','README.md','tests/test_installer.py']


def main():
    p=argparse.ArgumentParser();p.add_argument('--output-dir',default=str(ROOT/'dist'));args=p.parse_args()
    version=(ROOT/'VERSION').read_text(encoding='utf-8').strip()
    installer.need(re.fullmatch(r'\d+\.\d+\.\d+',version),'Invalid VERSION')
    files={name:installer.read(ROOT/name) for name in PAYLOAD+ROOT_FILES}
    installer.need(all(data is not None for data in files.values()),'Missing allowlisted source file')
    # Ship source, never the developer's state, environment, caches or logs.
    for name,data in files.items():
        text=data.decode('utf-8')
        for pattern in [r'(?i)[A-Z]:[/\\]+Users[/\\]+[^/\\\s]+',r'/Users/[^/\s]+/',r'\bsk-[A-Za-z0-9_-]{20,}\b']:
            # This scanner's pattern literals are not local paths or credentials.
            if name!='build.py':installer.need(not re.search(pattern,text),'Private source marker: '+name)
    manifest={'format':installer.FORMAT,'version':version,'files':{name:installer.sha(files[name]) for name in PAYLOAD}}
    raw=installer.json_bytes(manifest)
    (ROOT/'manifest.json').write_bytes(raw)
    installer.package(ROOT)
    files['manifest.json']=raw
    content=hashlib.sha256(b''.join(name.encode()+b'\0'+data for name,data in sorted(files.items()))).hexdigest()[:16]
    out=Path(args.output_dir).resolve();out.mkdir(parents=True,exist_ok=True)
    archive=out/('codex-swarm-kit-'+version+'-'+content+'.zip')
    with zipfile.ZipFile(archive,'w',compression=zipfile.ZIP_DEFLATED,compresslevel=9) as z:
        for name,data in sorted(files.items()):
            info=zipfile.ZipInfo('codex-swarm-kit/'+name,date_time=(2026,1,1,0,0,0));info.create_system=3
            info.external_attr=(0o100755 if name.endswith('.command') else 0o100644)<<16
            info.compress_type=zipfile.ZIP_DEFLATED
            z.writestr(info,data)
    digest=installer.sha(archive.read_bytes())
    checksum=archive.with_suffix('.zip.sha256');checksum.write_text(digest+'  '+archive.name+'\n',encoding='ascii')
    result={'archive':str(archive),'sha256':digest,'checksum':str(checksum),'files':len(files),'payload_files':len(PAYLOAD),'version':version}
    print(json.dumps(result,ensure_ascii=True))


if __name__=='__main__':main()
