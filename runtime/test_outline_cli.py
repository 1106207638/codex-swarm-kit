import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
import test_outline_state as fixtures
from test_task_state import CATALOG, PROFILES, provenance


class OutlineCliTests(unittest.TestCase):
    def test_process_restart_and_legacy_schema_rejection(self):
        f=fixtures.OutlineStateTests();f.setUp()
        base={'state_dir':str(f.root/'state'),'owner':f.owner,'epoch':1}
        def run(op,code=0,**kw):
            if op=='prepare':kw['host_slots_available']=2
            r=subprocess.run([sys.executable,str(Path(__file__).parent/'routing.py')],
                             input=json.dumps(dict(base,op=op,**kw)),capture_output=True,text=True,encoding='utf-8',
                             env=dict(os.environ,PYTHONIOENCODING='utf-8'),timeout=15)
            self.assertEqual(r.returncode,code,r.stdout+r.stderr)
            return json.loads(r.stdout)
        run('outline_register',outline=f.spec)
        run('add',task=f.item('cli'))
        blocked=run('prepare',2,task_id='cli',catalog=CATALOG,catalog_provenance=provenance(),profiles_dir=PROFILES)
        self.assertIn('current module',blocked['error'])
        state=run('status')['result'];self.assertEqual(state['attempts'],{})
        self.assertEqual(state['outline']['cursor'],0)
        run('add',task=f.item('parse'))
        prepared=run('prepare',task_id='parse',catalog=CATALOG,catalog_provenance=provenance(),profiles_dir=PROFILES)['result']
        self.assertEqual(prepared['status'],'dispatch_pending')
        state=run('status')['result'];self.assertEqual(len(state['attempts']),1)
        run('prepare',2,task_id='parse',catalog=CATALOG,catalog_provenance=provenance(),profiles_dir=PROFILES)
        self.assertEqual(len(run('status')['result']['attempts']),1)


if __name__=='__main__':unittest.main()
