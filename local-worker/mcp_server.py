"""Official MCP SDK adapter for the same queue and JSON contract as the CLI."""

import argparse
import json
from pathlib import Path
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from local_agent import KINDS, MODEL, Queue, backend, backend_ref, public_job, require_visible_job
from task_contract import MAX_REQUEST_BYTES, PROTOCOL, need


ROOT = Path(__file__).resolve().parent


def make_server(state: Path, endpoint: str, model: str) -> MCPServer:
    initial = backend(endpoint, model)
    queue = Queue(state)
    schema = json.loads((ROOT/'schema-v1.json').read_text(encoding='utf-8'))
    server = MCPServer('Local Worker', version='1.0.0', instructions=(
        'Submit a bounded text task, continue your main work, then call get_task with the returned id. '
        'Use describe_protocol for the request schema. Only treat a result as a candidate for your own final acceptance.'))

    @server.tool(annotations=ToolAnnotations(readOnlyHint=True,openWorldHint=False))
    def describe_protocol() -> dict[str, Any]:
        """Get the task contract schema, example and request limits."""
        settings = queue.configured() or initial
        return {'protocol':PROTOCOL,'request_schema':schema,
                'example':json.loads((ROOT/'contract-example.json').read_text(encoding='utf-8')),
                'kinds':sorted(KINDS),'max_request_bytes':MAX_REQUEST_BYTES,'max_input_characters':18000,
                'max_items':24,'queue_concurrency':1,'configured_model':settings['model'],
                'backend_ref':backend_ref(settings)}

    @server.tool(annotations=ToolAnnotations(readOnlyHint=False,destructiveHint=False,idempotentHint=True,openWorldHint=True))
    def submit_task(request: dict[str, Any], backend_ref: str | None = None) -> dict[str, Any]:
        """Queue a local-worker/v1 request. Returns id immediately; keep a stable request.task_id on retry."""
        return queue.submit_request(request,endpoint,model,use_config=True,expected_backend=backend_ref)

    @server.tool(annotations=ToolAnnotations(readOnlyHint=True,openWorldHint=False))
    def get_task(job_id: str) -> dict[str, Any]:
        """Read status or result by the job id returned by submit_task. This call does not wait."""
        with queue.locked():
            job = queue.read(job_id)
            need(job.get('protocol')==PROTOCOL, 'The task does not use local-worker/v1')
            # Tagged MCP jobs remain readable against their original connection
            # even when the operator is repairing an invalid configuration file.
            current = None if job.get('origin_backend_ref') else queue.configured()
            require_visible_job(job,initial,current)
            return public_job(job)

    return server


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state',type=Path,default=ROOT/'state')
    parser.add_argument('--endpoint',required=True,help='Explicit private or loopback IPv4 URL ending in /v1')
    parser.add_argument('--model',default=MODEL)
    args=parser.parse_args()
    make_server(args.state,args.endpoint,args.model).run(transport='stdio')


if __name__=='__main__':
    main()
