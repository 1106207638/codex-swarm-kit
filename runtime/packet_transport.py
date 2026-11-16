"""Digest-bound task files. No native spawning or parsing of encrypted logs."""
import copy
import hashlib
import json
import os
import re
from pathlib import Path

from task_state import _need
from platform_runtime import python_command

FORMAT = 'codex-task-packet-v1'


def _json_object(pairs):
    value = {}
    for key, item in pairs:
        _need(key not in value, 'duplicate packet JSON key')
        value[key] = item
    return value


def read_packet(path, digest, attempt_id):
    _need(isinstance(path,str) and Path(path).is_absolute(), 'packet path must be absolute')
    _need(isinstance(digest,str) and re.fullmatch('[0-9a-f]{64}',digest), 'invalid packet digest')
    _need(isinstance(attempt_id,str) and attempt_id, 'packet attempt_id required')
    data = Path(path).read_bytes()
    _need(hashlib.sha256(data).hexdigest() == digest, 'packet bytes changed; do not execute')
    value = json.loads(data.decode('utf-8'),object_pairs_hook=_json_object)
    _need(isinstance(value,dict) and set(value)=={'format','attempt_id','input_digest','body'}, 'invalid task packet envelope')
    _need(value['format']==FORMAT and value['attempt_id']==attempt_id, 'task packet identity mismatch')
    body = value['body']
    _need(isinstance(body,dict) and body.get('attempt_id')==attempt_id and isinstance(body.get('read_only'),bool), 'invalid packet body')
    _need(isinstance(body.get('role_developer_instructions'),str) and body['role_developer_instructions'].strip(), 'complete role instructions required')
    _need(isinstance(value['input_digest'],str) and re.fullmatch('[0-9a-f]{64}',value['input_digest']), 'invalid task input digest')
    return {'packet_sha256':digest, **value}


def materialize(packet, task, attempt_id, state_root, script, input_digest):
    if task.get('packet_transport','inline') == 'inline': return packet
    path = (Path(state_root)/'packets'/(attempt_id+'.json')).resolve()
    body = copy.deepcopy(packet['body'])
    body['bootstrap_read_paths'] = [str(path)]
    body['execution_constraints'].append('bootstrap_read_paths仅额外授权读取本任务包，不授权读取其他台账或大纲文件；先核包hash和身份，完整读取全部角色职责后才执行。')
    envelope = {'format':FORMAT,'attempt_id':attempt_id,'input_digest':input_digest,'body':body}
    data = (json.dumps(envelope,ensure_ascii=False,indent=2)+'\n').encode('utf-8')
    digest = hashlib.sha256(data).hexdigest()
    path.parent.mkdir(parents=True,exist_ok=True)
    # Publication may leave an orphan on a crash before the ledger commits.
    # The file alone never authorizes a dispatch; recovery still starts status.
    if path.exists():
        _need(path.read_bytes()==data, 'existing packet differs; refusing overwrite')
    else:
        with path.open('xb') as stream:
            stream.write(data);stream.flush();os.fsync(stream.fileno())
    command, shell = python_command(['-B', '-X', 'utf8', str(Path(script).resolve()), '--packet', str(path), '--sha256', digest, '--attempt-id', attempt_id])
    packet = copy.deepcopy(packet)
    packet['body'] = body
    packet['transport'] = {'format':FORMAT,'path':str(path),'sha256':digest,'attempt_id':attempt_id,'input_digest':input_digest}
    packet['spawn_args']['message'] = (
        '先读取并校验冻结任务包，输出预算至少10000 tokens，完整读取返回JSON中的全部角色职责与约束后才工作。'
        '读取失败立即向父中枢回报，不修包、不猜路径、不读取其他台账。'
        +('本任务只读，禁止修改文件。' if packet['read_only'] else '只在任务包ownership内修改，不覆盖他人编辑。')
        +'使用'+shell+'执行以下命令（只读文件，不发送消息或派发代理；exec_command显式选择此shell，login=false）：\n'+command+
        '\n最终回报附packet_sha256；自报不替代中枢核验实际读取轨迹。')
    return packet


def validate_receipt(attempt, supplied):
    transport = attempt['packet'].get('transport')
    if transport is None: return
    _need(supplied == transport['sha256'], 'file task requires matching packet_sha256 receipt')
    value = read_packet(transport['path'],transport['sha256'],attempt['attempt_id'])
    _need(value['input_digest']==attempt['input_digest'] and value['body']==attempt['packet']['body'],
          'task packet does not match retained prepared task')
