"""Crash-safe single-coordinator task state with explicit dispatch receipts.

The lock serializes read/validate/write across processes. One JSON document is the
source of truth; event history and task/attempt state commit in one os.replace.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import tempfile
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path


class StateError(ValueError):
    """State is invalid, stale, or the requested transition is not allowed."""


SCHEMA_VERSION = 1
MAX_ATTEMPTS = 3
MAX_ACTIVE = 2
ACTIVE = {"dispatch_pending", "bound", "reported", "failure_reported"}
TERMINAL = {"verified", "failed", "no_spawn", "stopped"}


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _need(ok, message):
    if not ok:
        raise StateError(message)


def _nonempty(value, label):
    _need(isinstance(value, str) and value.strip(), f"{label} must be a nonempty string")
    return value.strip()


def _evidence(value, label="evidence"):
    _need(isinstance(value, list) and bool(value), f"{label} must be a nonempty list")
    _need(all(isinstance(item, str) and item.strip() for item in value), f"{label} must contain nonempty strings")
    return value


def input_digest(task):
    raw = json.dumps(task, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


@contextmanager
def _file_lock(path):
    """OS process lock; lock file is intentionally persistent and never unlinked."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+b")
    try:
        if os.name == "nt":
            import msvcrt
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            try:
                handle.seek(0, os.SEEK_END)
                if handle.tell() == 0:
                    handle.seek(0)
                    handle.write(b"0")
                    handle.flush()
                yield
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()


class Store:
    def __init__(self, root):
        self.root = Path(root).resolve(strict=False)
        self.path = self.root / "state.json"
        self.lock_path = self.root / "state.lock"

    def _read(self):
        if not self.path.exists():
            raise StateError("state is not initialized; run init explicitly")
        try:
            with self.path.open("r", encoding="utf-8") as stream:
                state = json.load(stream)
        except (OSError, json.JSONDecodeError) as exc:
            raise StateError(f"state is unreadable or corrupt; refusing reset: {exc}") from exc
        try:
            self._validate_state(state)
        except StateError:
            raise
        except (KeyError, TypeError, IndexError, AttributeError) as exc:
            raise StateError(f"state structure is corrupt; refusing reset: {exc}") from exc
        return state

    @staticmethod
    def _validate_state(state):
        from main_switch import validate_switches

        _need(isinstance(state, dict) and state.get("schema_version") in (1, 2), "unsupported or damaged state schema")
        for key in ("coordinator_id", "epoch", "tasks", "attempts", "events", "created_at", "updated_at"):
            _need(key in state, f"state missing {key}")
        _need(isinstance(state["coordinator_id"], str) and state["coordinator_id"], "invalid coordinator id")
        _need(type(state["epoch"]) is int and state["epoch"] > 0, "invalid coordinator epoch")
        _need(isinstance(state["tasks"], dict) and isinstance(state["attempts"], dict) and isinstance(state["events"], list), "invalid state collections")
        _need(state.get("limits") == {"max_attempts": MAX_ATTEMPTS, "max_active": MAX_ACTIVE}, "state limits mismatch")
        for task_id, record in state["tasks"].items():
            _need(isinstance(task_id, str) and isinstance(record, dict), "invalid task record")
            _need(type(record.get("current_version")) is int and record["current_version"] > 0, "invalid task version")
            _need(type(record.get("validation_generation")) is int and record["validation_generation"] > 0,
                  "invalid task validation generation")
            _need(type(record.get("verified")) is bool and isinstance(record.get("versions"), list) and record["versions"], "invalid task version history")
            _need(record["current_version"] == record["versions"][-1].get("version"), "current task version does not match history")
            for index, version in enumerate(record["versions"], start=1):
                _need(isinstance(version, dict) and version.get("version") == index, "task versions must be consecutive")
                task = version.get("task")
                _need(isinstance(task, dict) and task.get("task_id") == task_id, "task snapshot id mismatch")
                _need(version.get("input_digest") == input_digest(task), "task snapshot digest mismatch")
            _need(record.get("direct_pending") is None or isinstance(record.get("direct_pending"), dict), "invalid direct pending record")
            _need(isinstance(record.get("direct_completions"), list), "invalid direct completion history")
            if record.get("direct_pending") is not None:
                pending = record["direct_pending"]
                _need(pending.get("task_version") == record["current_version"] and
                      pending.get("input_digest") == record["versions"][-1]["input_digest"] and
                      pending.get("validation_generation") == record["validation_generation"] and
                      isinstance(pending.get("dependency_versions"), dict) and
                      isinstance(pending.get("owned_paths"), list) and isinstance(pending.get("decision"), dict),
                      "direct pending record does not match current task version")
            for completion in record["direct_completions"]:
                _need(isinstance(completion, dict) and type(completion.get("task_version")) is int and
                      1 <= completion["task_version"] <= record["current_version"], "invalid direct completion version")
                _need(completion.get("input_digest") == record["versions"][completion["task_version"] - 1]["input_digest"],
                      "direct completion digest does not match retained version")
                _need(type(completion.get("validation_generation")) is int and isinstance(completion.get("dependency_versions"), dict),
                      "invalid direct completion validation snapshot")
        allowed_statuses = ACTIVE | TERMINAL
        attempt_numbers = {}
        for attempt_id, attempt in state["attempts"].items():
            _need(isinstance(attempt_id, str) and isinstance(attempt, dict) and attempt.get("attempt_id") == attempt_id, "attempt id mismatch")
            _need(attempt.get("task_id") in state["tasks"], "attempt references missing task")
            _need(type(attempt.get("task_version")) is int and attempt["task_version"] > 0 and
                  attempt["task_version"] <= state["tasks"][attempt["task_id"]]["current_version"], "attempt task version is invalid")
            _need(attempt.get("status") in allowed_statuses, "invalid attempt status")
            _need(type(attempt.get("attempt_number")) is int and 1 <= attempt["attempt_number"] <= MAX_ATTEMPTS, "invalid attempt number")
            attempt_numbers.setdefault(attempt["task_id"], []).append(attempt["attempt_number"])
            _need(type(attempt.get("validation_generation")) is int and attempt["validation_generation"] > 0,
                  "invalid attempt validation generation")
            _need(isinstance(attempt.get("task_snapshot"), dict) and attempt.get("input_digest") == input_digest(attempt["task_snapshot"]), "attempt input digest mismatch")
            _need(attempt["task_snapshot"].get("task_id") == attempt["task_id"], "attempt snapshot task id mismatch")
            _need(type(attempt.get("worker_stopped")) is bool and type(attempt.get("read_only")) is bool, "invalid attempt lifecycle flags")
            _need(isinstance(attempt.get("evidence"), list) and isinstance(attempt.get("decision"), dict) and
                  isinstance(attempt.get("packet"), dict) and isinstance(attempt.get("requested_model"), str) and
                  isinstance(attempt.get("effort"), str) and isinstance(attempt.get("owned_paths"), list), "invalid attempt record fields")
            _need(attempt.get("usage") == "unknown", "unsupported usage state")
            task_version = state["tasks"][attempt["task_id"]]["versions"][attempt["task_version"] - 1]
            _need(attempt["task_snapshot"] == task_version["task"] and attempt["input_digest"] == task_version["input_digest"],
                  "attempt does not match its retained task version")
        for task_id, numbers in attempt_numbers.items():
            _need(sorted(numbers) == list(range(1, len(numbers) + 1)), f"attempt numbers are not consecutive for task {task_id}")
        for index, event in enumerate(state["events"], start=1):
            _need(isinstance(event, dict) and event.get("seq") == index and isinstance(event.get("type"), str), "invalid event sequence")
        validate_switches(state)
        from outline_state import validate_state
        validate_state(state)

    def _write(self, state):
        state["updated_at"] = _now()
        encoded = json.dumps(state, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8")
        self.root.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(prefix="state-", suffix=".tmp", dir=self.root)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp_name, self.path)
            if os.name != "nt":
                dir_fd = os.open(self.root, os.O_RDONLY)
                try:
                    os.fsync(dir_fd)
                finally:
                    os.close(dir_fd)
        except Exception:
            raise

    def init(self, owner):
        owner = _nonempty(owner, "owner")
        with _file_lock(self.lock_path):
            if self.path.exists():
                raise StateError("state already exists; init never takes over or resets it")
            now = _now()
            state = {"schema_version": SCHEMA_VERSION, "coordinator_id": owner, "epoch": 1,
                     "limits": {"max_attempts": MAX_ATTEMPTS, "max_active": MAX_ACTIVE},
                     "tasks": {}, "attempts": {}, "events": [], "created_at": now, "updated_at": now}
            self._event(state, "init", owner, {"epoch": 1})
            self._write(state)
            return copy.deepcopy(state)

    def _event(self, state, kind, owner, data):
        state["events"].append({"seq": len(state["events"]) + 1, "at": _now(), "type": kind,
                                "owner": owner, "data": copy.deepcopy(data)})

    @staticmethod
    def _check_owner(state, owner, epoch):
        _need(owner == state["coordinator_id"], "coordinator owner mismatch")
        _need(type(epoch) is int and epoch == state["epoch"], "stale coordinator epoch")

    def status(self, owner, epoch):
        with _file_lock(self.lock_path):
            state = self._read()
            self._check_owner(state, owner, epoch)
            return copy.deepcopy(state)

    def apply(self, op, owner, epoch, **payload):
        op = _nonempty(op, "op")
        with _file_lock(self.lock_path):
            state = self._read()
            self._check_owner(state, owner, epoch)
            candidate = copy.deepcopy(state)
            result = self._transition(candidate, op, owner, payload)
            self._write(candidate)
            return result

    def _transition(self, state, op, owner, p):
        from main_switch import guard, transition
        from outline_state import guard as outline_guard, transition as outline_transition

        if op.startswith('outline_'):
            return outline_transition(self, state, op, owner, p)
        outline_guard(self, state, op, p)
        if op.startswith("main_switch_"):
            return transition(self, state, op, owner, p)
        guard(state, op, p)
        if op == "add":
            from router_policy import PolicyError, _validate_task
            task = p.get("task")
            _need(isinstance(task, dict), "task object is required")
            try:
                _validate_task(task)
                self._path_set(task)
            except (PolicyError, KeyError, TypeError) as exc:
                raise StateError(f"invalid task: {exc}") from exc
            task_id = _nonempty(task.get("task_id"), "task_id")
            _need(task_id not in state["tasks"], "task already exists")
            deps = task.get("depends_on", [])
            _need(isinstance(deps, list) and all(isinstance(dep, str) and dep for dep in deps), "depends_on must be a list of task ids")
            _need(task_id not in deps and len(set(deps)) == len(deps), "self or duplicate dependency is invalid")
            _need(all(dep in state["tasks"] for dep in deps), "dependencies must already exist")
            state["tasks"][task_id] = {"current_version": 1, "verified": False,
                                      "versions": [{"version": 1, "task": copy.deepcopy(task), "input_digest": input_digest(task), "at": _now()}],
                                      "validation_generation": 1,
                                      "retry_authorization": None, "model_failure_attempt_id": None,
                                      "resolved_model_failure_attempt_id": None, "direct_pending": None,
                                      "direct_completions": []}
            self._event(state, "task_added", owner, {"task_id": task_id, "version": 1})
            return {"task_id": task_id, "version": 1, "input_digest": input_digest(task)}

        if op == "update":
            from router_policy import PolicyError, _validate_task
            task = p.get("task")
            _need(isinstance(task, dict), "complete replacement task is required")
            try:
                _validate_task(task)
                self._path_set(task)
            except (PolicyError, KeyError, TypeError) as exc:
                raise StateError(f"invalid task: {exc}") from exc
            task_id = _nonempty(task.get("task_id"), "task_id")
            record = state["tasks"].get(task_id)
            _need(record is not None, "unknown task")
            _need(not self._active_for_task(state, task_id) and record.get("direct_pending") is None,
                  "cannot update task input while an attempt/direct action may still be active")
            deps = task.get("depends_on", [])
            _need(isinstance(deps, list) and task_id not in deps and len(set(deps)) == len(deps), "self or duplicate dependency is invalid")
            _need(all(dep in state["tasks"] for dep in deps), "dependencies must already exist")
            stack, seen = list(deps), set()
            while stack:
                dep = stack.pop()
                _need(dep != task_id, "dependency update would create a cycle")
                if dep in seen:
                    continue
                seen.add(dep)
                stack.extend(state["tasks"][dep]["versions"][-1]["task"].get("depends_on", []))
            downstream = self._downstream(state, task_id)
            _need(not any(self._active_for_task(state, child) or state["tasks"][child].get("direct_pending") is not None for child in downstream),
                  "cannot update input while a downstream dependent attempt/direct action may still be active")
            version = record["current_version"] + 1
            digest = input_digest(task)
            record["current_version"] = version
            record["validation_generation"] += 1
            record["verified"] = False
            record["direct_pending"] = None
            record["versions"].append({"version": version, "task": copy.deepcopy(task), "input_digest": digest, "at": _now()})
            invalidated = []
            for child in downstream:
                child_record = state["tasks"][child]
                if child_record["verified"]:
                    invalidated.append(child)
                child_record["verified"] = False
                child_record["validation_generation"] += 1
                if child not in invalidated:
                    invalidated.append(child)
            self._event(state, "task_updated", owner, {"task_id": task_id, "version": version, "input_digest": digest,
                                                        "invalidated_downstream": invalidated})
            return {"task_id": task_id, "version": version, "input_digest": digest, "invalidated_downstream": invalidated}

        if op == "prepare":
            return self._prepare(state, owner, p)
        if op == "complete_direct":
            return self._complete_direct(state, owner, p)
        if op == "bind":
            return self._bind(state, owner, p)
        if op == "report":
            return self._report(state, owner, p)
        if op == "record_check":
            return self._record_check(state, owner, p)
        if op == "verify":
            return self._verify(state, owner, p)
        if op == "fail":
            return self._fail(state, owner, p)
        if op == "reconcile":
            return self._reconcile(state, owner, p)
        if op == "reconcile_direct":
            return self._reconcile_direct(state, owner, p)
        if op == "retry":
            return self._retry(state, owner, p)
        if op == "handoff":
            return self._handoff(state, owner, p)
        raise StateError("unknown state operation: " + op)

    def _task_version(self, state, task_id):
        record = state["tasks"].get(task_id)
        _need(record is not None, "unknown task")
        return record, record["versions"][-1]

    @staticmethod
    def _active_for_task(state, task_id):
        return [a for a in state["attempts"].values() if a["task_id"] == task_id and a["status"] in ACTIVE and not a["worker_stopped"]]

    @staticmethod
    def _active(state):
        return [a for a in state["attempts"].values() if a["status"] in ACTIVE and not a["worker_stopped"]]

    @staticmethod
    def _depends_verified(state, task):
        for dep in task.get("depends_on", []):
            record = state["tasks"].get(dep)
            _need(record is not None, f"dependency does not exist: {dep}")
            _need(record["verified"] is True, f"dependency is not verified: {dep}")

    @staticmethod
    def _downstream(state, parent_id):
        found, frontier = set(), {parent_id}
        while frontier:
            next_frontier = set()
            for task_id, record in state["tasks"].items():
                deps = record["versions"][-1]["task"].get("depends_on", [])
                if task_id not in found and any(dep in frontier for dep in deps):
                    found.add(task_id)
                    next_frontier.add(task_id)
            frontier = next_frontier
        return sorted(found)

    @staticmethod
    def _path_set(task):
        workspace = Path(task["workspace"]).resolve(strict=False)
        _need(workspace.is_dir(), "workspace must resolve to an existing directory")
        resolved = []
        for raw in task["ownership"]:
            path = Path(raw)
            _need(path.is_absolute(), "ownership paths must be absolute")
            absolute = path.resolve(strict=False)
            try:
                absolute.relative_to(workspace)
            except ValueError as exc:
                raise StateError("ownership path escapes workspace: " + raw) from exc
            resolved.append(str(absolute).casefold())
        return workspace, resolved

    @staticmethod
    def _overlap(left, right):
        sep = os.sep.casefold()
        for a in left:
            for b in right:
                if a == b or a.startswith(b.rstrip(sep) + sep) or b.startswith(a.rstrip(sep) + sep):
                    return True
        return False

    def _dependency_agent_ids(self, state, task, role=None):
        result = set()
        visited = set()
        stack = list(task.get("depends_on", []))
        while stack:
            task_id = stack.pop()
            if task_id in visited:
                continue
            visited.add(task_id)
            record = state["tasks"].get(task_id)
            if not record:
                continue
            stack.extend(record["versions"][-1]["task"].get("depends_on", []))
            for attempt in state["attempts"].values():
                if attempt["task_id"] == task_id and (role is None or attempt.get("role") == role) and attempt.get("agent_id"):
                    result.add(attempt["agent_id"])
        return sorted(result)

    def _dependency_versions(self, state, task):
        result, visited, stack = {}, set(), list(task.get("depends_on", []))
        while stack:
            task_id = stack.pop()
            if task_id in visited:
                continue
            visited.add(task_id)
            record = state["tasks"].get(task_id)
            if not record:
                continue
            latest = record["versions"][-1]
            result[task_id] = {"version": latest["version"], "input_digest": latest["input_digest"],
                               "validation_generation": record["validation_generation"]}
            stack.extend(latest["task"].get("depends_on", []))
        return result

    def _prepare(self, state, owner, p):
        from router_policy import PolicyError, build_packet, decide

        task_id = _nonempty(p.get("task_id"), "task_id")
        record, version = self._task_version(state, task_id)
        task = copy.deepcopy(version["task"])
        _need(record["verified"] is False, "task version is already verified")
        _need(not self._active_for_task(state, task_id), "task has an active or unknown dispatch attempt; reconcile it first")
        _need(record.get("direct_pending") is None, "task has a direct action awaiting completion or reconciliation")
        all_previous = sorted((a for a in state["attempts"].values() if a["task_id"] == task_id), key=lambda a: a["attempt_number"])
        previous = [a for a in all_previous if a["task_version"] == version["version"] and
                    a.get("validation_generation") == record["validation_generation"]]
        _need(len(all_previous) < MAX_ATTEMPTS, f"task attempt budget exhausted ({MAX_ATTEMPTS})")
        auth = record.get("retry_authorization")
        if record.get("model_failure_attempt_id") and record.get("model_failure_attempt_id") != record.get("resolved_model_failure_attempt_id"):
            _need(isinstance(auth, dict) and auth.get("human_authorized") is True and
                  auth.get("previous_attempt_id") == record["model_failure_attempt_id"],
                  "model failure is human-only; an explicit human retry authorization is required")
        if all_previous and all_previous[-1]["status"] not in {"verified"}:
            _need(isinstance(auth, dict), "retry authorization with controller reason is required")
            _need(auth.get("previous_attempt_id") == all_previous[-1]["attempt_id"],
                  "retry authorization does not match the latest attempt")
            _need(all_previous[-1]["status"] in {"failed", "no_spawn", "stopped"}, "previous attempt is not safely terminal")
        from check_protocol import retry_gate, initialize_history
        initialize_history(state, task_id)
        retry_gate(record)
        if previous:
            auth = record.get("retry_authorization")
            _need(isinstance(auth, dict), "retry authorization with root cause and fix is required")
            _need(previous[-1]["status"] in {"failed", "no_spawn", "stopped"}, "previous attempt is not safely terminal")
        self._depends_verified(state, task)
        workspace, owned_paths = self._path_set(task)
        catalog = p.get("catalog")
        provenance = p.get("catalog_provenance")
        _need(isinstance(provenance, dict), "catalog_provenance is required")
        for key in ("source", "captured_at", "entrypoint"):
            _nonempty(provenance.get(key), f"catalog_provenance.{key}")
        _need(provenance["source"] == "runtime-tool" and isinstance(catalog, dict) and catalog.get("source") == provenance["source"],
              "catalog source must match runtime-tool provenance")
        try:
            decision = decide(task, catalog)
        except (PolicyError, ValueError, TypeError) as exc:
            raise StateError(f"policy decision failed: {exc}") from exc
        from work_evidence import prepare_checks
        prepare_checks(self, state, task, decision, p)
        if decision["action"] != "blocked" and auth and auth.get("requested_model"):
            _need((auth["requested_model"], auth.get("requested_effort")) ==
                  (decision.get("model"), decision.get("effort")),
                  "retry decision differs from explicitly authorized model/effort")
        if decision["action"] != "delegate":
            if decision["action"] == "direct":
                _need(record.get("direct_pending") is None, "a direct action is already awaiting completion evidence")
                _, direct_paths = self._path_set(task)
                for other_task_id, other_record in state["tasks"].items():
                    pending_direct = other_record.get("direct_pending")
                    if other_task_id != task_id and pending_direct and self._overlap(direct_paths, pending_direct["owned_paths"]):
                        raise StateError("direct write ownership overlaps another active direct action")
                for other in self._active(state):
                    if not other["read_only"] and self._overlap(direct_paths, other["owned_paths"]):
                        raise StateError(f"direct write ownership overlaps active attempt {other['attempt_id']}")
                direct_id = str(uuid.uuid4())
                record["direct_pending"] = {"direct_id": direct_id, "task_version": version["version"],
                                            "policy_contract": "3.1",
                                            "input_digest": version["input_digest"], "decision": copy.deepcopy(decision),
                                            "catalog": copy.deepcopy(catalog), "catalog_provenance": copy.deepcopy(provenance),
                                            "validation_generation": record["validation_generation"],
                                            "dependency_versions": self._dependency_versions(state, task),
                                            "owned_paths": direct_paths, "read_only": False, "prepared_at": _now()}
                from check_protocol import initialize
                initialize(record['direct_pending'], task)
                from check_protocol import consume_retry
                consume_retry(record)
                self._event(state, "direct_prepared", owner, {"task_id": task_id, "direct_id": direct_id,
                                                                "input_digest": version["input_digest"],
                                                                "reason_codes": decision["reason_codes"]})
                return {"decision": decision, "dispatch": None, "direct_id": direct_id,
                        "check_contract": record['direct_pending']['check_contract'],
                        "input_digest": version["input_digest"], "status": "direct_pending",
                        "note": "No spawn was created; complete_direct requires execution and acceptance evidence."}
            self._event(state, "route_blocked", owner, {"task_id": task_id, "task_version": version["version"],
                                                          "input_digest": version["input_digest"], "decision": decision})
            return {"decision": decision, "dispatch": None, "note": "direct/blocked decisions do not create attempts"}
        if all_previous:
            auth = record["retry_authorization"]
            if all_previous[-1]["status"] != "verified":
                _need(auth.get("previous_attempt_id") == all_previous[-1]["attempt_id"],
                      "retry authorization does not match the latest attempt")
                _need(auth.get("controller_reason"), "retry requires controller reason based on root cause/fix")
            requested = (decision["model"], decision["effort"])
            prior = all_previous[-1]
            if record.get("model_failure_attempt_id") == prior["attempt_id"]:
                _need(auth["human_authorized"] is True, "model failure is human-only; explicit human authorization is required")
        if auth and auth.get("requested_model"):
            requested = (decision["model"], decision["effort"])
            _need(auth["requested_model"] == requested[0] and auth.get("requested_effort") == requested[1],
                  "retry decision differs from explicitly authorized model/effort")
        attempt_id = str(uuid.uuid4())
        try:
            packet = build_packet(task, decision, p.get("profiles_dir", ""), attempt_id)
        except (PolicyError, ValueError, OSError) as exc:
            raise StateError(f"could not build delegation packet: {exc}") from exc
        read_only = packet["read_only"]
        current_active = self._active(state)
        _need(len(current_active) < MAX_ACTIVE, f"active subagent limit reached ({MAX_ACTIVE})")
        if not read_only:
            for other in current_active:
                if not other["read_only"] and self._overlap(owned_paths, other["owned_paths"]):
                    raise StateError(f"write ownership overlaps active attempt {other['attempt_id']}")
            for other_task_id, other_record in state["tasks"].items():
                pending_direct = other_record.get("direct_pending")
                if other_task_id != task_id and pending_direct and self._overlap(owned_paths, pending_direct["owned_paths"]):
                    raise StateError(f"write ownership overlaps active direct action {pending_direct['direct_id']}")
        _need(all(other.get("agent_id") is None or other.get("agent_id") != "" for other in current_active), "active attempt identity is corrupt")
        task_snapshot_hash = input_digest(task)
        from packet_transport import materialize
        packet = materialize(packet,task,attempt_id,self.root,Path(__file__).with_name('routing.py'),task_snapshot_hash)
        attempt = {"attempt_id": attempt_id, "task_id": task_id, "task_version": version["version"],
                   "attempt_number": len(all_previous) + 1,
                   "validation_generation": record["validation_generation"],
                   "input_digest": task_snapshot_hash, "task_snapshot": task, "decision": decision,
                   "packet": packet, "role": decision["role"], "requested_model": decision["model"],
                   "effort": decision["effort"], "host_reported_model": None, "usage": "unknown",
                   "catalog_provenance": {"source": provenance["source"], "captured_at": provenance["captured_at"],
                                          "entrypoint": provenance["entrypoint"],
                                          "authentication_note": "Caller-supplied metadata; not independently authenticated."},
                   "host_slots_available": p.get('host_slots_available'),
                   "read_only": read_only, "owned_paths": owned_paths,
                   "dependency_agent_ids": self._dependency_agent_ids(state, task),
                   "dependency_versions": self._dependency_versions(state, task),
                   "planner_agent_ids": self._dependency_agent_ids(state, task, "sol_planner"),
                   "independence_required": decision.get("independence_required") is True,
                   "retry_authorization": copy.deepcopy(record.get("retry_authorization")),
                   "status": "dispatch_pending", "agent_id": None, "worker_stopped": False,
                   "created_at": _now(), "updated_at": _now(), "report": None, "verification": None,
                   "failure_category": None, "evidence": [], "retry_note": None}
        state["attempts"][attempt_id] = attempt
        from check_protocol import initialize
        initialize(attempt, task)
        from check_protocol import consume_retry
        consume_retry(record)
        if all_previous:
            state["tasks"][task_id]["retry_authorization"] = None
            used_auth = attempt.get("retry_authorization") or {}
            if used_auth.get("human_authorized") is True and state["tasks"][task_id].get("model_failure_attempt_id") == used_auth.get("previous_attempt_id"):
                state["tasks"][task_id]["resolved_model_failure_attempt_id"] = used_auth["previous_attempt_id"]
        self._event(state, "dispatch_prepared", owner, {"task_id": task_id, "attempt_id": attempt_id,
                                                          "input_digest": task_snapshot_hash,
                                                          "requested_model": decision["model"], "effort": decision["effort"]})
        return {"decision": decision, "attempt_id": attempt_id, "input_digest": task_snapshot_hash,
                "check_contract": attempt['check_contract'],
                "spawn_args": packet["spawn_args"], "status": "dispatch_pending",
                "usage": "unknown", "catalog_provenance": attempt["catalog_provenance"]}

    def _complete_direct(self, state, owner, p):
        from router_policy import PolicyError, direct_completion_decision

        task_id = _nonempty(p.get("task_id"), "task_id")
        record, version = self._task_version(state, task_id)
        pending = record.get("direct_pending")
        direct_id = _nonempty(p.get("direct_id"), "direct_id")
        if pending is None:
            completed = next((item for item in record["direct_completions"] if item.get("direct_id") == direct_id), None)
            _need(completed is not None, "task has no prepared direct action")
            if p.get('check_results') is not None:
                from check_protocol import replay
                replay(completed, p['check_results'], version['task'])
            _need(completed["task_version"] == version["version"] and completed["input_digest"] == version["input_digest"],
                  "direct completion belongs to an old task version")
            _need(completed.get("validation_generation") == record["validation_generation"] and
                  completed.get("dependency_versions") == self._dependency_versions(state, version["task"]),
                  "direct completion dependencies changed since prepare")
            _need(p.get("input_digest") == completed["input_digest"] and
                  p.get("execution_evidence") == completed["execution_evidence"] and
                  p.get("acceptance_evidence") == completed["acceptance_evidence"] and
                  p.get("verifier_context_id") == completed["verifier_context_id"] and
                  p.get('handoff') == completed.get('handoff') and
                  p.get("limitation_acknowledged", False) == completed["independence_limitation_acknowledged"],
                  "conflicting duplicate direct completion")
            return {"task_id": task_id, "status": "verified", "direct_id": direct_id,
                    "input_digest": completed["input_digest"], "idempotent": True}
        _need(isinstance(pending, dict), "task has no prepared direct action")
        _need(direct_id == pending["direct_id"], "direct action receipt mismatch")
        _need(p.get("input_digest") == pending["input_digest"] == version["input_digest"], "direct action input digest is stale")
        _need(pending["task_version"] == version["version"], "direct action belongs to an old task version")
        _need(pending["validation_generation"] == record["validation_generation"] and
              pending["dependency_versions"] == self._dependency_versions(state, version["task"]),
              "direct action dependencies changed since prepare")
        if p.get('check_results') is not None:
            outcome = self._record_check(state, owner, p)
            if outcome.get('requires_controller'): return outcome
        from check_protocol import accept
        accept(pending, version['task'])
        try:
            current_decision = direct_completion_decision(version["task"], pending["decision"],
                                                          pending["catalog"], pending.get("policy_contract"))
        except PolicyError as exc:
            raise StateError(str(exc)) from exc
        execution_evidence = _evidence(p.get("execution_evidence"), "execution_evidence")
        acceptance_evidence = _evidence(p.get("acceptance_evidence"), "acceptance_evidence")
        verifier = _nonempty(p.get("verifier_context_id"), "verifier_context_id")
        acknowledged = p.get("limitation_acknowledged", False)
        _need(type(acknowledged) is bool, "limitation_acknowledged must be a boolean")
        if current_decision.get("independence_required"):
            _need(version["task"]["delegation"]["prohibited"] is True,
                  "direct review completion is allowed only when the user explicitly prohibited delegation")
            _need(acknowledged is True, "direct completion must acknowledge reduced independence when delegation was unavailable")
        work_handoff = None
        if version['task'].get('mode')=='outline':
            from work_evidence import handoff
            work_handoff = handoff(version['task'], p.get('handoff'), accept=True)
        completion = {"direct_id": pending["direct_id"], "task_version": version["version"],
                      "validation_generation": record["validation_generation"],
                      "dependency_versions": copy.deepcopy(pending["dependency_versions"]),
                      "input_digest": version["input_digest"], "decision": copy.deepcopy(current_decision),
                      "prepared_decision": copy.deepcopy(pending["decision"]),
                      "prepared_policy_contract": pending.get("policy_contract"),
                      "delegation_source": version["task"]["delegation"]["source"],
                      "execution_evidence": execution_evidence, "acceptance_evidence": acceptance_evidence,
                      "verifier_context_id": verifier, "independence_limitation_acknowledged": acknowledged,
                      "at": _now()}
        record["direct_completions"].append(completion)
        completion['check_contract'] = pending.get('check_contract', 'unclassified_legacy')
        completion['check_records'] = copy.deepcopy(pending.get('check_records', {}))
        completion['check_observations'] = copy.deepcopy(pending.get('check_observations', []))
        if work_handoff is not None: completion['handoff'] = work_handoff
        record["direct_pending"] = None
        record["verified"] = True
        from check_protocol import initialize_history, register_success
        initialize_history(state, task_id)
        register_success(record, pending['direct_id'])
        self._event(state, "direct_completed", owner, {"task_id": task_id, "direct_id": pending["direct_id"],
                                                         "input_digest": version["input_digest"],
                                                         "independence_limitation_acknowledged": acknowledged})
        return {"task_id": task_id, "status": "verified", "direct_id": pending["direct_id"],
                "input_digest": version["input_digest"]}

    def _reconcile_direct(self, state, owner, p):
        task_id = _nonempty(p.get("task_id"), "task_id")
        record, _version = self._task_version(state, task_id)
        pending = record.get("direct_pending")
        _need(isinstance(pending, dict), "task has no pending direct action")
        direct_id = _nonempty(p.get("direct_id"), "direct_id")
        _need(direct_id == pending["direct_id"], "direct action receipt mismatch")
        action = p.get("action")
        _need(action in {"no_action", "stopped"}, "reconcile_direct action must be no_action or stopped")
        evidence = _evidence(p.get("evidence"))
        failure = pending.get('check_failure')
        if failure:
            _need(action == 'stopped', 'a recorded check failure cannot be reconciled as no_action')
        record.setdefault("direct_history", []).append({"direct_id": direct_id, "action": action,
                                                         "evidence": evidence, "at": _now(),
                                                         "receipt": copy.deepcopy(pending)})
        record["direct_pending"] = None
        self._event(state, "direct_reconciled", owner, {"task_id": task_id, "direct_id": direct_id,
                                                          "action": action, "evidence": evidence})
        return {"task_id": task_id, "direct_id": direct_id, "status": "reconciled", "action": action}

    def _attempt(self, state, p):
        attempt_id = _nonempty(p.get("attempt_id"), "attempt_id")
        attempt = state["attempts"].get(attempt_id)
        _need(attempt is not None, "unknown attempt_id")
        return attempt

    def _bind(self, state, owner, p):
        attempt = self._attempt(state, p)
        agent_id = _nonempty(p.get("agent_id"), "agent_id")
        _need(p.get("attempt_id") == attempt["attempt_id"], "attempt receipt mismatch")
        if attempt["status"] == "bound" and attempt.get("agent_id") == agent_id:
            return {"attempt_id": attempt["attempt_id"], "status": "bound", "agent_id": agent_id, "idempotent": True}
        _need(attempt["status"] == "dispatch_pending", "only a pending dispatch can be bound")
        for other in self._active(state):
            if other["attempt_id"] != attempt["attempt_id"] and other.get("agent_id") == agent_id:
                raise StateError("one agent context cannot occupy multiple active tasks")
        if any(other["attempt_id"] != attempt["attempt_id"] and other.get("agent_id") == agent_id
               for other in state["attempts"].values()):
            raise StateError("agent context was already bound to a historical attempt; use a fresh context")
        if attempt["role"] == "sol_architect":
            _need(agent_id not in attempt["dependency_agent_ids"], "reviewer context must differ from implementation/planner contexts in dependency chain")
        attempt["agent_id"] = agent_id
        attempt["status"] = "bound"
        attempt["updated_at"] = _now()
        self._event(state, "dispatch_bound", owner, {"attempt_id": attempt["attempt_id"], "agent_id": agent_id})
        return {"attempt_id": attempt["attempt_id"], "status": "bound", "agent_id": agent_id}

    def _record_check(self, state, owner, p):
        """Persist real failures rather than raising and discarding their evidence."""
        from check_protocol import record, register_failure, initialize_history
        direct = 'direct_id' in p
        if direct:
            task_record, version = self._task_version(state, p.get('task_id'))
            receipt = task_record.get('direct_pending')
            _need(isinstance(receipt, dict) and receipt['direct_id'] == p['direct_id'], 'direct check receipt mismatch')
            task = version['task']
        else:
            receipt = self._attempt(state, p)
            _need(receipt['status'] in ('bound', 'reported', 'failure_reported'), 'check requires bound/reported/failing worker')
            _need(p.get('agent_id') == receipt['agent_id'], 'check agent mismatch')
            task = receipt['task_snapshot']
            task_record = state['tasks'][task['task_id']]
        _need(p.get('input_digest') == receipt['input_digest'], 'check input_digest mismatch')
        initialize_history(state, task['task_id'])
        failure = record(receipt, task, p.get('check_results'),
                         failure_only=not direct and receipt['status'] == 'failure_reported')
        self._event(state, 'checks_recorded', owner, {'task_id': task['task_id'],
                    'receipt_id': receipt['direct_id'] if direct else receipt['attempt_id'],
                    'check_ids': [r['id'] for r in p['check_results'] if isinstance(r,dict) and isinstance(r.get('id'),str)]})
        if failure:
            register_failure(task_record, receipt, failure)
            category = failure['verdict']['category']
            if direct:
                receipt['check_failure'] = copy.deepcopy(failure)
                return {'status': 'direct_check_failed', 'requires_controller': True,
                        'category': category, 'direct_id': receipt['direct_id']}
            stopped = p.get('host_stopped_evidence')
            if stopped is not None and not (isinstance(stopped,str) and stopped.strip()):
                receipt.setdefault('check_protocol_errors', []).append('invalid host_stopped_evidence; stop remains unconfirmed')
                stopped = None
            result = self._fail(state, owner, {'attempt_id': receipt['attempt_id'],
                'input_digest': receipt['input_digest'],
                'agent_id': receipt['agent_id'], 'category': category,
                'evidence': [failure['result']['evidence_ref'], 'Frozen check result: '+json.dumps(failure, ensure_ascii=False)],
                'host_stopped_evidence': stopped})
            result['requires_controller'] = True
            return result
        return {'status': 'checks_recorded', 'requires_controller': False,
                'check_contract': receipt['check_contract']}

    def _report(self, state, owner, p):
        attempt = self._attempt(state, p)
        _need(p.get("attempt_id") == attempt["attempt_id"], "attempt receipt mismatch")
        _need(p.get("input_digest") == attempt["input_digest"], "report input_digest does not match prepared input")
        _need(attempt['status'] in ('bound', 'reported', 'failure_reported'), 'only a bound/reported/failing worker can report')
        _need(p.get('agent_id') == attempt['agent_id'], 'reporting agent does not match bound worker')
        if attempt['status'] == 'failure_reported':
            from check_protocol import contains_failure
            _need(contains_failure(attempt['task_snapshot'],p.get('check_results')),
                  'failing worker report must contain a new attributable failure, not green or completion')
        if p.get('check_results') is not None:
            outcome = self._record_check(state, owner, p)
            if outcome.get('requires_controller'): return outcome
        from check_protocol import accept
        accept(attempt, attempt['task_snapshot'])
        from packet_transport import validate_receipt
        validate_receipt(attempt,p.get('packet_sha256'))
        evidence = _evidence(p.get("evidence"))
        report = {"attempt_id": attempt["attempt_id"], "input_digest": attempt["input_digest"],
                  "agent_id": _nonempty(p.get("agent_id"), "agent_id"), "summary": _nonempty(p.get("summary"), "summary"),
                  "evidence": evidence, "host_reported_model": p.get("host_reported_model"),
                  "host_reported_effort": p.get("host_reported_effort"), "host_stopped_evidence": p.get("host_stopped_evidence"),
                  "at": _now()}
        if attempt['packet'].get('transport') is not None: report['packet_sha256'] = p.get('packet_sha256')
        if attempt['task_snapshot'].get('mode')=='outline':
            from work_evidence import handoff
            report['handoff'] = handoff(attempt['task_snapshot'], p.get('handoff'))
        if attempt["status"] == "reported":
            old = attempt["report"]
            for key in ("attempt_id", "input_digest", "agent_id", "summary", "evidence", "host_reported_model", "host_reported_effort", "host_stopped_evidence", "handoff", "packet_sha256"):
                _need(old.get(key) == report.get(key), "conflicting duplicate report")
            return {"attempt_id": attempt["attempt_id"], "status": "reported", "idempotent": True}
        _need(attempt["status"] == "bound", "only a bound worker can report")
        _need(report["agent_id"] == attempt["agent_id"], "reporting agent does not match bound worker")
        if report["host_reported_model"] is not None:
            _nonempty(report["host_reported_model"], "host_reported_model")
            attempt["host_reported_model"] = report["host_reported_model"]
        if report["host_reported_effort"] is not None:
            _nonempty(report["host_reported_effort"], "host_reported_effort")
        if report["host_stopped_evidence"] is not None:
            _nonempty(report["host_stopped_evidence"], "host_stopped_evidence")
            attempt["worker_stopped"] = True
            attempt["stop_evidence"] = report["host_stopped_evidence"]
        attempt["report"] = report
        attempt["evidence"] = evidence
        attempt["status"] = "reported"
        attempt["updated_at"] = _now()
        self._event(state, "worker_reported", owner, {"attempt_id": attempt["attempt_id"], "input_digest": attempt["input_digest"]})
        return {"attempt_id": attempt["attempt_id"], "status": "reported", "worker_stopped": attempt["worker_stopped"]}

    def _verify(self, state, owner, p):
        attempt = self._attempt(state, p)
        _need(p.get("attempt_id") == attempt["attempt_id"], "attempt receipt mismatch")
        _need(p.get("input_digest") == attempt["input_digest"], "verification input_digest does not match prepared input")
        verifier = _nonempty(p.get("verifier_context_id"), "verifier_context_id")
        evidence = _evidence(p.get("evidence"), "verification evidence")
        task_record, current_version = self._task_version(state, attempt["task_id"])
        _need(attempt["task_version"] == current_version["version"] and attempt["input_digest"] == current_version["input_digest"],
              "attempt belongs to an old task input version and cannot verify the current input")
        _need(attempt.get("validation_generation") == task_record["validation_generation"],
              "attempt belongs to an invalidated validation generation")
        _need(attempt.get("dependency_versions") == self._dependency_versions(state, current_version["task"]),
              "attempt dependencies changed since prepare; old evidence cannot verify this input")
        from packet_transport import validate_receipt
        validate_receipt(attempt,(attempt.get('report') or {}).get('packet_sha256'))
        if attempt["status"] == "verified":
            old = attempt["verification"]
            _need(old["verifier_context_id"] == verifier and old["evidence"] == evidence, "conflicting duplicate verification")
            return {"attempt_id": attempt["attempt_id"], "status": "verified", "idempotent": True}
        _need(attempt["status"] == "reported", "only a reported attempt can be verified")
        from check_protocol import accept
        accept(attempt, attempt['task_snapshot'])
        _need(task_record["verified"] is False, "task version is already verified")
        if attempt['task_snapshot'].get('mode')=='outline':
            from work_evidence import handoff
            handoff(attempt['task_snapshot'], attempt['report'].get('handoff'), accept=True)
        _need(attempt["worker_stopped"] is True, "worker must have stopped with explicit host evidence before verification")
        _need(verifier != attempt["agent_id"], "verification must use a context independent from the worker")
        _need(verifier not in attempt["dependency_agent_ids"], "verification context must differ from implementation/planner contexts in dependency chain")
        host_model = attempt.get("host_reported_model")
        host_effort = (attempt.get("report") or {}).get("host_reported_effort")
        _need(host_model is None or host_model == attempt["requested_model"], "host-reported model differs from requested model")
        _need(host_effort is None or host_effort == attempt["effort"], "host-reported effort differs from requested effort")
        verification = {"verifier_context_id": verifier, "evidence": evidence, "at": _now()}
        attempt["verification"] = verification
        attempt["evidence"].extend(evidence)
        attempt["status"] = "verified"
        attempt["updated_at"] = _now()
        state["tasks"][attempt["task_id"]]["verified"] = True
        from check_protocol import initialize_history, register_success
        initialize_history(state, attempt['task_id'])
        register_success(task_record, attempt['attempt_id'])
        self._event(state, "attempt_verified", owner, {"attempt_id": attempt["attempt_id"], "verifier_context_id": verifier})
        return {"attempt_id": attempt["attempt_id"], "status": "verified", "task_id": attempt["task_id"]}

    def _fail(self, state, owner, p):
        attempt = self._attempt(state, p)
        _need(p.get("attempt_id") == attempt["attempt_id"], "attempt receipt mismatch")
        category = p.get("category")
        _need(category in {"model", "input", "environment", "validation", "checker"}, "failure category must be model/input/environment/validation/checker")
        evidence = _evidence(p.get("evidence"))
        _need(attempt["status"] in {"dispatch_pending", "bound", "reported", "failure_reported"}, "failure requires a pending/bound/reported/failing worker")
        if attempt['status'] == 'failure_reported':
            _need(p.get('input_digest') == attempt['input_digest'], 'late failure input_digest mismatch')
        if attempt["status"] == "dispatch_pending":
            _need(category == "model", "pending dispatch failure without agent receipt is only valid for model unavailability")
            _need(attempt.get("agent_id") is None and p.get("agent_id") is None, "unbound failure must not claim an agent id")
        else:
            _need(p.get("agent_id") == attempt["agent_id"], "failure agent does not match bound worker")
        stopped = p.get("host_stopped_evidence")
        if stopped is not None:
            if not (isinstance(stopped,str) and stopped.strip()):
                attempt.setdefault('check_protocol_errors', []).append('invalid host_stopped_evidence; stop remains unconfirmed')
                stopped = None
            else:
                stopped = stopped.strip()
        no_spawn_evidence = None
        if attempt["status"] == "dispatch_pending":
            no_spawn_evidence = _nonempty(p.get("host_no_spawn_evidence"), "host_no_spawn_evidence")
        from check_protocol import initialize_history, register_failure
        initialize_history(state, attempt['task_id'])
        attempt.setdefault('failure_observations', []).append({'category':category, 'evidence':copy.deepcopy(evidence), 'at':_now()})
        category = 'model' if 'model' in (attempt.get('failure_category'),category) else (attempt.get('failure_category') or category)
        attempt["failure_category"] = category
        register_failure(state['tasks'][attempt['task_id']], attempt, {'verdict': {'category':category}})
        if category == "model":
            state["tasks"][attempt["task_id"]]["model_failure_attempt_id"] = attempt["attempt_id"]
        attempt["evidence"].extend(evidence)
        attempt.setdefault('failure_evidence', copy.deepcopy(evidence))
        stopped_confirmed = bool(stopped or no_spawn_evidence or attempt.get("worker_stopped"))
        attempt["status"] = "failed" if stopped_confirmed else "failure_reported"
        attempt["worker_stopped"] = stopped_confirmed
        if stopped or no_spawn_evidence:
            attempt["stop_evidence"] = stopped or no_spawn_evidence
        attempt["updated_at"] = _now()
        self._event(state, "attempt_failed", owner, {"attempt_id": attempt["attempt_id"], "category": category, "worker_stopped": bool(stopped)})
        return {"attempt_id": attempt["attempt_id"], "status": attempt["status"], "category": category,
                "retry_policy": "human-only" if category == "model" else "requires root-cause/fix note and stopped worker"}

    def _reconcile(self, state, owner, p):
        attempt = self._attempt(state, p)
        _need(p.get("attempt_id") == attempt["attempt_id"], "attempt receipt mismatch")
        action = p.get("action")
        evidence = _evidence(p.get("evidence"))
        if attempt["status"] in {"no_spawn", "stopped"} and attempt.get("worker_stopped"):
            prior_action = "no_spawn" if attempt["status"] == "no_spawn" else "stopped"
            _need(action == prior_action and attempt.get("stop_evidence") == evidence, "conflicting duplicate reconciliation")
            return {"attempt_id": attempt["attempt_id"], "status": attempt["status"], "worker_stopped": True, "idempotent": True}
        _need(attempt["status"] in ACTIVE, "only active/unknown attempts need reconciliation")
        if attempt["worker_stopped"] and attempt["status"] == "reported":
            _need(action == "stopped" and attempt.get("stop_evidence") == evidence, "conflicting duplicate reconciliation")
            return {"attempt_id": attempt["attempt_id"], "status": "reported", "worker_stopped": True, "idempotent": True}
        if action == "no_spawn":
            _need(attempt["status"] == "dispatch_pending", "no_spawn only applies before a bind receipt")
            attempt["status"] = "no_spawn"
            attempt["worker_stopped"] = True
            attempt["stop_evidence"] = evidence
        elif action == "stopped":
            if attempt["status"] == "failure_reported":
                attempt["status"] = "failed"
            elif attempt["status"] == "reported":
                attempt["status"] = "reported"
            else:
                attempt["status"] = "stopped"
            attempt["worker_stopped"] = True
            attempt["stop_evidence"] = evidence
        else:
            raise StateError("reconcile action must be no_spawn or stopped")
        attempt["evidence"].extend(evidence)
        attempt["updated_at"] = _now()
        self._event(state, "attempt_reconciled", owner, {"attempt_id": attempt["attempt_id"], "action": action, "evidence": evidence})
        return {"attempt_id": attempt["attempt_id"], "status": attempt["status"], "worker_stopped": True}

    def _retry(self, state, owner, p):
        if 'direct_id' in p:
            record, _ = self._task_version(state, p.get('task_id'))
            _need(record.get('direct_pending') is None, 'direct retry requires confirmed stop')
            history = record.get('direct_history', [])
            _need(history and history[-1]['direct_id'] == p['direct_id'] and history[-1]['action'] == 'stopped',
                  'direct retry must reference the latest stopped direct receipt')
            failure = history[-1].get('receipt', {}).get('check_failure')
            _need(failure is not None, 'direct retry requires a recorded check failure')
            note = _nonempty(p.get('controller_reason', p.get('root_cause_and_fix')), 'controller_reason/root_cause_and_fix')
            _need(type(p.get('human_authorized')) is bool, 'human_authorized must be a boolean')
            if failure['verdict']['category'] == 'model':
                _need(p['human_authorized'], 'model failure retry is human-only')
            auth = {'receipt_id':p['direct_id'], 'controller_reason':note,
                    'human_authorized':p['human_authorized'], 'at':_now()}
            record['check_retry_authorization'] = auth
            self._event(state,'direct_retry_authorized',owner,{'task_id':p['task_id'], **auth})
            return {'task_id':p['task_id'], 'retry_authorization':auth}
        attempt = self._attempt(state, p)
        latest = max((item for item in state["attempts"].values() if item["task_id"] == attempt["task_id"]),
                     key=lambda item: item["attempt_number"], default=None)
        _need(latest is not None and latest["attempt_id"] == attempt["attempt_id"],
              "retry must reference the latest task attempt")
        _need(attempt["status"] in TERMINAL and attempt["status"] in {"failed", "stopped", "no_spawn"}, "retry requires a safely terminal previous attempt")
        _need(attempt["worker_stopped"] is True, "retry requires explicit evidence that the prior worker stopped")
        note = _nonempty(p.get("controller_reason", p.get("root_cause_and_fix")), "controller_reason/root_cause_and_fix")
        human_authorized = p.get("human_authorized")
        _need(type(human_authorized) is bool, "human_authorized must be a boolean")
        requested_model = p.get("requested_model")
        requested_effort = p.get("requested_effort")
        if requested_model is not None or requested_effort is not None:
            _nonempty(requested_model, "requested_model")
            _nonempty(requested_effort, "requested_effort")
            _need(isinstance(p.get("controller_reason", p.get("root_cause_and_fix")), str), "model/effort change requires a recorded controller reason")
        if attempt.get("failure_category") == "model":
            _need(human_authorized is True, "model failure retry is human-only")
        if state["tasks"][attempt["task_id"]].get("model_failure_attempt_id") == attempt["attempt_id"]:
            _need(human_authorized is True, "model failure retry is human-only")
        auth = {"root_cause_and_fix": note, "controller_reason": note, "human_authorized": human_authorized,
                "requested_model": requested_model, "requested_effort": requested_effort,
                "previous_attempt_id": attempt["attempt_id"], "at": _now()}
        state["tasks"][attempt["task_id"]]["retry_authorization"] = auth
        state['tasks'][attempt['task_id']]['check_retry_authorization'] = {
            'receipt_id':attempt['attempt_id'], 'controller_reason':note,
            'human_authorized':human_authorized, 'at':_now()}
        self._event(state, "retry_authorized", owner, {"task_id": attempt["task_id"], "previous_attempt_id": attempt["attempt_id"],
                                                        "human_authorized": human_authorized, "requested_model": requested_model,
                                                        "requested_effort": requested_effort, "root_cause_and_fix": note})
        return {"task_id": attempt["task_id"], "retry_authorization": auth}

    def _handoff(self, state, owner, p):
        new_owner = _nonempty(p.get("new_owner"), "new_owner")
        evidence = _evidence(p.get("evidence"), "handoff evidence")
        unresolved = [a["attempt_id"] for a in self._active(state)]
        unresolved_direct = [v["direct_pending"]["direct_id"] for v in state["tasks"].values() if v.get("direct_pending")]
        _need(not unresolved and not unresolved_direct,
              "handoff requires all dispatches/direct actions resolved; unresolved ids: " + ", ".join(unresolved + unresolved_direct))
        old_owner, old_epoch = state["coordinator_id"], state["epoch"]
        state["coordinator_id"] = new_owner
        state["epoch"] = old_epoch + 1
        self._event(state, "handoff", owner, {"from": old_owner, "to": new_owner, "old_epoch": old_epoch,
                                               "new_epoch": state["epoch"], "evidence": evidence})
        return {"coordinator_id": new_owner, "epoch": state["epoch"], "status": "handed_off"}


def template():
    """Return a complete task template with unmistakable fill-in placeholders."""
    return {"note": "Replace every TEMPLATE value. Runtime model catalog must be captured from the current tool declaration; this example is not live model evidence. current is main-thread-only. Omit override unless the user explicitly requires one model for the whole task; then supply scope=all_tasks, model and source. Add override.effort only for an explicit whole-task effort instruction.",
            "task": {"task_id": "TEMPLATE_TASK_ID", "goal": "TEMPLATE_GOAL", "input": "TEMPLATE_INPUT",
                     "packet_transport": "file",
                     "check_plan": [{"id": "TEMPLATE_CHECK_ID", "command": "TEMPLATE_EXACT_COMMAND",
                                     "kind": "acceptance", "expected_exit_codes": [0], "expected_signal": ""}],
                     "workspace": "TEMPLATE_ABSOLUTE_WORKSPACE", "ownership": ["TEMPLATE_ABSOLUTE_PATH_WITHIN_WORKSPACE"],
                     "constraints": ["TEMPLATE_CONSTRAINT"], "forbidden": ["TEMPLATE_FORBIDDEN_ACTION"],
                     "success": ["TEMPLATE_OBSERVABLE_SUCCESS"], "verification": ["TEMPLATE_VERIFICATION"],
                     "escalation": ["TEMPLATE_ESCALATION"], "depends_on": [], "input_version": "TEMPLATE_INPUT_VERSION",
                     "features": {"stage": "execution", "ambiguity": "clear", "risk": "normal", "complexity": "normal",
                                  "deterministic": False, "multi_source": False, "plan_ready": False,
                                  "required_tools": [], "available_tools": []},
                     "delegation": {"authorized": False, "prohibited": False, "source": "TEMPLATE_AUTHORIZATION_SOURCE"},
                     "approval": {"required": False, "granted": False},
                     "current": {"model": "TEMPLATE_CURRENT_MODEL", "effort": "TEMPLATE_CURRENT_EFFORT"}}}
