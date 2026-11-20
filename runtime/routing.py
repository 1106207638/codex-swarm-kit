"""JSON CLI for routing decisions and the single-coordinator task ledger.

Input: one JSON object on stdin or --request FILE. Output is one JSON envelope.
The catalog provenance fields are caller claims and are recorded, not authenticated.
"""
import argparse
import json
import sys

from router_policy import decide
from task_state import StateError, Store, template
import call_log
import main_switch


def handle(request, journal_call_id=None):
    if not isinstance(request, dict):
        raise StateError("request must be a JSON object")
    op = request.get("op")
    if op == "template":
        return template()
    if op == 'packet_read':
        from packet_transport import read_packet
        return read_packet(request.get('path'),request.get('sha256'),request.get('attempt_id'))
    if op == "route":
        return {"decision": decide(request.get("task"), request.get("catalog"))}
    if op == "main_observe":
        return main_switch.observe(request.get("rollout_path"), request.get("thread_id"))
    if op == "main_recommend":
        return {"decision": main_switch.recommend(request.get("task"), request.get("catalog"),
                                                   request.get("pins", {}), request.get("substantial"))}
    root = request.get("state_dir")
    if not isinstance(root, str) or not root.strip():
        raise StateError("state_dir must be supplied")
    store = Store(root)
    if op == "init":
        return store.init(request.get("owner"))
    owner, epoch = request.get("owner"), request.get("epoch")
    if op == "swarm_recap":
        from swarm_recap import read
        return read(store, owner, epoch, request.get("scope"))
    if op == "status":
        return store.status(owner, epoch)
    if op == "call_log":
        filters = {key: value for key, value in request.items() if key not in {"op", "state_dir", "owner", "epoch"}}
        return call_log.read(store, owner, epoch, **filters)
    if op == "main_switch_get":
        return main_switch.get(store.status(owner, epoch), request.get("request_id"))
    payload = {key: value for key, value in request.items()
               if key not in {"op", "state_dir", "owner", "epoch", "journal_call_id"}}
    return store.apply(op, owner, epoch, journal_call_id=journal_call_id, **payload)


CLI_ERRORS = (ValueError, TypeError, OSError)


def run(request):
    """Wrap the CLI boundary without changing the ledger's commit semantics."""
    call = None
    if isinstance(request, dict):
        op, root = request.get("op"), request.get("state_dir")
        if isinstance(op, str):
            op = op.strip()
            request = {**request, "op": op}
        if isinstance(op, str) and op in call_log.LOGGED and isinstance(root, str) and root.strip():
            store = Store(root)
            if op in call_log.PREVIEWS:
                store.status(request.get("owner"), request.get("epoch"))
            if store.path.is_file():
                call = call_log.Call(store.root, request)
                try:
                    call.write("start")
                except CLI_ERRORS:
                    return {"ok": False, "error": "could not record call start; operation did not run",
                            "call_log": {"id": call.call_id, "status": "start_write_failed"}}, 2

    error = result = None
    try:
        result = handle(request, journal_call_id=call.call_id if call is not None else None)
        response, exit_code = {"ok": True, "result": result}, 0
    except CLI_ERRORS as exc:
        error = exc
        response, exit_code = {"ok": False, "error": str(exc)}, 2
    if call is not None:
        try:
            call.finish(result, error)
            status = "recorded"
        except CLI_ERRORS:
            # The state operation may already be committed. Preserve its response.
            status = "finish_write_failed"
        response["call_log"] = {"id": call.call_id, "status": status}
    return response, exit_code


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request", help="JSON request file; omit or use - for stdin")
    parser.add_argument('--packet',help='Read only this frozen task file; never reads the ledger')
    parser.add_argument('--sha256')
    parser.add_argument('--attempt-id')
    args = parser.parse_args(argv)
    try:
        if args.packet is not None:
            if args.request: raise StateError('packet and request modes cannot be combined')
            request = {'op':'packet_read','path':args.packet,'sha256':args.sha256,'attempt_id':args.attempt_id}
        elif args.sha256 is not None or args.attempt_id is not None:
            raise StateError('packet digest/identity options require --packet')
        elif args.request and args.request != "-":
            with open(args.request, "r", encoding="utf-8") as stream:
                request = json.load(stream)
        else:
            request = json.load(sys.stdin)
        response, exit_code = run(request)
    except CLI_ERRORS as exc:
        response, exit_code = {"ok": False, "error": str(exc)}, 2
    print(json.dumps(response, ensure_ascii=False))
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
