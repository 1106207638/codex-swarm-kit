"""Frozen check intentions and append-only observations, not a tool interceptor.

Exit codes and signal text are evidence claims. The controller must compare them
with the host trace. Classification cannot authenticate the reporting process.
"""
import copy

from task_state import StateError, _need, _nonempty

CONTRACT = 'classified-v1'
LEGACY = 'unclassified_legacy'


def validate_plan(plan):
    _need(isinstance(plan, list) and plan, 'check_plan must be a nonempty list')
    ids = set()
    for spec in plan:
        _need(isinstance(spec, dict) and set(spec) ==
              {'id', 'command', 'kind', 'expected_exit_codes', 'expected_signal'}, 'invalid check plan fields')
        for key in ('id', 'command'): _nonempty(spec[key], 'check_plan.'+key)
        _need(spec['id'] not in ids, 'duplicate check id'); ids.add(spec['id'])
        _need(spec['kind'] in ('probe', 'acceptance', 'expected_failure'), 'invalid check kind')
        codes = spec['expected_exit_codes']
        _need(isinstance(codes, list) and codes and all(type(c) is int for c in codes)
              and len(codes) == len(set(codes)), 'expected exit codes must be unique integers')
        _need(isinstance(spec['expected_signal'], str), 'expected_signal must be a string')
        if spec['kind'] == 'acceptance':
            _need(codes == [0], 'positive acceptance requires exit 0')
        if spec['kind'] == 'expected_failure':
            _need(0 not in codes, 'expected failure cannot also accept success')
        if any(c != 0 for c in codes):
            _nonempty(spec['expected_signal'], 'nonzero outcome requires a distinguishing expected_signal')
    return plan


def initialize(receipt, task):
    receipt['check_contract'] = CONTRACT if 'check_plan' in task else LEGACY
    receipt['check_records'] = {}


def classify(spec, result):
    _need(isinstance(result, dict) and set(result) ==
          {'id', 'exit_code', 'error_kind', 'observed_signal', 'evidence_ref'}, 'invalid check result fields')
    _need(result['id'] == spec['id'], 'check id mismatch')
    _nonempty(result['evidence_ref'], 'check evidence_ref')
    _need(result['error_kind'] in (None, 'checker', 'model', 'environment', 'permission'), 'invalid error_kind')
    _need(result['exit_code'] is None or type(result['exit_code']) is int, 'exit_code must be integer or null')
    _need(isinstance(result['observed_signal'], str), 'observed_signal must be a string')
    error = result['error_kind']
    if error:
        return {'outcome': 'error', 'category': error if error != 'permission' else 'environment',
                'requires_controller': True}
    _need(result['exit_code'] is not None, 'normal result requires an exit code')
    signal_matches = (spec['kind'] == 'probe' and result['exit_code'] == 0 or
                      not spec['expected_signal'] or spec['expected_signal'] in result['observed_signal'])
    if result['exit_code'] in spec['expected_exit_codes'] and signal_matches:
        return {'outcome': 'observed' if spec['kind'] == 'probe' else 'passed',
                'category': None, 'requires_controller': False}
    # A mismatching discovery or expected-error signature does not prove a
    # product defect. It needs controller investigation, never automatic repair.
    category = 'validation' if spec['kind'] == 'acceptance' else 'checker'
    return {'outcome': 'failed' if category == 'validation' else 'unknown',
            'category': category, 'requires_controller': True}


def contains_failure(task, results):
    """Whether this submission (not retained history) has an attributable red."""
    if not isinstance(results,list) or 'check_plan' not in task: return False
    specs = {s['id']:s for s in validate_plan(task['check_plan'])}
    for row in results:
        if not isinstance(row,dict) or not isinstance(row.get('id'),str) or row['id'] not in specs: continue
        try:
            if classify(specs[row['id']],row)['requires_controller']: return True
        except StateError:
            continue
    return False


def record(receipt, task, results, *, failure_only=False):
    _need(receipt.get('check_contract') == CONTRACT, 'receipt has no classified check contract')
    specs = {s['id']: s for s in validate_plan(task['check_plan'])}
    _need(isinstance(results, list) and results, 'check_results must be a nonempty list')
    errors = []
    observations = receipt.setdefault('check_observations', [])
    for result in results:
        try:
            _need(isinstance(result, dict) and isinstance(result.get('id'), str), 'check result id required')
            cid = result['id']
            _need(cid in specs, 'unknown result id')
            verdict = classify(specs[cid], result)
            if failure_only and not verdict['requires_controller']:
                errors.append('failing worker cannot submit green observations')
                continue
            row = {'result': copy.deepcopy(result), 'verdict': verdict}
            old = receipt['check_records'].get(cid)
            if row not in observations: observations.append(row)
            if old is not None and old != row:
                errors.append('conflicting repeat observation for '+cid)
            else:
                receipt['check_records'][cid] = row
        except StateError as exc:
            errors.append(str(exc))
    failures = [r for r in observations if r['verdict']['requires_controller']]
    if errors and not failures:
        raise StateError('; '.join(errors))
    if errors: receipt.setdefault('check_protocol_errors', []).extend(errors)
    # Model errors always preserve the stronger human-only lock.
    failures.sort(key=lambda r: r['verdict']['category'] != 'model')
    return failures[0] if failures else None


def register_failure(task_record, receipt, failure):
    rid = receipt.get('attempt_id', receipt.get('direct_id'))
    history = task_record.setdefault('check_failure_history', [])
    for row in history:
        if row['receipt_id'] == rid and failure['verdict']['category'] == 'model':
            row['category'] = 'model'
    if not any(r['receipt_id'] == rid for r in history):
        history.append({'receipt_id': rid, 'category': failure['verdict']['category'],
                        'success_generation': task_record.get('check_success_generation', 0)})


def register_success(task_record, receipt_id):
    successes = task_record.setdefault('check_success_receipts', [])
    if receipt_id not in successes:
        successes.append(receipt_id)
        task_record['check_success_generation'] = task_record.get('check_success_generation', 0) + 1


def initialize_history(state, task_id):
    """Seed legacy outcomes from committed event order, preserving old receipts."""
    record = state['tasks'][task_id]
    if record.get('check_history_initialized'): return
    # Pre-install candidate histories are retained, never erased or guessed.
    if 'check_failure_history' not in record:
        record['check_failure_history'] = []
        historical_auth = None
        for event in state['events']:
            data = event['data']
            attempt = state['attempts'].get(data.get('attempt_id'), {})
            if attempt.get('task_id') == task_id:
                if event['type'] == 'attempt_failed':
                    register_failure(record, attempt, {'verdict': {'category': data['category']}})
                elif event['type'] == 'attempt_verified':
                    register_success(record, attempt['attempt_id'])
            if event['type'] == 'direct_completed' and data.get('task_id') == task_id:
                register_success(record, data['direct_id'])
            if event['type'] == 'retry_authorized' and data.get('task_id') == task_id:
                historical_auth = data
            if event['type'] in ('dispatch_prepared','direct_prepared') and data.get('task_id') == task_id and record['check_failure_history']:
                latest = record['check_failure_history'][-1]['receipt_id']
                auth = legacy_authorization(historical_auth,latest)
                if auth:
                    probe = copy.deepcopy(record)
                    probe['check_retry_authorization'] = auth
                    probe['resolved_check_failure'] = None
                    try: retry_gate(probe)
                    except StateError: pass
                    else: record['resolved_check_failure'] = latest
        history = record['check_failure_history']
        if history and 'check_retry_authorization' not in record:
            latest = history[-1]['receipt_id']
            old_attempt = state['attempts'].get(latest,{})
            all_attempts = [a for a in state['attempts'].values() if a['task_id'] == task_id]
            newest = max(all_attempts,key=lambda a:a['attempt_number'],default={})
            auth = legacy_authorization(record.get('retry_authorization'),latest)
            if (auth and newest.get('attempt_id') == latest and old_attempt.get('worker_stopped') is True
                    and old_attempt.get('status') in ('failed','stopped','no_spawn')):
                record['check_retry_authorization'] = auth
    record['check_history_initialized'] = True


def legacy_authorization(value, receipt_id):
    if not isinstance(value,dict) or value.get('previous_attempt_id') != receipt_id: return None
    reason = value.get('controller_reason',value.get('root_cause_and_fix'))
    if not isinstance(reason,str) or not reason.strip() or type(value.get('human_authorized')) is not bool: return None
    return {'receipt_id':receipt_id,'controller_reason':reason,'human_authorized':value['human_authorized'],
            'source':'matching retained legacy retry decision'}


def retry_gate(task_record):
    history = task_record.get('check_failure_history', [])
    if not history or task_record.get('resolved_check_failure') == history[-1]['receipt_id']: return
    latest = history[-1]
    if latest.get('success_generation', 0) < task_record.get('check_success_generation', 0): return
    auth = task_record.get('check_retry_authorization') or {}
    same_twice = (len(history) >= 2 and latest['category'] in ('validation', 'checker') and
                  history[-2]['category'] == latest['category'] and
                  history[-2].get('success_generation', 0) == latest.get('success_generation', 0))
    if latest['category'] == 'model' or same_twice:
        _need(auth.get('receipt_id') == latest['receipt_id'] and auth.get('human_authorized') is True,
              'model error or two same-category classified failures require human authorization')
    _need(auth.get('receipt_id') == latest['receipt_id'] and auth.get('controller_reason'),
          'classified failure requires controller retry decision after stopped evidence')


def consume_retry(task_record):
    history = task_record.get('check_failure_history', [])
    if history: task_record['resolved_check_failure'] = history[-1]['receipt_id']


def replay(receipt, results, task):
    _need(isinstance(results, list) and results, 'check replay needs results')
    specs = {s['id']:s for s in validate_plan(task['check_plan'])}
    seen = set()
    for row in results:
        _need(isinstance(row, dict) and isinstance(row.get('id'), str), 'invalid check replay')
        cid = row['id']
        _need(cid in specs, 'unknown check replay id')
        classify(specs[cid], row)
        _need(cid not in seen and receipt.get('check_records', {}).get(cid, {}).get('result') == row,
              'conflicting completed check replay')
        seen.add(cid)


def accept(receipt, task):
    if receipt.get('check_contract', LEGACY) == LEGACY:
        return LEGACY
    _need(receipt.get('check_contract') == CONTRACT, 'unknown check contract')
    specs = validate_plan(task['check_plan'])
    records = receipt.get('check_records', {})
    _need(not receipt.get('check_protocol_errors'), 'unresolved check protocol errors')
    _need(not any(r['verdict']['requires_controller'] for r in receipt.get('check_observations', [])),
          'a subsequent failed observation remains unresolved')
    _need(set(records) == {s['id'] for s in specs}, 'required checks missing')
    _need(any(s['kind'] != 'probe' for s in specs), 'probes alone cannot prove acceptance')
    for spec in specs:
        row = records[spec['id']]
        _need(classify(spec, row['result']) == row['verdict'], 'stored check verdict mismatch')
        _need(not row['verdict']['requires_controller'], 'unresolved check failure requires controller decision')
    return CONTRACT
