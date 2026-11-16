import os
import shlex
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch
from platform_runtime import codex_home, python_command

class PortableTests(unittest.TestCase):
    def test_custom_home_and_relative_rejected(self):
        with patch.dict(os.environ, {'CODEX_HOME': str(Path.cwd() / '中文 home')}):
            self.assertEqual(codex_home(), (Path.cwd() / '中文 home').resolve())
        with patch.dict(os.environ, {'CODEX_HOME': 'relative'}):
            with self.assertRaises(ValueError): codex_home()

    def test_posix_roundtrip(self):
        args=['中文 spaces', "apostrophe'", '$(touch danger)', '`echo nope`', '"quote"', '']
        cmd,shell=python_command(args, platform='posix',executable='/usr/bin/python3')
        self.assertEqual(shlex.split(cmd), ['/usr/bin/python3', *args])
        self.assertEqual(shell, '/bin/sh')

    @unittest.skipUnless(os.name=='nt','Windows actual PowerShell test')
    def test_windows_actual_inert_arguments(self):
        import json
        args=['中文 spaces', "apostrophe'", '$(Write-Output danger)', '`echo nope`', '"quote"']
        # PowerShell 7 preserves embedded double quotes in native arguments.
        # File paths on Windows cannot contain double quotes; test valid paths here.
        args=args[:-1]+['弯引号‘’测试', 'x’; Write-Output should-not-run; ‘']
        cmd,_=python_command(['-X','utf8','-c','import sys,json; print(json.dumps(sys.argv[1:]))',*args])
        proc=subprocess.run(['powershell','-NoProfile','-Command',cmd],capture_output=True,text=True)
        self.assertEqual(proc.returncode,0,proc.stderr)
        self.assertEqual(json.loads(proc.stdout),args)

    def test_unknown_platform_and_multiline_rejected(self):
        for kwargs in [{'platform':'mystery'},{'executable':'bad\npath'}]:
            with self.assertRaises(ValueError):python_command([],**kwargs)

    def test_default_home(self):
        with patch.dict(os.environ, {}, clear=True), patch('platform_runtime.Path.home',return_value=Path.cwd()):
            self.assertEqual(codex_home(),(Path.cwd()/'.codex').resolve())

    def test_session_must_belong_to_selected_home(self):
        import tempfile,uuid
        from main_switch import _session_path
        from task_state import StateError
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);tid=str(uuid.uuid4())
            logs=[]
            for name in ['selected','other']:
                p=root/name/'sessions'/('rollout-fixture-'+tid+'.jsonl');p.parent.mkdir(parents=True);p.write_text('{}');logs.append(p)
            with patch.dict(os.environ,{'CODEX_HOME':str(root/'selected')}):
                self.assertEqual(_session_path(str(logs[0]),tid),logs[0].resolve())
                with self.assertRaises(StateError):_session_path(str(logs[1]),tid)
