"""Read-only evidence checks and typed checker results.

Hashes prove observed bytes, not product correctness or authorization. Metadata
is never opened as a file. Scope declarations are caller supplied, not OS locks.
The controller must retain and check actual process output and exit status.
"""
import argparse
import hashlib
import json
import re
from pathlib import Path

FORMAT = 'codex-audit-v1'
INPUT_FORMAT = 'codex-audit-input-v1'
CODES = {'passed': 0, 'product_failed': 1, 'checker_error': 2,
         'evidence_unavailable': 2, 'evidence_changed': 2}


class ProductFailure(Exception):
    """An explicit product assertion; other exceptions are checker faults."""


def _require(condition, message):
    if not condition:
        raise ValueError(message)


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        _require(key not in result, 'duplicate JSON key: '+key)
        result[key] = value
    return result


def _constant(value):
    raise ValueError('non-finite JSON number: '+value)


def loads(text):
    return json.loads(text, object_pairs_hook=_pairs, parse_constant=_constant)


def report(status, *details):
    _require(status in CODES, 'unknown audit status')
    _require(details and all(isinstance(x, str) and x.strip() for x in details), 'audit details required')
    return {'format': FORMAT, 'status': status, 'details': list(details)}


def validate_report(value):
    _require(isinstance(value, dict) and set(value) == {'format', 'status', 'details'}, 'invalid audit report fields')
    _require(value['format'] == FORMAT, 'unsupported audit report')
    _require(isinstance(value['status'], str) and value['status'] in CODES, 'invalid audit status')
    _require(isinstance(value['details'], list), 'audit details must be a list')
    report(value['status'], *value['details'])
    return value


def normalize(output, exit_code):
    """Use the actual process exit code. Never trust semantic success alone."""
    try:
        value = loads(output) if isinstance(output, str) else output
        validate_report(value)
        _require(type(exit_code) is int, 'actual process exit code must be an integer')
        _require(exit_code == CODES[value['status']], 'process exit code contradicts audit status')
        return value
    except (ValueError, TypeError, KeyError, RecursionError) as exc:
        return report('checker_error', str(exc))


def to_check_result(check_id, output, exit_code, evidence_ref):
    """Adapt to classified-v1 without changing its existing result schema."""
    _require(isinstance(check_id, str) and check_id.strip(), 'check id required')
    _require(isinstance(evidence_ref, str) and evidence_ref.strip(), 'raw host evidence reference required')
    value = normalize(output, exit_code)
    error = {'checker_error': 'checker', 'evidence_unavailable': 'environment',
             'evidence_changed': 'environment'}.get(value['status'])
    return {'id': check_id, 'exit_code': exit_code if type(exit_code) is int else None,
            'error_kind': error, 'observed_signal': json.dumps(value, ensure_ascii=True),
            'evidence_ref': evidence_ref}


def run_check(check):
    """Run an in-process explicit boolean check, never arbitrary JSON commands."""
    try:
        value = check()
        _require(type(value) is bool, 'checker must return an explicit boolean')
        return report('passed' if value else 'product_failed', 'Product check returned '+str(value))
    except ProductFailure as exc:
        return report('product_failed', str(exc).strip() or 'Explicit product mismatch')
    except Exception as exc:
        return report('checker_error', type(exc).__name__+': '+str(exc))


def _path(value):
    _require(isinstance(value, str) and Path(value).is_absolute(), 'absolute file path required')
    return Path(value).resolve()


def _spec(value):
    _require(isinstance(value, dict) and set(value) ==
             {'format', 'read_paths', 'snapshot_files', 'artifacts', 'metadata'}, 'invalid audit input fields')
    _require(value['format'] == INPUT_FORMAT, 'unsupported audit input')
    _require(isinstance(value['metadata'], dict), 'metadata must be an object')
    # Metadata must be JSON data, but even an outline_ref naming a file grants
    # no read permission and contributes no file evidence.
    json.dumps(value['metadata'], allow_nan=False)
    _require(isinstance(value['read_paths'], list) and value['read_paths'], 'read_paths required')
    roots = [_path(p) for p in value['read_paths']]
    _require(len(set(roots)) == len(roots), 'duplicate read path')
    files = {}
    for field in ('snapshot_files', 'artifacts'):
        mapping = value[field]
        _require(isinstance(mapping, dict), field+' must be a path/hash map')
        seen = set()
        for name, digest in mapping.items():
            path = _path(name)
            _require(path not in seen, 'duplicate canonical file in '+field)
            seen.add(path)
            _require(any(path == root or path.is_relative_to(root) for root in roots), 'file outside declared read scope: '+name)
            _require(isinstance(digest, str) and re.fullmatch('[0-9a-f]{64}', digest), 'invalid SHA-256: '+name)
            _require(path not in files or files[path] == digest, 'conflicting file hashes: '+name)
            files[path] = digest
    _require(files, 'file evidence required; metadata alone cannot pass')
    return files


def audit_files(spec):
    try:
        files = _spec(spec)  # Validate the entire contract before opening files.
    except Exception as exc:
        return report('checker_error', type(exc).__name__+': '+str(exc))
    missing, changed = [], []
    for path, expected in files.items():
        try:
            actual = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError as exc:
            missing.append(str(path)+': '+type(exc).__name__)
            continue
        except Exception as exc:
            return report('checker_error', type(exc).__name__+': '+str(exc))
        if actual != expected:
            changed.append(str(path))
    if missing:
        return report('evidence_unavailable', *missing, *['Changed: '+p for p in changed])
    if changed:
        return report('evidence_changed', *changed)
    return report('passed', str(len(files))+' file hashes match; product semantics require separate checks')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--spec', required=True)
    args = parser.parse_args(argv)
    try:
        path = _path(args.spec)
        try:
            text = path.read_text(encoding='utf-8')
        except OSError as exc:
            value = report('evidence_unavailable', type(exc).__name__+': '+str(exc))
        else:
            value = audit_files(loads(text))
    except (ValueError, TypeError, RecursionError, RuntimeError) as exc:
        value = report('checker_error', type(exc).__name__+': '+str(exc))
    # Escaped JSON preserves Unicode values while remaining safe even for lone
    # surrogate diagnostics and ASCII/legacy Windows redirected output streams.
    print(json.dumps(value, ensure_ascii=True))
    return CODES[value['status']]


if __name__ == '__main__':
    raise SystemExit(main())
