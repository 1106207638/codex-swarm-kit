"""Version 1 of the task and acceptance contract shared by CLI and MCP."""

import hashlib
import json
import re

PROTOCOL = 'local-worker/v1'
MAX_REQUEST_BYTES = 192 * 1024


class AgentError(Exception):
    pass


class AcceptanceError(AgentError):
    def __init__(self, checks):
        super().__init__('One or more acceptance checks failed')
        self.checks = checks


def need(condition, message):
    if not condition:
        raise AgentError(message)


def object_fields(value, required, optional=()):
    need(isinstance(value, dict) and set(required) <= set(value) <= set(required) | set(optional),
         f'Expected fields {sorted(required)}, optional {sorted(optional)}')


def text(value, limit):
    return isinstance(value, str) and value.strip() and 0 < len(value) <= limit


def validate_standard(standard, task):
    object_fields(standard, ('criteria',), ('checks',))
    criteria = standard['criteria']
    need(isinstance(criteria, list) and 1 <= len(criteria) <= 8
         and all(text(criterion, 500) for criterion in criteria), 'Provide 1–8 criteria, up to 500 characters each')
    # The existing validator already guarantees unique item IDs and known task kinds.
    item_ids = {item['id'] for item in task['items']}
    checks = standard.get('checks', [])
    need(isinstance(checks, list) and len(checks) <= 24, 'At most 24 acceptance checks')
    seen = set()
    for check in checks:
        object_fields(check, ('id', 'item_id', 'op'), ('field', 'expected'))
        identity, item_id, op = check['id'], check['item_id'], check['op']
        need(text(identity, 64) and identity not in seen, 'Check IDs must be non-empty and unique')
        need(isinstance(item_id, str) and item_id in item_ids, 'Check item_id must match an input item')
        need(isinstance(op, str) and op in ('equals', 'contains', 'source_quote'), 'Unknown check operation')
        seen.add(identity)
        field = check.get('field')
        if task['kind'] == 'extract':
            need(isinstance(field, str) and field in task['fields'], 'Check field must match an extract field')
            expected_type = task['fields'][field]['type']
        else:
            need('field' not in check, 'field is only used for extraction')
            expected_type = 'string'
        if op == 'equals':
            need('expected' in check and (check['expected'] is None or type(check['expected']) in (str, int, bool)),
                 'equals requires a string, integer, boolean or null expected value')
            expected = check['expected']
            if expected is None:
                need(task['kind']=='extract' and task['fields'][field]['nullable'], 'null is not allowed for this field')
            else:
                need({str:'string', int:'integer', bool:'boolean'}[type(expected)]==expected_type,
                     'equals expected has the wrong type for this field')
        else:
            need(expected_type=='string', f'{op} can only be used with a text value')
            if op == 'contains':
                need(text(check.get('expected'),512), 'contains requires a non-empty text expected value')
            else:
                need('expected' not in check, 'source_quote compares to the input, so it does not accept expected')


def validate_request(request, validate_task):
    object_fields(request, ('protocol', 'task_id', 'goal', 'task', 'standard'))
    need(request['protocol']==PROTOCOL, 'Unsupported protocol version')
    identity = request['task_id']
    need(isinstance(identity,str) and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:-]{0,95}',identity),
         'task_id must be 1–96 ASCII identifier characters')
    need(text(request['goal'],1000), 'Goal must be 1–1000 characters')
    validate_task(request['task'])
    validate_standard(request['standard'], request['task'])
    canonical=canonical_bytes(request)
    need(len(canonical)<=MAX_REQUEST_BYTES, 'Request exceeds 192 KiB')
    return canonical


def canonical_bytes(value):
    return json.dumps(value,ensure_ascii=False,allow_nan=False,sort_keys=True,separators=(',',':')).encode('utf-8')


def digest(data):
    return hashlib.sha256(data).hexdigest()


def check_result(standard, task, result):
    inputs={item['id']:item['text'] for item in task['items']}
    outputs={row['id']:row['value'] for row in result['items']}
    reports=[]
    for check in standard.get('checks', []):
        actual=outputs[check['item_id']]
        if 'field' in check:
            actual=actual[check['field']]
        op=check['op']
        if op=='equals':
            expected=check['expected']
            passed=type(actual) is type(expected) and actual==expected
        elif op=='contains':
            passed=isinstance(actual,str) and check['expected'] in actual
        else:
            passed=isinstance(actual,str) and bool(actual.strip()) and actual in inputs[check['item_id']]
        reports.append({'id':check['id'],'passed':passed})
    return reports
