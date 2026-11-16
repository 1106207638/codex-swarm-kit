import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import audit_tools as a
from check_protocol import classify


class AuditTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.file = self.root/'中文 空格.txt'
        self.file.write_bytes(b'line1\nline2\n')
        self.digest = hashlib.sha256(self.file.read_bytes()).hexdigest()
        self.spec = {'format': a.INPUT_FORMAT, 'read_paths': [str(self.root)],
                     'snapshot_files': {str(self.file): self.digest}, 'artifacts': {},
                     'metadata': {'outline_ref': 'outside/missing-plan.md', 'revision': 1}}

    def test_metadata_is_not_opened_and_compatible_hash_map(self):
        self.assertEqual(a.audit_files(self.spec)['status'], 'passed')
        self.spec['artifacts'] = {str(self.file): self.digest}
        self.assertEqual(a.audit_files(self.spec)['status'], 'passed')

    def test_entire_spec_validated_before_file_reads(self):
        self.spec['artifacts'] = {str(self.root.parent/'outside'): '0'*64}
        with patch.object(Path, 'read_bytes', side_effect=AssertionError('must not read')) as read:
            self.assertEqual(a.audit_files(self.spec)['status'], 'checker_error')
            read.assert_not_called()

    def test_missing_and_changed_are_evidence_not_product(self):
        self.file.write_text('changed')
        self.assertEqual(a.audit_files(self.spec)['status'], 'evidence_changed')
        self.spec['snapshot_files'][str(self.root/'absent')] = '0'*64
        value = a.audit_files(self.spec)
        self.assertEqual(value['status'], 'evidence_unavailable')
        self.assertTrue(any('Changed:' in x for x in value['details']))

    def test_invalid_maps_hashes_metadata_and_conflicts(self):
        for field, value in [('snapshot_files', []), ('metadata', []), ('read_paths', []),
                             ('snapshot_files', {str(self.file): True}),
                             ('artifacts', {str(self.file): '0'*64})]:
            with self.subTest(field=field, value=value):
                spec = dict(self.spec, **{field: value})
                self.assertEqual(a.audit_files(spec)['status'], 'checker_error')
        self.spec['snapshot_files'] = {}
        self.assertEqual(a.audit_files(self.spec)['status'], 'checker_error')

    def test_canonical_duplicates_rejected(self):
        self.spec['snapshot_files'][str(self.root/'sub/../中文 空格.txt')] = self.digest
        self.assertEqual(a.audit_files(self.spec)['status'], 'checker_error')

    def test_checker_contract(self):
        self.assertEqual(a.run_check(lambda: True)['status'], 'passed')
        self.assertEqual(a.run_check(lambda: False)['status'], 'product_failed')
        for val in [None, 1, 'true', {'passed': True}]:
            self.assertEqual(a.run_check(lambda: val)['status'], 'checker_error')
        for exception, expected in [(a.ProductFailure('mismatch'), 'product_failed'),
                                    (a.ProductFailure(' \n '), 'product_failed'),
                                    (AssertionError('script bug'), 'checker_error'),
                                    (RuntimeError('broken'), 'checker_error')]:
            def check(): raise exception
            self.assertEqual(a.run_check(check)['status'], expected)

    def test_read_permission_and_checker_fault_are_distinct(self):
        for exc, status in [(PermissionError('denied'), 'evidence_unavailable'),
                            (RuntimeError('broken reader'), 'checker_error')]:
            with patch.object(Path, 'read_bytes', side_effect=exc):
                self.assertEqual(a.audit_files(self.spec)['status'], status)

    def test_normalize_strict_status_exit_contract(self):
        for status, code in a.CODES.items():
            report = a.report(status, 'detail')
            self.assertEqual(a.normalize(json.dumps(report), code)['status'], status)
            self.assertEqual(a.normalize(report, (code+1)%3)['status'], 'checker_error')
        for code in [False, 0.0, None, '0']:
            self.assertEqual(a.normalize(a.report('passed', 'ok'), code)['status'], 'checker_error')
        for value in ['not-json', '[]', '{"x":1,"x":2}', '{"a":NaN}', {},
                      {'format': a.FORMAT, 'status': [], 'details': ['x']},
                      {'format': a.FORMAT, 'status': 'passed', 'details': []}]:
            self.assertEqual(a.normalize(value, 0)['status'], 'checker_error')

    def test_existing_result_contract_and_real_exit_preserved(self):
        plan = {'id': 'audit', 'command': 'fixed', 'kind': 'acceptance',
                'expected_exit_codes': [0], 'expected_signal': ''}
        for status, expected in [('passed', None), ('product_failed', 'validation'),
                                 ('checker_error', 'checker'), ('evidence_changed', 'environment'),
                                 ('evidence_unavailable', 'environment')]:
            value = a.to_check_result('audit', a.report(status, 'detail'), a.CODES[status], 'host.json')
            self.assertEqual(classify(plan, value)['category'], expected)
        bad = a.to_check_result('audit', a.report('passed', 'ok'), 7, 'host.json')
        self.assertEqual(bad['exit_code'], 7)
        self.assertEqual(classify(plan, bad)['category'], 'checker')

    def test_cli_real_exit_and_output(self):
        spec = self.root/'input.json'
        spec.write_text(json.dumps(self.spec), encoding='utf-8')
        cmd = [sys.executable, '-B', str(Path(a.__file__)), '--spec', str(spec)]
        proc = subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8')
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(a.normalize(proc.stdout, proc.returncode)['status'], 'passed')
        self.file.write_text('changed')
        proc = subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8')
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(a.normalize(proc.stdout, proc.returncode)['status'], 'evidence_changed')
        spec.write_text('{"x":1,"x":2}', encoding='utf-8')
        proc = subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8')
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(a.normalize(proc.stdout, proc.returncode)['status'], 'checker_error')

    def test_deep_json_and_invalid_cli_path_are_checker_errors(self):
        deep = '['*2000+'0'+']'*2000
        self.assertEqual(a.normalize(deep, 0)['status'], 'checker_error')
        value = a.to_check_result('audit', deep, 1, 'actual-host.json')
        self.assertEqual(value['error_kind'], 'checker')
        self.assertEqual(value['exit_code'], 1)
        spec = self.root/'deep.json'
        spec.write_text(deep, encoding='utf-8')
        for path, status in [(str(spec), 'checker_error'), ('relative.json', 'checker_error'),
                             (str(self.root/'missing'), 'evidence_unavailable')]:
            proc = subprocess.run([sys.executable, '-B', str(Path(a.__file__)), '--spec', path],
                                  capture_output=True, text=True, encoding='utf-8')
            self.assertEqual(proc.returncode, 2, proc.stderr)
            self.assertEqual(a.normalize(proc.stdout, proc.returncode)['status'], status)
        spec.write_bytes(b'\xff')
        proc = subprocess.run([sys.executable, '-B', str(Path(a.__file__)), '--spec', str(spec)],
                              capture_output=True, text=True, encoding='utf-8')
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(a.normalize(proc.stdout, proc.returncode)['status'], 'checker_error')

    def test_unicode_diagnostics_survive_strict_stdout_and_adapter(self):
        spec = self.root/'unicode.json'
        bad = '{"\\ud800":1,"\\ud800":2}'
        spec.write_text(bad, encoding='ascii')
        value = a.to_check_result('audit', bad, 2, 'host.json')
        self.assertEqual(value['error_kind'], 'checker')
        value['observed_signal'].encode('ascii', errors='strict')
        self.assertIn('\ud800', json.loads(value['observed_signal'])['details'][0])
        for encoding in ('utf-8:strict', 'ascii:strict'):
            env = dict(os.environ, PYTHONIOENCODING=encoding)
            cmd = [sys.executable, '-B', str(Path(a.__file__)), '--spec', str(spec)]
            proc = subprocess.run(cmd, env=env, capture_output=True, text=True, encoding='ascii')
            self.assertEqual(proc.returncode, 2, proc.stderr)
            self.assertEqual(a.normalize(proc.stdout, proc.returncode)['status'], 'checker_error')
            self.assertIn('\ud800', json.loads(proc.stdout)['details'][0])
        # Ordinary Chinese diagnostic paths must also survive an ASCII pipe.
        spec.write_text(json.dumps(self.spec), encoding='ascii')
        self.file.write_text('changed')
        proc = subprocess.run(cmd, env=env, capture_output=True, text=True, encoding='ascii')
        self.assertEqual(proc.returncode, 2, proc.stderr)
        result = a.normalize(proc.stdout, proc.returncode)
        self.assertEqual(result['status'], 'evidence_changed')
        self.assertIn('中文 空格.txt', result['details'][0])


if __name__ == '__main__': unittest.main()
