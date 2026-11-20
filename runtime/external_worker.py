"""Controller-mediated MCP worker integration. This module never sends requests."""
import copy
import json
import re

from external_contract import PROTOCOL, canonical_bytes, check_result, digest, object_fields, text
from external_contract import validate_request, validate_result, validate_task as validate_text_task
from task_state import StateError, _need, _nonempty, _evidence

TOOLS = {
    'describe': 'mcp__local_worker__describe_protocol',
    'submit': 'mcp__local_worker__submit_task',
    'get': 'mcp__local_worker__get_task',
}
OPS = {'external_bind', 'external_poll', 'external_resume'}


def validate_selection(task):
    value = task.get('external_worker')
    if value is None:
        return
    object_fields(value, ('protocol', 'model', 'task', 'standard', 'reason'))
    _need(value['protocol'] == PROTOCOL, 'external worker protocol is unsupported')
    _need(text(value['model'], 120) and text(value['reason'], 1000), 'external model and controller reason required')
    request = {'protocol': PROTOCOL, 'task_id': 'swarm-validation', 'goal': task['goal'],
               'task': value['task'], 'standard': value['standard']}
    validate_request(request, validate_text_task)
    _need(not task.get('check_plan'), 'external tasks use standard.checks; execution checks belong to the controller')


def select(task):
    """Return a block reason or a bounded external route explicitly chosen by the controller."""
    value = task['external_worker']
    f = task['features']
    if task.get('override') is not None:
        return 'blocked_external_restriction'
    if task['delegation']['prohibited'] or not task['delegation']['authorized']:
        return 'blocked_delegation_authority'
    if (f['stage'] != 'execution' or task.get('operation') == 'controller' or f['risk'] == 'high'
            or f['ambiguity'] != 'clear' or not f['plan_ready'] or f['complexity'] == 'complex'):
        return 'blocked_external_scope'
    if not set(TOOLS.values()) <= set(f['available_tools']):
        return 'blocked_environment'
    return {'action': 'delegate', 'executor': 'external', 'provider': 'local_worker',
            'reason_codes': ['bounded_external_text'], 'role': 'local_worker',
            'model': value['model'], 'effort': 'unknown', 'independence_required': False, 'limitations': []}


def build_packet(task, decision, attempt_id, capabilities):
    _need(isinstance(capabilities, dict), 'fresh describe_protocol capabilities required')
    value = task['external_worker']
    _need(capabilities.get('protocol') == PROTOCOL and capabilities.get('configured_model') == decision['model'],
          'external protocol or model changed; return to controller')
    _need(type(capabilities.get('queue_concurrency')) is int and capabilities['queue_concurrency'] == 1,
          'external queue concurrency must match the reviewed contract')
    request = {'protocol': PROTOCOL, 'task_id': 'swarm-' + attempt_id.replace('-', ''),
               'goal': task['goal'], 'task': copy.deepcopy(value['task']), 'standard': copy.deepcopy(value['standard'])}
    validate_request(request, validate_text_task)
    _need(isinstance(capabilities.get('kinds'), list) and request['task']['kind'] in capabilities['kinds'],
          'external task kind is unavailable')
    limits = {'max_items': len(request['task']['items']),
              'max_input_characters': sum(len(i['text']) for i in request['task']['items']),
              'max_request_bytes': len(canonical_bytes(request))}
    _need(all(type(capabilities.get(k)) is int and capabilities[k] >= count for k, count in limits.items()),
          'external request exceeds current service limits')
    backend_fields = ()
    if 'backend_ref' in capabilities:
        _need(isinstance(capabilities['backend_ref'], str) and re.fullmatch('[0-9a-f]{64}', capabilities['backend_ref']),
              'invalid external backend reference')
        backend_fields = ('backend_ref',)
    return {'read_only': True, 'external_request': request, 'request_sha256': digest(canonical_bytes(request)),
            'capabilities': {k: copy.deepcopy(capabilities[k]) for k in
                             ('protocol', 'configured_model', 'queue_concurrency', 'kinds', *limits, *backend_fields)}}


def dispatch(attempt):
    request = attempt['packet']['external_request']
    arguments = {'request': copy.deepcopy(request)}
    if 'backend_ref' in attempt['packet']['capabilities']:
        arguments['backend_ref'] = attempt['packet']['capabilities']['backend_ref']
    return {'attempt_id': attempt['attempt_id'], 'decision': copy.deepcopy(attempt['decision']),
            'input_digest': attempt['input_digest'], 'status': attempt['status'],
            'request_sha256': attempt['packet']['request_sha256'],
            'external_call': {'tool': TOOLS['submit'], 'arguments': arguments}}


def validate_attempt(attempt):
    if attempt.get('decision', {}).get('executor') != 'external':
        return
    _need(attempt['role'] == 'local_worker' and attempt['read_only'] is True, 'invalid external role')
    _need(attempt['effort'] == 'unknown' and attempt['requested_model'] == attempt['decision']['model'],
          'invalid external model metadata')
    packet = attempt['packet']
    expected = build_packet(attempt['task_snapshot'], attempt['decision'], attempt['attempt_id'], packet['capabilities'])
    _need(packet == expected, 'external frozen request changed')
    if attempt.get('external_receipt') is not None:
        validate_job(attempt, attempt['external_receipt'])


def validate_job(attempt, job, *, full=True):
    _need(isinstance(job, dict), 'external receipt must be an object')
    _need(len(canonical_bytes(job)) <= 512 * 1024, 'external receipt too large')
    _need(job.get('protocol') == PROTOCOL and job.get('task_id') == attempt['packet']['external_request']['task_id'],
          'external receipt does not match the prepared request')
    job_id = job.get('id')
    _need(isinstance(job_id, str) and re.fullmatch('[0-9a-f]{32}', job_id), 'invalid external job id')
    if attempt.get('agent_id') is not None:
        _need(attempt['agent_id'] == 'local_worker:' + job_id, 'external job differs from bound receipt')
    status = job.get('status')
    _need(status in ('queued', 'running', 'succeeded', 'failed'), 'unknown external job status')
    if status == 'succeeded' and full:
        _need(job.get('model_used') == attempt['requested_model'], 'external returned a different model')
        request = attempt['packet']['external_request']
        output = validate_result(request['task'], json.dumps(job.get('result'), ensure_ascii=False))
        checks = check_result(request['standard'], request['task'], output)
        _need(all(c['passed'] for c in checks), 'external result fails frozen acceptance checks')
        acceptance = job.get('acceptance')
        _need(isinstance(acceptance, dict) and acceptance.get('status') == 'automated_checks_passed'
              and acceptance.get('checks') == checks and acceptance.get('parent_review_required') is True,
              'external acceptance receipt differs from frozen standards')
    if status == 'failed' and full:
        error = job.get('error')
        _need(isinstance(error, dict) and text(error.get('kind'), 120), 'external failure kind is missing')
    return job_id


def transition(store, state, op, owner, p):
    attempt = store._attempt(state, p)
    _need(attempt['decision'].get('executor') == 'external', 'external operation requires external attempt')
    _need(p.get('input_digest') == attempt['input_digest'], 'external input digest mismatch')
    if op == 'external_resume':
        _need(attempt['status'] in ('dispatch_pending', 'bound', 'failure_reported'),
              'external attempt is already terminal or reported')
        if attempt['status'] == 'dispatch_pending':
            from outline_state import guard as outline_guard
            from main_switch import guard as switch_guard
            from work_evidence import check_hashes
            task = attempt['task_snapshot']
            outline_guard(store, state, 'prepare', {'task_id': attempt['task_id']})
            switch_guard(state, 'prepare', {})
            store._depends_verified(state, task)
            _need(attempt['dependency_versions'] == store._dependency_versions(state, task),
                  'external dependency changed before recovery')
            check_hashes(task.get('snapshot_files', {}), 'external recovery snapshot')
            expected = build_packet(task, attempt['decision'], attempt['attempt_id'], p.get('external_capabilities'))
            _need(expected == attempt['packet'], 'external capabilities changed before recovery')
            evidence = _evidence(p.get('recovery_evidence'), 'same service/queue idempotency evidence')
            store._event(state, op, owner, {'attempt_id': attempt['attempt_id'], 'recovery_evidence': evidence})
            return dispatch(attempt)
        store._event(state, op, owner, {'attempt_id': attempt['attempt_id']})
        return {'attempt_id': attempt['attempt_id'], 'status': attempt['status'],
                'external_call': {'tool': TOOLS['get'], 'arguments': {'job_id': attempt['agent_id'].split(':', 1)[1]}}}

    job = copy.deepcopy(p.get('receipt'))
    job_id = validate_job(attempt, job, full=False)
    identity = {'attempt_id': attempt['attempt_id'], 'agent_id': 'local_worker:' + job_id}
    if op == 'external_bind':
        return store._bind(state, owner, identity)
    _need(attempt['status'] in ('bound', 'reported', 'failed', 'failure_reported'),
          'bind the external submission receipt before polling')
    if attempt['status'] == 'failed':
        _need(attempt.get('external_receipt',attempt.get('external_rejected_receipt')) == job,
              'conflicting terminal external failure')
        return {'attempt_id': attempt['attempt_id'], 'status': 'failed', 'idempotent': True}
    if job['status'] in ('queued', 'running'):
        _need(attempt['status'] in ('bound','failure_reported'), 'a reported result cannot become pending')
        store._event(state, 'external_pending', owner, {'attempt_id': attempt['attempt_id'], 'status': job['status']})
        return {'attempt_id': attempt['attempt_id'], 'status': attempt['status'], 'external_status': job['status']}
    evidence = p.get('evidence')
    stopped = 'local-worker terminal queue receipt ' + job_id
    try:
        validate_job(attempt, job)
    except (StateError, ValueError, TypeError, KeyError) as exc:
        _need(attempt['status'] in ('bound','failure_reported'),
              'a retained reported result cannot be replaced by a rejected receipt')
        attempt['external_rejected_receipt'] = job
        category = 'model' if job.get('model_used') != attempt['requested_model'] and job['status']=='succeeded' else 'validation'
        return store._fail(state, owner, {**identity, 'input_digest': attempt['input_digest'],
            'category': category, 'evidence': [*_evidence(evidence), str(exc)], 'host_stopped_evidence': stopped})
    prior = attempt.get('external_receipt')
    _need(prior is None or prior == job, 'external terminal receipt changed')
    attempt['external_receipt'] = job
    if attempt['status'] == 'failure_reported' and job['status'] == 'succeeded':
        # A later candidate can establish queue termination, but cannot erase a
        # prior attributable failure or turn the same failed attempt green.
        return store._fail(state, owner, {**identity, 'input_digest': attempt['input_digest'],
            'category': attempt['failure_category'], 'evidence': evidence, 'host_stopped_evidence': stopped})
    if job['status'] == 'failed':
        kind = job['error']['kind']
        # The v1 service also uses invalid_output for a backend model mismatch.
        # Only an explicit acceptance failure is safe to classify as validation.
        category = 'validation' if kind == 'acceptance_failed' else 'model'
        return store._fail(state, owner, {**identity, 'input_digest': attempt['input_digest'],
            'category': category, 'evidence': evidence, 'host_stopped_evidence': stopped})
    return store._report(state, owner, {**identity, 'input_digest': attempt['input_digest'],
        'summary': _nonempty(p.get('summary'), 'controller result summary'), 'evidence': evidence,
        'handoff': p.get('handoff'), 'host_reported_model': job['model_used'],
        'host_reported_effort': None, 'host_stopped_evidence': stopped})
