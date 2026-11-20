"""Frozen local-worker/v1 wire validation for the swarm boundary.

Reuses the delivered general worker contract. No worker runtime or endpoint dependency.
"""
import hashlib
import json
import re

from task_state import StateError

PROTOCOL = 'local-worker/v1'
MAX_REQUEST_BYTES = 192 * 1024
KINDS = {'classify', 'extract', 'translate', 'summarize'}
TYPES = {'string': str, 'integer': int, 'boolean': bool}


def parse_json(content):
    return json.loads(content)


def need(condition, message):
    if not condition:
        raise StateError(message)


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

def validate_task(task):
    object_fields(task, ('kind', 'instruction', 'items'), ('labels', 'fields'))
    kind = task['kind']
    need(isinstance(kind, str) and kind in KINDS, 'Unknown task kind')
    instruction = task['instruction']
    need(isinstance(instruction, str) and 0 < len(instruction.strip()) <= 2000, 'Instruction must be 1–2000 characters')
    items = task['items']
    need(isinstance(items, list) and 1 <= len(items) <= 24, 'Submit 1–24 items per task')
    ids, total = set(), 0
    for item in items:
        optional = ('keep',) if kind == 'translate' else ()
        object_fields(item, ('id', 'text'), optional)
        key, source = item['id'], item['text']
        need(isinstance(key, str) and 0 < len(key.strip()) <= 80 and key not in ids, 'Item IDs must be unique and non-empty')
        need(isinstance(source, str) and source.strip(), 'Item text cannot be empty')
        ids.add(key)
        total += len(source)
        keep = item.get('keep', [])
        need(isinstance(keep, list) and len(keep) <= 20
             and all(isinstance(term, str) and term and term in source for term in keep),
             'Each keep value must occur verbatim in the source')
    need(total <= 18000, 'Input exceeds 18,000 characters. Split the work into smaller tasks.')
    if kind == 'classify':
        labels = task.get('labels')
        need(isinstance(labels, list) and 2 <= len(labels) <= 16
             and all(isinstance(label, str) and 0 < len(label.strip()) <= 40 for label in labels)
             and len(set(labels)) == len(labels) and 'fields' not in task, 'Classify requires 2–16 unique labels')
    elif kind == 'extract':
        fields = task.get('fields')
        need(isinstance(fields, dict) and 1 <= len(fields) <= 16 and 'labels' not in task, 'Extract requires 1–16 fields')
        for key, spec in fields.items():
            need(0 < len(key.strip()) <= 64, 'Invalid field name')
            object_fields(spec, ('type', 'nullable'))
            need(isinstance(spec['type'], str) and spec['type'] in TYPES
                 and type(spec['nullable']) is bool, 'Invalid field type or nullable value')
    else:
        need('fields' not in task and 'labels' not in task, 'Unexpected fields or labels')
    return task


def validate_result(task, content):
    result = parse_json(content)
    object_fields(result, ('items',))
    expected = task['items']
    rows = result['items']
    need(isinstance(rows, list) and len(rows) == len(expected), 'Result omitted or duplicated items')
    for row, item in zip(rows, expected):
        required = ('id', 'value', 'evidence') if task['kind'] == 'summarize' else ('id', 'value')
        object_fields(row, required)
        need(row['id'] == item['id'], 'Result item ID or order differs from input')
        value = row['value']
        match task['kind']:
            case 'classify':
                need(isinstance(value, str) and value in task['labels'], 'Result label outside the allowed list')
            case 'extract':
                object_fields(value, task['fields'])
                for name, spec in task['fields'].items():
                    field = value[name]
                    need((field is None and spec['nullable']) or type(field) is TYPES[spec['type']],
                         f'Wrong type or null value for {name}')
            case 'translate' | 'summarize':
                need(isinstance(value, str) and value.strip(), 'Expected a non-empty text value')
                need(all(term in value for term in item.get('keep', [])), 'Translation lost a required literal')
        if task['kind'] == 'summarize':
            evidence = row['evidence']
            need(isinstance(evidence, list) and 1 <= len(evidence) <= 5
                 and all(isinstance(q, str) and q.strip() and q in item['text'] for q in evidence),
                 'Summary evidence must quote the supplied text verbatim')
    return result


