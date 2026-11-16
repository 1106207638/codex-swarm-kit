"""Development verification. Python and Node must already be installed."""
import json
from pathlib import Path
import shutil
import subprocess
import sys

root=Path(__file__).resolve().parent
commands=[[sys.executable,'-B','-X','utf8','-m','unittest','discover','-s',str(root/'runtime'),'-p','test_*.py'],
          [sys.executable,'-B','-X','utf8','-m','unittest','discover','-s',str(root/'tests'),'-p','test_*.py']]
node=shutil.which('node')
if node:commands.append([node,'--test',str(root/'runtime/test_main_switch_host.js')])
results=[]
for command in commands:
    p=subprocess.run(command,cwd=root,capture_output=True,text=True,encoding='utf-8')
    results.append({'command':command,'exit_code':p.returncode,'stdout':p.stdout,'stderr':p.stderr})
print(json.dumps({'checks':results,'node_available':bool(node),'macOS_real_machine':'not established by this command'},ensure_ascii=True,indent=2))
raise SystemExit(0 if node and all(r['exit_code']==0 for r in results) else 2)
