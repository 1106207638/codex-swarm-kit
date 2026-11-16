"""JSON CLI for routing decisions and the single-coordinator task ledger.

Input: one JSON object on stdin or --request FILE. Output is one JSON envelope.
The catalog provenance fields are caller claims and are recorded, not authenticated.
"""
import argparse
import json
import sys

from router_policy import PolicyError, decide
from task_state import StateError, Store, template
import main_switch


def handle(request):
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
    if op == "status":
        return store.status(owner, epoch)
    if op == "main_switch_get":
        return main_switch.get(store.status(owner, epoch), request.get("request_id"))
    return store.apply(op, owner, epoch, **{key: value for key, value in request.items()
                                            if key not in {"op", "state_dir", "owner", "epoch"}})


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
        result = handle(request)
    except (StateError, PolicyError, ValueError, TypeError, OSError, json.JSONDecodeError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 2
    print(json.dumps({"ok": True, "result": result}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
