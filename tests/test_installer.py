import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import shutil
import stat

spec=importlib.util.spec_from_file_location('kit_setup',Path(__file__).resolve().parents[1]/'setup.py')
kit=importlib.util.module_from_spec(spec);spec.loader.exec_module(kit)

class InstallerTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name).resolve();self.home=self.root/"Codex 中文 ' ‘ home"
        self.source=self.root/'source';self.source.mkdir()
        self.files=sorted(kit.REQUIRED)
        for name in self.files:
            p=self.source/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_text('fixture '+name,encoding='utf-8')
        self.manifest()

    def manifest(self,version='1.0.0'):
        self.m={'format':kit.FORMAT,'version':version,'files':{n:kit.sha((self.source/n).read_bytes()) for n in self.files}}
        (self.source/'manifest.json').write_bytes(kit.json_bytes(self.m))

    def install(self,**kwargs):return kit.install(self.home,self.source,**kwargs)
    def stop(self,index):
        if index==0:raise RuntimeError('simulated process interruption')

    def test_preview_no_writes_then_fresh_and_repeat(self):
        self.assertEqual(self.install(preview=True)['action'],'preview');self.assertFalse(self.home.exists())
        receipt=self.install();self.assertEqual(receipt['action'],'installed')
        self.assertTrue(kit.doctor(self.home)['installed'])
        self.assertEqual(self.install()['action'],'already-installed')
        self.assertFalse((self.home/'config.toml').exists())
        self.assertFalse((self.home/'agents').exists())

    def test_preserve_rules_binary_newlines_config_and_restore(self):
        self.home.mkdir();original='用户约束\r\n# rules\nno final newline'.encode('utf-8')
        (self.home/'AGENTS.md').write_bytes(original);(self.home/'config.toml').write_bytes(b'# untouched\r\n')
        receipt=self.install();extra=b'\n# later user edit'
        with (self.home/'AGENTS.md').open('ab') as f:f.write(extra)
        kit.restore(self.home,receipt['transaction'])
        self.assertEqual((self.home/'AGENTS.md').read_bytes(),original+extra)
        self.assertEqual((self.home/'config.toml').read_bytes(),b'# untouched\r\n')
        self.assertFalse(kit.doctor(self.home)['installed'])
        self.assertTrue((self.home/'swarm-kit/releases'/receipt['release']).exists())

    def test_upgrade_restore_chain_and_retained_releases(self):
        first=self.install()
        (self.source/'docs/RULES.md').write_text('updated');self.manifest('1.1.0')
        second=self.install();self.assertNotEqual(first['release'],second['release'])
        with self.assertRaises(kit.InstallError):kit.restore(self.home,first['transaction'])
        kit.restore(self.home,second['transaction']);self.assertEqual(kit.doctor(self.home)['release'],first['release'])
        kit.restore(self.home,first['transaction']);self.assertFalse(kit.doctor(self.home)['installed'])

    def test_original_router_conflict_leaves_bytes(self):
        self.home.mkdir();original=b'custom\n<!-- BEGIN CODEX-UNIVERSAL-ROUTER -->\nold'
        (self.home/'AGENTS.md').write_bytes(original)
        with self.assertRaisesRegex(kit.InstallError,'Existing universal'):self.install()
        self.assertEqual((self.home/'AGENTS.md').read_bytes(),original)
        self.assertFalse((self.home/'swarm-kit/active.json').exists())

    def test_malformed_unowned_and_duplicate_blocks_refused(self):
        self.home.mkdir()
        for data in [kit.BEGIN,kit.END,kit.BEGIN+b'\n'+kit.END+b'\n',kit.BEGIN+kit.END+kit.BEGIN+kit.END]:
            (self.home/'AGENTS.md').write_bytes(data)
            with self.assertRaises(kit.InstallError):self.install()
            self.assertEqual((self.home/'AGENTS.md').read_bytes(),data)

    def test_package_tamper_no_destination(self):
        (self.source/'runtime/routing.py').write_text('tampered')
        with self.assertRaisesRegex(kit.InstallError,'changed'):self.install()
        self.assertFalse(self.home.exists())

    def test_manifest_path_validation(self):
        for name in ['.','../escape.py','runtime/../escape.py','/runtime/foo.py','runtime\\foo.py','runtime/x:ads.py','runtime/CON.py','runtime//x.py','config.toml','runtime/NUL.py','runtime/x./a.py']:
            with self.subTest(name=name):
                with self.assertRaises(kit.InstallError):kit.relative_path(name)
        self.m['files']['runtime/ROUTING.py']=self.m['files']['runtime/routing.py']
        (self.source/'manifest.json').write_bytes(kit.json_bytes(self.m))
        with self.assertRaisesRegex(kit.InstallError,'Case-colliding'):self.install()

    def test_current_release_and_managed_rule_tamper_refused(self):
        receipt=self.install();target=self.home/'swarm-kit/releases'/receipt['release']/'docs/RULES.md'
        target.write_text('external modification')
        for operation in [lambda:kit.doctor(self.home),self.install,lambda:kit.restore(self.home,receipt['transaction'])]:
            with self.assertRaises(kit.InstallError):operation()

    def test_interruption_resume_is_explicit_and_idempotent(self):
        with self.assertRaisesRegex(RuntimeError,'interruption'):self.install(checkpoint=self.stop)
        ids=kit.pending(self.home);self.assertEqual(len(ids),1)
        with self.assertRaisesRegex(kit.InstallError,'Incomplete'):self.install()
        with kit.locked(self.home):kit.resume_locked(self.home,ids[0])
        self.assertTrue(kit.doctor(self.home)['installed'])
        with kit.locked(self.home):self.assertEqual(kit.resume_locked(self.home,ids[0])['status'],'complete')

    def test_interruption_third_value_freezes_before_writes(self):
        with self.assertRaises(RuntimeError):self.install(checkpoint=self.stop)
        tid=kit.pending(self.home)[0];pointer=(self.home/'swarm-kit/active.json').read_bytes()
        (self.home/'AGENTS.md').write_bytes(b'external')
        with kit.locked(self.home):
            with self.assertRaisesRegex(kit.InstallError,'External edit'):kit.resume_locked(self.home,tid)
        self.assertEqual((self.home/'AGENTS.md').read_bytes(),b'external')
        self.assertEqual((self.home/'swarm-kit/active.json').read_bytes(),pointer)

    def test_resume_rechecks_payload_and_backup_integrity(self):
        with self.assertRaises(RuntimeError):self.install(checkpoint=self.stop)
        tid=kit.pending(self.home)[0];active=json.loads((self.home/'swarm-kit/active.json').read_bytes())
        (self.home/'swarm-kit/releases'/active['release']/'docs/RULES.md').write_text('changed')
        with kit.locked(self.home):
            with self.assertRaisesRegex(kit.InstallError,'changed'):kit.resume_locked(self.home,tid)
        self.assertFalse((self.home/'AGENTS.md').exists())

    def test_restore_interruption_can_resume(self):
        original=b'user';self.home.mkdir();(self.home/'AGENTS.md').write_bytes(original)
        receipt=self.install()
        with self.assertRaises(RuntimeError):kit.restore(self.home,receipt['transaction'],checkpoint=self.stop)
        tid=kit.pending(self.home)[0]
        with kit.locked(self.home):kit.resume_locked(self.home,tid)
        self.assertEqual((self.home/'AGENTS.md').read_bytes(),original)
        self.assertFalse(kit.doctor(self.home)['installed'])

    def test_managed_rule_external_edit_no_clobber(self):
        receipt=self.install();path=self.home/'AGENTS.md';value=path.read_bytes().replace(b'Small tasks',b'My tasks');path.write_bytes(value)
        with self.assertRaises(kit.InstallError):kit.restore(self.home,receipt['transaction'])
        self.assertEqual(path.read_bytes(),value)

    def test_active_pointer_path_and_journal_tamper_refused(self):
        receipt=self.install();p=self.home/'swarm-kit/active.json';value=json.loads(p.read_bytes());value['release']='../../external';p.write_bytes(kit.json_bytes(value))
        with self.assertRaisesRegex(kit.InstallError,'Invalid active'):kit.doctor(self.home)

    def test_actual_process_lock(self):
        code="import importlib.util,sys;from pathlib import Path;s=importlib.util.spec_from_file_location('k',sys.argv[1]);k=importlib.util.module_from_spec(s);s.loader.exec_module(k);\nwith k.locked(Path(sys.argv[2])):pass"
        with kit.locked(self.home):
            p=subprocess.run([sys.executable,'-B','-c',code,str(Path(kit.__file__)),str(self.home)],capture_output=True,text=True)
            self.assertNotEqual(p.returncode,0);self.assertIn('Another installer',p.stderr)
        with kit.locked(self.home):pass

    def test_symlink_target_refused(self):
        target=self.root/'elsewhere';target.mkdir();self.home.mkdir()
        link=self.home/'swarm-kit'
        try:link.symlink_to(target,target_is_directory=True)
        except OSError:self.skipTest('OS does not permit fixture symlinks')
        with self.assertRaisesRegex(kit.InstallError,'Linked/reparse'):self.install()
        self.assertEqual(list(target.iterdir()),[])

    def test_external_save_between_preflight_and_journal_is_not_adopted(self):
        self.home.mkdir();target=self.home/'AGENTS.md';target.write_bytes(b'before')
        original=kit.make_transaction
        def edit_then_commit(*args,**kwargs):
            target.write_bytes(b'user just saved')
            return original(*args,**kwargs)
        with patch.object(kit,'make_transaction',side_effect=edit_then_commit):
            with self.assertRaisesRegex(kit.InstallError,'External edit'):self.install()
        self.assertEqual(target.read_bytes(),b'user just saved')
        self.assertFalse((self.home/'swarm-kit/active.json').exists())

    def test_restore_does_not_adopt_external_save(self):
        receipt=self.install();target=self.home/'AGENTS.md';prior=target.read_bytes();original=kit.make_transaction
        def edit_then_commit(*args,**kwargs):
            target.write_bytes(prior+b'user just saved')
            return original(*args,**kwargs)
        with patch.object(kit,'make_transaction',side_effect=edit_then_commit):
            with self.assertRaisesRegex(kit.InstallError,'External edit'):kit.restore(self.home,receipt['transaction'])
        self.assertEqual(target.read_bytes(),prior+b'user just saved')

    def test_damaged_backup_rejected_and_no_new_transaction(self):
        self.home.mkdir();(self.home/'AGENTS.md').write_bytes(b'private old rules')
        receipt=self.install();path=self.home/'swarm-kit/transactions'/receipt['transaction']/'journal.json';record=json.loads(path.read_bytes())
        next(r for r in record['changes'] if r['path']=='AGENTS.md')['before']=kit.encode(b'damaged')
        path.write_bytes(kit.json_bytes(record));before=(self.home/'AGENTS.md').read_bytes()
        with self.assertRaisesRegex(kit.InstallError,'Damaged transaction'):kit.restore(self.home,receipt['transaction'])
        self.assertEqual((self.home/'AGENTS.md').read_bytes(),before)
        self.assertEqual(len(list(path.parent.parent.glob('*/journal.json'))),1)

    def test_consistent_hash_but_inconsistent_backup_pair_rejected(self):
        receipt=self.install();path=self.home/'swarm-kit/transactions'/receipt['transaction']/'journal.json';record=json.loads(path.read_bytes())
        row=next(r for r in record['changes'] if r['path']=='AGENTS.md');row['before']=row['after'];row['before_sha256']=row['after_sha256']
        path.write_bytes(kit.json_bytes(record))
        with self.assertRaisesRegex(kit.InstallError,'inactive pointer'):kit.restore(self.home,receipt['transaction'])

    def test_confirmation_binds_package_and_target(self):
        plan=self.install(preview=True)
        (self.source/'docs/RULES.md').write_text('different');self.manifest('2.0.0')
        with self.assertRaisesRegex(kit.InstallError,'since confirmation'):kit.install(self.home,self.source,approved=plan['binding'])
        self.assertFalse((self.home/'AGENTS.md').exists())
        plan=self.install(preview=True);(self.home/'AGENTS.md').write_bytes(b'new user content')
        with self.assertRaisesRegex(kit.InstallError,'since confirmation'):kit.install(self.home,self.source,approved=plan['binding'])
        self.assertEqual((self.home/'AGENTS.md').read_bytes(),b'new user content')

    def test_resume_override_and_confirmation_change_refused(self):
        with self.assertRaises(RuntimeError):self.install(checkpoint=self.stop)
        tid=kit.pending(self.home)[0];plan=kit.resume_preview(self.home,tid)
        (self.home/'AGENTS.override.md').write_text('override')
        with kit.locked(self.home):
            with self.assertRaisesRegex(kit.InstallError,'override'):kit.resume_locked(self.home,tid,approved=plan)
        self.assertFalse((self.home/'AGENTS.md').exists())

    def test_bad_json_and_missing_journal_use_structured_cli_errors(self):
        for value in [None,b'[]',b'null',b'']:
            with self.assertRaises(kit.InstallError):kit.parse(value)
        cmd=[sys.executable,'-B',str(Path(kit.__file__)),'resume','--codex-home',str(self.home),'--transaction','0'*32,'--yes']
        p=subprocess.run(cmd,capture_output=True,text=True,encoding='utf-8');self.assertEqual(p.returncode,2,p.stderr);self.assertFalse(json.loads(p.stdout)['ok']);self.assertEqual(p.stderr,'')

    def test_html_markers_in_display_path_cannot_break_rules(self):
        # Such names are legal on POSIX, but not Windows. Construction is pure.
        data=kit.block(Path('/tmp/<!-- BEGIN CODEX-SWARM-KIT -->/release'))
        self.assertEqual(kit.section(data)[2],data)

    @unittest.skipIf(os.name=='nt','POSIX file mode semantics require POSIX host; macOS pending')
    def test_posix_private_backup_and_preserved_rules_mode(self):
        self.home.mkdir();p=self.home/'AGENTS.md';p.write_bytes(b'private');p.chmod(0o600)
        old=os.umask(0o022)
        try:receipt=self.install()
        finally:os.umask(old)
        self.assertEqual(stat.S_IMODE(p.stat().st_mode),0o600)
        journal=self.home/'swarm-kit/transactions'/receipt['transaction']/'journal.json'
        self.assertEqual(stat.S_IMODE(journal.stat().st_mode),0o600)
        kit.restore(self.home,receipt['transaction']);self.assertEqual(stat.S_IMODE(p.stat().st_mode),0o600)

    def test_real_source_through_public_cli(self):
        project=Path(kit.__file__).parent
        for name in self.files:shutil.copyfile(project/name,self.source/name)
        shutil.copyfile(project/'setup.py',self.source/'setup.py');self.manifest()
        def cli(action,*args):
            p=subprocess.run([sys.executable,'-B','-X','utf8',str(self.source/'setup.py'),action,'--codex-home',str(self.home),*args],capture_output=True,text=True,encoding='utf-8')
            self.assertEqual(p.returncode,0,p.stdout+p.stderr);return json.loads(p.stdout)['result']
        cli('preview');self.assertFalse(self.home.exists())
        receipt=cli('install','--yes');self.assertTrue(cli('doctor')['installed'])
        installed=self.home/'swarm-kit/releases'/receipt['release']/'runtime/routing.py'
        p=subprocess.run([sys.executable,'-B','-X','utf8',str(installed)],input='{"op":"template"}',capture_output=True,text=True,encoding='utf-8')
        self.assertEqual(p.returncode,0,p.stdout+p.stderr);self.assertTrue(json.loads(p.stdout)['ok'])
        cli('restore','--transaction',receipt['transaction'],'--yes');self.assertFalse(cli('doctor')['installed'])

    def test_save_during_temp_preparation_is_preserved_for_install_and_restore(self):
        self.home.mkdir();target=self.home/'AGENTS.md';target.write_bytes(b'original rules')
        original_atomic=kit.atomic;original_fsync=kit.os.fsync
        def inject(path,data,**kwargs):
            if path==target:
                def sync_then_save(fd):
                    original_fsync(fd);target.write_bytes(b'user saved during temp fsync')
                with patch.object(kit.os,'fsync',side_effect=sync_then_save):return original_atomic(path,data,**kwargs)
            return original_atomic(path,data,**kwargs)
        with patch.object(kit,'atomic',side_effect=inject):
            with self.assertRaisesRegex(kit.InstallError,'preparing replacement'):self.install()
        self.assertEqual(target.read_bytes(),b'user saved during temp fsync')
        self.assertEqual(len(kit.pending(self.home)),1)
        # A separate target verifies the same write path for restore.
        self.home=self.root/'restore-target';receipt=self.install();target=self.home/'AGENTS.md'
        with patch.object(kit,'atomic',side_effect=inject):
            with self.assertRaisesRegex(kit.InstallError,'preparing replacement'):kit.restore(self.home,receipt['transaction'])
        self.assertEqual(target.read_bytes(),b'user saved during temp fsync')
        self.assertEqual(len(kit.pending(self.home)),1)

    def test_case_alias_parent_directories_rejected_before_destination(self):
        for name in ['runtime/Tools/one.py','runtime/tools/two.py']:
            p=self.source/name;p.parent.mkdir(exist_ok=True);p.write_text('fixture');self.files.append(name)
        self.manifest()
        with self.assertRaisesRegex(kit.InstallError,'Case-alias'):self.install()
        self.assertFalse(self.home.exists())

    def test_reserved_word_in_home_roundtrip(self):
        self.home=self.root/'CODEX-UNIVERSAL-ROUTER'/'home'
        self.assertEqual(self.install(preview=True)['action'],'preview')
        receipt=self.install();self.assertTrue(kit.doctor(self.home)['installed'])
        kit.restore(self.home,receipt['transaction']);self.assertFalse(kit.doctor(self.home)['installed'])

    def test_dangling_override_blocks_install_and_resume(self):
        self.home.mkdir();link=self.home/'AGENTS.override.md'
        try:link.symlink_to(self.root/'missing-target')
        except OSError:self.skipTest('OS does not permit fixture symlinks')
        with self.assertRaisesRegex(kit.InstallError,'override'):self.install()
        self.assertFalse((self.home/'AGENTS.md').exists())
        self.home=self.root/'interrupted-target'
        with self.assertRaises(RuntimeError):self.install(checkpoint=self.stop)
        tid=kit.pending(self.home)[0];(self.home/'AGENTS.override.md').symlink_to(self.root/'missing-target')
        with kit.locked(self.home):
            with self.assertRaisesRegex(kit.InstallError,'override'):kit.resume_locked(self.home,tid)
        self.assertFalse((self.home/'AGENTS.md').exists())

if __name__=='__main__':unittest.main()
