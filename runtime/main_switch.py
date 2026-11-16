"""Stage the next desktop turn's settings from the current main thread."""
from __future__ import annotations

import copy
import hashlib
import hmac
import json
import os
import re
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from platform_runtime import codex_home

from resolve_family_models import ResolutionError, select_family
from router_policy import PolicyError, _catalog_model, _validate_task, merged_pins, controller_restriction, outline_collaboration, validate_pins
from task_state import MAX_ATTEMPTS, StateError, _need, _nonempty


ENTRYPOINT = "mcp__codex_app__send_message_to_thread"
FINISHED = {"confirmed", "cancelled", "resolved", "rejected_before_start", "superseded"}
STATES = FINISHED | {"prepared", "ready", "sending", "accepted", "staged", "unknown", "mismatch"}
MODE = "desktop_self_next_turn"
WINDOW = timedelta(minutes=5)
VERIFICATION_ANSWER = "允许本聊天续接并验证（推荐）"
VERIFICATION_QUESTION = (
    "为验证真实切换，是否允许适配器向本聊天发送少量带目标模型和推理强度的续接消息？"
    "仅用于本次实现与验收，不给其他聊天发消息。"
)
GOAL_VERIFICATION_ANSWER = "允许本聊天发送两条换模验证消息（推荐）"
GOAL_VERIFICATION_QUESTION = (
    "为完成本聊天的 Goal 换模验收，是否允许发送最多两条可见的设置请求消息："
    "先切到 Sol/max 做只读核验，再回到 Astra/high？仅本聊天，不给其他聊天发消息。"
)
DELEGATION = re.compile(
    r"<codex_delegation>\n  <source_thread_id>([0-9a-f-]{36})</source_thread_id>\n"
    r"  <input>(.*)</input>\n</codex_delegation>", re.DOTALL,
)


def _now():
    return datetime.now(timezone.utc)


def _time(value):
    dt = datetime.fromisoformat(_nonempty(value, "timestamp"))
    _need(dt.tzinfo is not None, "timestamp needs a timezone")
    return dt


def _settings(record, effort_key="effort"):
    return {"model": record.get("model"), "effort": record.get(effort_key)}


def _uuid(value, label):
    value = _nonempty(value, label)
    _need(str(uuid.UUID(value)) == value, f"{label} must be a canonical UUID")
    return value


def _permit_hash(permit):
    return hashlib.sha256(_uuid(permit, "send_permit").encode("ascii")).hexdigest()


def _require_caller(thread_id):
    _need(os.environ.get("CODEX_THREAD_ID") == thread_id,
          "settings writes must run inside the target main chat")


def _user_authorizes(item, grant):
    # A matching quote alone is insufficient: recognize positive, scoped
    # consent and its send budget. Tool output and delegation are excluded.
    if not item.get("client_id") or item.get("type") != "UserMessage":
        return False
    content = item.get("content", [])
    if len(content) != 1 or content[0].get("type") != "text":
        return False
    value = content[0].get("text", "").strip()
    if grant["purpose"] in {"task_continuation", "resolve_switch"}:
        return value == grant["answer"]
    tag = "send_user_message_question_reply"
    if not (value.startswith(f"<{tag}>") and value.endswith(f"</{tag}>")):
        return False
    try:
        entries = json.loads(value[len(tag) + 2:-len(tag) - 3])
    except ValueError:
        return False
    answer, question = ((GOAL_VERIFICATION_ANSWER, GOAL_VERIFICATION_QUESTION)
                        if grant["purpose"] == "goal_verification" else (VERIFICATION_ANSWER, VERIFICATION_QUESTION))
    return isinstance(entries, list) and any(
        isinstance(row, dict) and row.get("answer") == answer and
        isinstance(row.get("question"), str) and row["question"].startswith(question)
        for row in entries
    )


def _session_path(path, thread_id):
    thread_id = _uuid(thread_id, "thread_id")
    sessions = (codex_home() / 'sessions').resolve()
    path = Path(_nonempty(path, "rollout_path")).resolve(strict=True)
    _need(path.is_relative_to(sessions) and path.name.startswith("rollout-") and
          path.name.endswith(f"-{thread_id}.jsonl"), "use the matching local Codex session rollout")
    return path


def _check_session(stream, thread_id):
    meta = json.loads(stream.readline())
    _need(meta.get("type") == "session_meta" and meta["payload"].get("id") == thread_id,
          "rollout session id differs from the target chat")


def _rollout(path, thread_id, *, grant=None, prompt=None):
    """Read host metadata and the required input marker, never export history."""
    path = _session_path(path, thread_id)
    turns = {}
    settings = None
    settings_events = []
    last_human = None
    last_human_sequence = None
    grant_at = None
    with path.open("rb") as stream:
        _check_session(stream, thread_id)
        for sequence, line in enumerate(stream):
            if not line.endswith(b"\n"):
                break  # a concurrent append has not finished
            if not (b'"turn_context"' in line[:150] or b'"event_msg"' in line[:150]):
                continue
            if not any(tag in line[:240] for tag in (b'"turn_context"', b'"task_started"', b'"task_complete"',
                                                      b'"turn_aborted"', b'"thread_settings_applied"', b'"item_completed"')):
                continue
            entry = json.loads(line)
            data = entry.get("payload", {})
            turn_id = data.get("turn_id")
            typ = data.get("type")
            at = entry.get("timestamp")
            if entry["type"] == "turn_context" and turn_id in turns:
                turn = turns[turn_id]
                context = dict(_settings(data), at=at)
                turn.setdefault("first_context", context)
                turn["context"] = context
                settings_seen = turn.setdefault("settings_seen", [])
                if _settings(context) not in settings_seen:
                    settings_seen.append(_settings(context))
            elif entry["type"] != "event_msg":
                continue
            elif typ == "task_started":
                duplicate = turn_id in turns
                turns.setdefault(turn_id, {"turn_id": turn_id, "status": "inProgress", "started_at": at,
                                           "started_sequence": sequence, "inputs": 0, "work_started": False})
                if duplicate:
                    turns[turn_id]["duplicate_start"] = True
                else:
                    turns[turn_id]["attribution"] = copy.deepcopy(data.get("turn_attribution"))
                    turns[turn_id]["root_turn_id"] = data.get("root_turn_id")
            elif typ in {"task_complete", "turn_aborted"} and turn_id in turns:
                if turns[turn_id]["status"] != "interrupted":
                    turns[turn_id].update(status="completed" if typ == "task_complete" else "interrupted",
                                          ended_at=at, ended_sequence=sequence)
            elif typ == "thread_settings_applied" and data.get("thread_id") == thread_id:
                settings = _settings(data["thread_settings"], "reasoning_effort")
                settings_events.append(dict(settings, at=at, sequence=sequence))
            elif typ == "item_completed" and turn_id in turns:
                item = data["item"]
                turn = turns[turn_id]
                if item.get("type") == "UserMessage":
                    turn["inputs"] += 1
                    if item.get("client_id"):
                        last_human = item.get("id")
                        last_human_sequence = sequence
                        if turn["inputs"] == 1 and not turn["work_started"]:
                            turn["started_by_human"] = True
                    if grant and item.get("id") == grant["message_id"] and _user_authorizes(item, grant):
                        grant_at = at
                elif (item.get("type"), item.get("namespace"), item.get("name")) == (
                    "FunctionCallOutput", "codex_app", "send_message_to_thread"
                ):
                    turn["inputs"] += 1
                    output = item.get("output", "")
                    if prompt and isinstance(output, str):
                        match = DELEGATION.fullmatch(output)
                        if match and match[2] == prompt:
                            turn.setdefault("matching_inputs", []).append({
                                "kind": "codex_app.send_message_to_thread", "item_id": item["id"],
                                "source_thread_id": match[1], "at": at, "sequence": sequence,
                            })
                elif item.get("type") != "ContextCompaction":
                    turn["work_started"] = True
    _need(not grant or grant_at, "the specified human authorization was not found in this chat")
    return {"turns": turns, "settings": settings, "settings_events": settings_events,
            "last_human": last_human, "last_human_sequence": last_human_sequence, "grant_at": grant_at}


def _goal_identity(value, thread_id):
    # Python validates a supplied binding, not the authenticity/freshness of a
    # native tool call. The desktop bridge obtains get_goal directly each time.
    _need(isinstance(value, dict) and value.get("threadId") == thread_id and
          value.get("status") == "active", "the same chat Goal must be active")
    created = value.get("createdAt")
    _need(type(created) is int and created > 0, "Goal creation identity missing")
    objective = _nonempty(value.get("objective"), "Goal objective")
    return {"thread_id": thread_id, "created_at": created,
            "objective_sha256": hashlib.sha256(objective.encode("utf-8")).hexdigest()}


def _check_goal_state(request, payload):
    if request.get("next_source", "human") == "goal":
        _need(_goal_identity(payload.get("goal_state"), request["thread_id"]) == request["goal_binding"],
              "Goal identity or objective changed")


def _goal_root(facts, turn_id):
    """Validate native attribution, never the continuation prompt text.

    Ancestors may have acquired authorized inputs after their automatic start.
    Only the new successor has a zero-input condition during acceptance.
    """
    turns = facts["turns"]; order = list(turns); seen = set(); root = None
    while True:
        _need(turn_id in turns and turn_id not in seen, "Goal lineage missing or cyclic")
        seen.add(turn_id); turn = turns[turn_id]; attr = turn.get("attribution")
        _need(isinstance(attr, dict) and
              {"turn_id", "turn_trigger", "parent_turn_id", "root_turn_id", "initiating_agent_path"} <= set(attr) and
              attr["turn_id"] == turn_id and attr["initiating_agent_path"] is None and
              not turn.get("duplicate_start") and turn["status"] != "interrupted",
              "Goal lineage has untrusted attribution")
        if root is None:root = _uuid(attr["root_turn_id"], "Goal root turn")
        _need(attr["root_turn_id"] == root == turn.get("root_turn_id"), "Goal root attribution differs")
        if attr["turn_trigger"] == "composer":
            _need(turn_id == root and attr["parent_turn_id"] is None and turn.get("started_by_human"),
                  "Goal lineage needs a native human composer root")
            return root
        _need(attr["turn_trigger"] == "goal", "Goal lineage contains a different trigger")
        parent = attr["parent_turn_id"]; i = order.index(turn_id)
        _need(i > 0 and parent == order[i - 1], "Goal parent must be the immediate preceding turn")
        previous = turns[parent]
        _need(previous["status"] == "completed" and
              previous.get("ended_sequence", -1) < turn["started_sequence"],
              "Goal parent did not finish before its child")
        turn_id = parent


def observe(path, thread_id):
    facts = _rollout(path, thread_id)
    latest = next(reversed(facts["turns"].values()), None)
    return {"thread_id": thread_id, "latest_turn": latest, "thread_settings": facts["settings"],
            "latest_human_message_id": facts["last_human"]}


def recommend(task, catalog, pins, substantial):
    """Select a main-thread target without changing child-agent routing."""
    f = _validate_task(task)
    validate_pins(pins)
    try:
        pins = merged_pins(task, pins)
    except PolicyError as exc:
        return {"action": "blocked", "reason": "conflicting_explicit_pins", "detail": str(exc)}
    _need(type(substantial) is bool, "substantial must be a boolean")
    _need(isinstance(pins, dict) and set(pins) <= {"model", "effort", "no_switch", "source"},
          "pins must be an object containing only explicit main-thread constraints")
    if pins:
        _nonempty(pins.get("source"), "explicit pin source")
        _need(set(pins) != {"source"}, "pin source must describe a pinned field or no_switch")
    for key in ("model", "effort"):
        if key in pins:
            _nonempty(pins[key], f"pins.{key}")
    _need(type(pins.get("no_switch", False)) is bool, "no_switch must be a boolean")
    locked = {key: pins[key] for key in ("model", "effort") if key in pins}
    current = _settings(task["current"])
    whole = task.get("override")
    if whole is not None:
        if whole.get("scope") != "all_tasks" or not isinstance(whole.get("source"), str) or not whole["source"].strip():
            return {"action": "blocked", "reason": "ambiguous_whole_task_restriction"}
        for key in ("model", "effort"):
            if key in whole:
                if key in locked and locked[key] != whole[key]:
                    return {"action": "blocked", "reason": "conflicting_explicit_pins"}
                locked[key] = whole[key]
    mismatch = any(current[key] != value for key, value in locked.items())
    outline = outline_collaboration(task)
    if outline and not pins.get("no_switch") and "model" not in locked:
        mismatch = mismatch or current["model"] != "gpt-6-astra"
    if task["approval"]["required"] and not task["approval"]["granted"]:
        return {"action": "blocked", "reason": "approval_required"}
    if set(f["required_tools"]) - set(f["available_tools"]):
        return {"action": "blocked", "reason": "required_tools_unavailable"}
    if pins.get("no_switch") or (not substantial and not mismatch):
        return ({"action": "blocked", "reason": "fixed_settings_require_manual_selection"} if mismatch
                else {"action": "keep", "reason": "explicit_no_switch_or_short_phase", "target": current})

    complex_phase = f["complexity"] == "complex"
    if outline:
        family, effort, reason = "astra", "max" if complex_phase else "high", "outline_controller"
    elif f["stage"] == "strategy" or f["risk"] == "high" or f["ambiguity"] == "conflict":
        family, effort, reason = "astra", "max" if complex_phase else "high", "strategic_or_high_risk"
    elif f["stage"] in {"planning", "review"} or f["ambiguity"] == "unknown" or (
        f["stage"] == "execution" and not f["plan_ready"]
    ):
        family, effort, reason = "sol", "max" if complex_phase or f["stage"] == "review" else "high", "planning_or_analysis"
    elif f["stage"] == "research" and f["multi_source"]:
        family, effort, reason = "terra", "high", "multi_source_research"
    elif f["stage"] == "execution" and f["deterministic"]:
        family, effort, reason = "luna", "high" if complex_phase else "medium", "clear_execution"
    else:
        family, effort, reason = "sol", "high", "technical_work"
    effort = locked.get("effort", effort)
    try:
        model = locked.get("model")
        if model is None:
            model = "gpt-6-astra" if family == "astra" else select_family(catalog, family, effort)
        target = {"model": model, "effort": effort}
        if target == current:
            return {"action": "keep", "reason": "already_suitable", "target": current}
        _catalog_model(catalog, model, effort)
    except (ResolutionError, PolicyError) as exc:
        return {"action": "blocked" if mismatch else "keep", "reason": "target_not_advertised",
                "detail": str(exc), "target": None if mismatch else current}
    return {"action": "switch", "reason": reason, "target": target, "required": mismatch}


def pending_switch(state):
    return next((r for r in state.get("main_switches", {}).values() if r["status"] not in FINISHED), None)


def validate_switches(state):
    records = state.get("main_switches", {})
    _need(isinstance(records, dict), "invalid main switch history")
    for rid, r in records.items():
        _need(isinstance(r, dict) and r.get("request_id") == rid and r.get("status") in STATES and
              r.get("task_id") in state["tasks"] and type(r.get("epoch")) is int and 1 <= r["epoch"] <= state["epoch"] and
              (r.get("helper_attempt_id") is None or r["helper_attempt_id"] in state["attempts"]),
              "invalid main switch record")
    _need(sum(r["status"] not in FINISHED for r in records.values()) <= 1, "multiple unresolved main switches")


def guard(state, op, p):
    request = pending_switch(state)
    if request is None:
        return
    if op in {"prepare", "update", "handoff"}:
        raise StateError("resolve the main-thread switch before starting or changing other work")


def _provenance(p, now):
    provenance = p.get("catalog_provenance")
    _need(isinstance(provenance, dict) and provenance.get("source") == "runtime-tool" and
          provenance.get("entrypoint") == ENTRYPOINT, "use the current desktop send tool catalog")
    _need(timedelta(0) <= now - _time(provenance.get("captured_at")) <= WINDOW, "desktop catalog is stale")
    return provenance


def _grant(value, resolving=None):
    _need(isinstance(value, dict) and set(value) == {"message_id", "answer", "purpose", "max_sends", "reviewed_through"},
          "an explicit, scoped human chat-message grant is required")
    for key in ("message_id", "answer", "purpose", "reviewed_through"):
        _nonempty(value[key], f"authorization.{key}")
    budget = value["max_sends"]
    _need(type(budget) is int, "authorization must bound the number of sends")
    if resolving:
        _need(value["purpose"] == "resolve_switch" and budget == 0 and
              value["answer"] == f"允许解除本聊天续接请求 {resolving} 的冻结",
              "resolution needs explicit human authorization for this request")
    elif value["purpose"] == "verification":
        _need(budget == 1 and value["answer"] == VERIFICATION_ANSWER,
              "the verification grant covers only one recorded continuation")
    elif value["purpose"] == "goal_verification":
        _need(budget == 2 and value["answer"] == GOAL_VERIFICATION_ANSWER,
              "Goal verification needs an explicit two-message grant")
    else:
        _need(value["purpose"] == "task_continuation" and 1 <= budget <= MAX_ATTEMPTS and
              value["answer"] == f"允许在本聊天为当前任务自动续接{budget}次",
              "human authorization must explicitly allow this chat and the requested send count")
    return value


def _no_other_actions(store, state):
    _need(not store._active(state) and
          not any(t.get("direct_pending") for t in state["tasks"].values()),
          "old actions are not all stopped")


def _verification_sequence(state, request):
    if request["authorization"]["purpose"] != "goal_verification":
        return
    _need(request.get("next_source") == "goal", "verification requires a Goal successor")
    prior = [r for r in state.get("main_switches", {}).values()
             if r["request_id"] != request["request_id"] and
             r["authorization"]["message_id"] == request["authorization"]["message_id"] and
             r.get("claimed_at") is not None]
    home = {"model": "gpt-6-astra", "effort": "high"}
    if not prior:
        _need(request["before"] == home and request["target"]["effort"] == "max" and
              re.fullmatch(r"gpt-\d+(?:\.\d+)*-sol", request["target"]["model"]),
              "verification first switches Astra/high to Sol/max")
    else:
        _need(len(prior) == 1 and prior[0]["status"] == "confirmed" and
              prior[0]["task_id"] == request["task_id"] and
              prior[0].get("goal_binding") == request.get("goal_binding") and
              prior[0].get("goal_root_turn_id") == request.get("goal_root_turn_id") and
              prior[0].get("host_evidence", {}).get("source_valid") is True and
              request["before"] == prior[0]["target"] and request["target"] == home,
              "verification return requires the confirmed Sol step in the same task and Goal")


def _save(store, state, owner, record, status, **facts):
    record.update(status=status, updated_at=_now().isoformat(), **facts)
    store._event(state, "main_switch_" + status, owner, {"request_id": record["request_id"], **facts})
    return copy.deepcopy(record)


def prepare(store, state, owner, p):
    next_source = p.get("next_source", "human")
    _need(next_source in ("human", "goal"), "next_source must be human or goal")
    _need(pending_switch(state) is None, "a main-thread switch is unresolved; do not reissue")
    task_id = _nonempty(p.get("task_id"), "task_id")
    record, version = store._task_version(state, task_id)
    _need(not record["verified"], "main switch requires a current unverified task phase")
    _need(record.get("model_failure_attempt_id") == record.get("resolved_model_failure_attempt_id"),
          "business task has an unresolved model failure; existing human-only recovery applies")
    task = version["task"]
    decision = recommend(task, p.get("catalog"), p.get("pins", {}), p.get("substantial"))
    if decision["action"] != "switch":
        return {"decision": decision}
    _no_other_actions(store, state)
    now = _now()
    provenance = _provenance(p, now)
    thread_id = _uuid(p.get("thread_id"), "thread_id")
    _need(p.get("host_id") == "local", "only the local desktop host is supported")
    _need(owner == thread_id + "-root", "main switch is scoped to the owner chat")
    _require_caller(thread_id)
    before = _uuid(p.get("before_turn_id"), "before_turn_id")
    phase = _nonempty(p.get("phase"), "stable phase key")
    grant = _grant(p.get("authorization"))
    _need(grant["purpose"] != "goal_verification" or next_source == "goal",
          "Goal verification messages require an explicit Goal successor")
    path = _nonempty(p.get("rollout_path"), "rollout_path")
    facts = _rollout(path, thread_id, grant=grant)
    _need(facts["last_human"] == grant["reviewed_through"],
          "latest human input must be reviewed for changed authorization and task scope")
    latest = next(reversed(facts["turns"].values()), None)
    _need(latest and latest["turn_id"] == before and latest["status"] == "inProgress" and
          _settings(latest.get("context", {})) == _settings(task["current"]),
          "active main turn or observed runtime settings no longer match the task")
    history = state.setdefault("main_switches", {})
    used = [r for r in history.values() if r["thread_id"] == thread_id]
    _need(all(r["before_turn_id"] != before for r in used), "a switch was already prepared for this turn")
    task_history = [r for r in used if r["task_id"] == task_id]
    _need(len(task_history) < MAX_ATTEMPTS and all(r["phase"] != phase or r["status"] == "rejected_before_start"
                                                   for r in task_history),
          "switch budget or phase already used")
    _need(all(_time(facts["grant_at"]) > _time(r["resolution_at"]) for r in task_history if r["status"] == "resolved"),
          "continuation after an uncertain send requires a new human send grant")
    prior_grant = [r for r in used if r["authorization"]["message_id"] == grant["message_id"]]
    _need(all(r["task_id"] == task_id and all(r["authorization"][key] == grant[key]
              for key in ("answer", "purpose", "max_sends")) for r in prior_grant),
          "a chat-message grant cannot change scope or send budget")
    _need(sum(r.get("claimed_at") is not None and r["status"] != "rejected_before_start"
              for r in prior_grant) < grant["max_sends"],
          "this chat-message grant has no remaining sends")
    root = Path(task["workspace"]).resolve(strict=True)
    checkpoint = Path(_nonempty(p.get("checkpoint"), "checkpoint")).resolve(strict=True)
    _need(checkpoint.is_relative_to(root) and checkpoint.is_file(), "checkpoint must be an existing file within the task workspace")
    rid = str(uuid.uuid4())
    marker = f"[codex-main-switch:{rid}]"
    prompt = (
        f"为本聊天保存下一轮的模型设置。{marker}\n"
        "这条记录只用于核对设置，当前轮次按原任务正常结束。"
    )
    history[rid] = request = {
        "request_id": rid, "mode": MODE, "thread_id": thread_id, "host_id": "local", "before_turn_id": before,
        "before": _settings(task["current"]), "before_settings": facts["settings"],
        "before_human": facts["last_human"], "target": decision["target"], "phase": phase,
        "task_id": task_id, "input_digest": version["input_digest"], "epoch": state["epoch"],
        "authorization": copy.deepcopy(grant), "catalog_provenance": copy.deepcopy(provenance),
        "rollout_path": path, "prompt": prompt, "checkpoint": str(checkpoint),
        "pins": copy.deepcopy(merged_pins(task, p.get("pins", {}))),
        "fixed_settings": controller_restriction(task, p.get("pins", {})),
        "created_at": now.isoformat(), "expires_at": (now + WINDOW).isoformat(),
    }
    if next_source == "goal":
        request.update(next_source="goal", goal_binding=_goal_identity(p.get("goal_state"), thread_id),
                       goal_root_turn_id=_goal_root(facts, before), before_inputs=latest["inputs"],
                       before_human_sequence=facts["last_human_sequence"],
                       before_settings_sequence=(facts["settings_events"][-1]["sequence"]
                                                 if facts["settings_events"] else -1))
    _verification_sequence(state, request)
    return {"decision": decision, "request": _save(store, state, owner, request, "prepared")}


def get(state, request_id):
    record = state.get("main_switches", {}).get(_uuid(request_id, "request_id"))
    _need(record is not None, "unknown main switch request")
    return record


def _check_old_turn(request):
    facts = _rollout(request["rollout_path"], request["thread_id"], grant=request["authorization"])
    latest = next(reversed(facts["turns"].values()), None)
    _need(latest and latest["turn_id"] == request["before_turn_id"] and latest["status"] == "inProgress" and
          latest.get("settings_seen") == [request["before"]] and
          facts["last_human"] == request["before_human"] and facts["settings"] == request["before_settings"],
          "chat input, settings or turn boundary changed; do not send")
    if request.get("next_source", "human") == "goal":
        _need(_goal_root(facts, request["before_turn_id"]) == request["goal_root_turn_id"] and
              latest["inputs"] == request["before_inputs"] and
              facts["last_human_sequence"] == request["before_human_sequence"] and
              (facts["settings_events"][-1]["sequence"] if facts["settings_events"] else -1)
              == request["before_settings_sequence"],
              "Goal lineage or frozen input position changed; do not send")
    return latest


def _native_args(r):
    return {"threadId": r["thread_id"], "hostId": r["host_id"], "model": r["target"]["model"],
            "thinking": r["target"]["effort"], "prompt": r["prompt"]}


def _reconcile_ancestor_rejection(store, state, owner, r, p):
    _need(r.get("mode") is None and r["status"] == "unknown",
          "only the older native helper request can have an ancestor rejection")
    _no_other_actions(store, state)
    attempt = state["attempts"].get(r.get("helper_attempt_id"), {})
    _need(attempt.get("worker_stopped") is True, "native helper must be evidenced terminal")
    helper_id = r["helper_thread_id"]
    path = _session_path(p.get("helper_rollout_path"), helper_id)
    matches = []
    with path.open("rb") as stream:
        _check_session(stream, helper_id)
        for line in stream:
            if b'"item_completed"' not in line[:240] or b'"send_message_to_thread"' not in line:
                continue
            event = json.loads(line)
            item = event.get("payload", {}).get("item", {})
            if (event.get("type"), item.get("type"), item.get("server"), item.get("tool")) == (
                "event_msg", "McpToolCall", "codex_app", "send_message_to_thread"
            ) and item.get("arguments") == _native_args(r):
                matches.append((event["timestamp"], item))
    _need(len(matches) == 1, "one exact native receipt is required")
    at, item = matches[0]
    result = item.get("result", {})
    prefix = f"send_message_to_thread cannot send to your native ancestor (thread ID: {r['thread_id']})."
    _need(item.get("status") == "failed" and item.get("pluginId") == "codex-app-tools@openai-bundled" and
          result.get("isError") is True and _time(at) >= _time(r["claimed_at"]) and
          any(block.get("type") == "text" and block.get("text", "").startswith(prefix)
              for block in result.get("content", [])), "native receipt does not prove rejection before sending")
    return _save(store, state, owner, r, "rejected_before_start",
                 native_rejection={"reason": prefix, "at": at, "helper_thread_id": helper_id})


def transition(store, state, op, owner, p):
    if op == "main_switch_prepare":
        return prepare(store, state, owner, p)
    r = get(state, p.get("request_id"))
    status = r["status"]
    _require_caller(r["thread_id"])
    if op == "main_switch_confirm":
        return confirm(store, state, owner, r, p)
    _need(r["epoch"] == state["epoch"], "switch belongs to a previous ledger epoch")
    if op == "main_switch_reconcile_rejection":
        return _reconcile_ancestor_rejection(store, state, owner, r, p)
    if op == "main_switch_resolve":
        _need(status in {"unknown", "mismatch", "sending", "accepted", "staged"}, "switch does not need uncertainty resolution")
        _no_other_actions(store, state)
        resolution = _grant(p.get("authorization"), resolving=r["request_id"])
        facts = _rollout(r["rollout_path"], r["thread_id"], grant=resolution)
        _need(facts["last_human"] == resolution["message_id"] == resolution["reviewed_through"] and
              _time(facts["grant_at"]) > _time(r["claimed_at"]),
              "use a fresh human decision after this send was attempted")
        return _save(store, state, owner, r, "resolved",
                     resolution=resolution, resolution_at=facts["grant_at"],
                     stop_evidence=_nonempty(p.get("evidence"), "host resolution evidence"))
    if op == "main_switch_cancel" and status in {"prepared", "ready"}:
        _no_other_actions(store, state)
        return _save(store, state, owner, r, "cancelled", outcome=_nonempty(p.get("outcome"), "cancellation evidence"))
    _need(r.get("mode") == MODE, "native helpers cannot send to their ancestor; this route is disabled")
    _no_other_actions(store, state)
    if op == "main_switch_claim":
        _need(status == "prepared" and _now() < _time(r["expires_at"]), "switch is not prepared, expired or already claimed")
        _verification_sequence(state, r)
        _check_goal_state(r, p)
        _check_old_turn(r)
        claim = _now().isoformat()
        permit = str(uuid.uuid4())
        r["send_permit_hash"] = _permit_hash(permit)
        _save(store, state, owner, r, "sending", claimed_at=claim)
        return {"request_id": r["request_id"], "status": "sending", "send_permit": permit, "native_args": _native_args(r)}
    if op == "main_switch_check":
        _need(status == "sending" and _now() < _time(r["expires_at"]) and
              hmac.compare_digest(r["send_permit_hash"], _permit_hash(p.get("send_permit"))),
              "last check needs the current, unexpired send permit")
        _verification_sequence(state, r)
        _check_goal_state(r, p)
        _check_old_turn(r)
        return {"can_invoke": True}
    if op == "main_switch_cancel":
        _need(status == "sending" and hmac.compare_digest(r["send_permit_hash"], _permit_hash(p.get("send_permit"))),
              "only the claimant can cancel its unsent claim")
        return _save(store, state, owner, r, "cancelled", outcome=_nonempty(p.get("outcome"), "no-send evidence"))
    if op == "main_switch_receipt":
        _need(status == "sending", "receipt requires the single outstanding send")
        _need(hmac.compare_digest(r["send_permit_hash"], _permit_hash(p.get("send_permit"))),
              "receipt does not belong to the send claimant")
        if p.get("receipt_thread_id") == r["thread_id"] and p.get("outcome") == "accepted":
            return _save(store, state, owner, r, "accepted", receipt_thread_id=p["receipt_thread_id"])
        return _save(store, state, owner, r, "unknown", outcome=_nonempty(p.get("outcome"), "receipt failure evidence"))
    raise StateError("unknown main switch operation: " + op)


def confirm(store, state, owner, request, payload=None):
    # Re-read a false-negative input classification without authorizing a send.
    # Unknown receipts and actual runtime mismatches remain frozen.
    previous = request.get("host_evidence", {})
    goal_source = request.get("next_source", "human") == "goal"
    recheck = (not goal_source and request["status"] == "mismatch" and
               previous.get("started_by_human") is False and
               previous.get("context") and _settings(previous["context"]) == request["target"] and
               previous.get("settings_seen") == [request["target"]] and
               "staging_evidence" in request and not request.get("outcome"))
    if request.get("mode") != MODE or (request["status"] not in {"accepted", "staged"} and not recheck):
        return copy.deepcopy(request)
    _check_goal_state(request, payload or {})
    facts = _rollout(request["rollout_path"], request["thread_id"], prompt=request["prompt"])
    old_id = request["before_turn_id"]
    turns = facts["turns"]
    old = turns.get(old_id)
    _need(old is not None, "old runtime turn missing from the session record")
    later = list(turns.values())[list(turns).index(old_id) + 1:]
    # Revalidate saved stages too: an older parser may have accepted a
    # timestamp tie whose actual event order was invalid.
    claimed = _time(request["claimed_at"])
    inputs = old.get("matching_inputs", [])
    marker_sequence = inputs[0]["sequence"] if len(inputs) == 1 else -1
    changes = [event for event in facts["settings_events"]
               if claimed <= _time(event["at"]) and event["sequence"] < marker_sequence]
    matches = (len(inputs) == 1 and inputs[0]["source_thread_id"] == request["thread_id"] and
               _time(inputs[0]["at"]) >= claimed and changes and _settings(changes[-1]) == request["target"] and
               ("ended_sequence" not in old or marker_sequence < old["ended_sequence"]) and
               old.get("settings_seen") == [request["before"]])
    if goal_source:
        goal_changes = [event for event in facts["settings_events"]
                        if event["sequence"] > request["before_settings_sequence"]]
        matches = (matches and old["inputs"] == request["before_inputs"] + 1 and
                   facts["last_human"] == request["before_human"] and
                   facts["last_human_sequence"] == request["before_human_sequence"] and
                   all(_time(event["at"]) >= claimed and _settings(event) == request["target"]
                       for event in goal_changes))
    if not matches:
        if recheck or (request["status"] == "accepted" and old["status"] == "inProgress"):
            return copy.deepcopy(request)  # receipt may precede its log append
        return _save(store, state, owner, request, "mismatch", outcome="missing_or_conflicting_settings_evidence")
    staging = {"old_turn_id": old_id,
               "input": {key: value for key, value in inputs[0].items() if key != "sequence"},
               "thread_settings": {key: value for key, value in changes[-1].items() if key != "sequence"}}
    if request["status"] == "accepted":
        _save(store, state, owner, request, "staged", staging_evidence=staging)
    elif staging != request["staging_evidence"]:
        return copy.deepcopy(request) if recheck else _save(
            store, state, owner, request, "mismatch", outcome="staged_input_evidence_changed")
    if not later:
        return copy.deepcopy(request)
    turn = later[0]
    context = turn.get("first_context")
    if turn["status"] == "inProgress" and (not context or (not turn["inputs"] and not turn["work_started"])):
        return copy.deepcopy(request)  # rollout append still in progress
    evidence = {"thread_id": request["thread_id"], "turn_id": turn["turn_id"],
                "started_at": turn["started_at"], "context": context, "settings_seen": turn.get("settings_seen", []),
                "started_by_human": turn.get("started_by_human", False)}
    after_marker = marker_sequence
    next_setting_events = [event for event in facts["settings_events"]
                           if after_marker < event["sequence"] < turn["started_sequence"]]
    next_settings = next_setting_events[-1] if next_setting_events else None
    expected = _settings(next_settings) if next_settings else request["target"]
    # Keep exported evidence stable across parser versions; order is internal.
    evidence["settings_before_start"] = ({key: value for key, value in next_settings.items() if key != "sequence"}
                                         if next_settings else None)
    source_ok = evidence["started_by_human"]
    if goal_source:
        attr = turn.get("attribution") or {}
        try:
            root = _goal_root(facts, turn["turn_id"])
            source_ok = (root == request["goal_root_turn_id"] and attr.get("turn_trigger") == "goal" and
                         attr.get("parent_turn_id") == old_id and turn["inputs"] == 0)
        except (StateError, ValueError, TypeError):
            source_ok = False
        evidence.update(continuation_source="goal", source_valid=source_ok,
                        attribution=copy.deepcopy(attr), goal_binding=copy.deepcopy(request["goal_binding"]))
    if (not context or old["status"] != "completed" or
            not after_marker < old.get("ended_sequence", -1) < turn["started_sequence"] or
            not source_ok or turn["status"] == "interrupted"):
        status = "mismatch"
    elif turn["settings_seen"] != [expected] or not all(expected.values()):
        status = "mismatch"
    elif expected == request["target"]:
        status = "confirmed"
    elif not goal_source and all(expected[key] == value for key, value in request["fixed_settings"].items()):
        status = "superseded"
    else:
        status = "mismatch"
    if recheck:
        # Only the human-input classification may change. Keep all former
        # host evidence, including the exact turn, settings and timestamps.
        if status != "confirmed" or dict(previous, started_by_human=True) != evidence:
            return copy.deepcopy(request)
        return _save(store, state, owner, request, status, host_evidence=evidence,
                     evidence_correction={"reason": "initial_input_classification",
                                          "previous_status": "mismatch", "previous_host_evidence": previous})
    return _save(store, state, owner, request, status, host_evidence=evidence)
