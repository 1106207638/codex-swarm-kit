"""Scoped, read-only collaboration recap and controller-authored public decisions."""
import os
from collections import Counter

import call_log
from router_policy import public_summary
from task_state import _file_lock, _need


CHOICES = {"adopted": "采纳", "partial": "部分采纳", "returned": "退回", "unused": "未采用"}
ROLES = {"sol_architect": "独立审查", "sol_planner": "任务规划", "local_worker": "外接本地智能体",
         "luna_worker": "执行", "luna_deep": "复杂执行", "luna_general": "整理",
         "terra_general": "综合分析", "spark_worker": "定点修改"}
STATES = {"dispatch_pending": "已准备，调用情况待核实", "bound": "已收到调用回执，尚无终态",
          "reported": "已交回，待总控验收", "verified": "已验收", "failed": "失败",
          "failure_reported": "已报失败，待确认停止", "stopped": "已停止", "no_spawn": "已确认未启动"}
ATTEMPT_OPS = {"bind", "report", "verify", "fail", "reconcile", "record_check", "retry",
               "external_bind", "external_poll", "external_resume", "recap_note"}


def record_note(store, state, owner, payload):
    attempt = store._attempt(state, payload)
    _need(payload.get("input_digest") == attempt["input_digest"], "recap input mismatch")
    choice = payload.get("choice")
    _need(choice in CHOICES, "invalid controller disposition")
    note = {"choice": choice}
    for field in ("assignment", "output", "reason"):
        value = payload.get(field)
        _need(public_summary(value), f"{field} requires a short single-line public summary")
        note[field] = value.strip()
    prior = attempt.get("recap_note")
    if prior is not None:
        _need(prior == note, "conflicting disposition on the same attempt")
    else:
        record = state["tasks"][attempt["task_id"]]
        if choice in ("adopted", "partial"):
            _need(attempt["status"] == "verified" and record["verified"] and
                  attempt["validation_generation"] == record["validation_generation"],
                  "adoption requires current controller verification")
        else:
            _need(attempt["status"] in ("verified", "failed", "stopped") and attempt.get("agent_id"),
                  "return or nonuse requires a bound terminal attempt")
        attempt["recap_note"] = note
        store._event(state, "recap_note", owner, {"attempt_id": attempt["attempt_id"]})
    return {"attempt_id": attempt["attempt_id"], "status": attempt["status"], "idempotent": prior is not None}


def select_tasks(state, scope):
    _need(isinstance(scope, dict) and len(scope) == 1, "select one explicit recap scope")
    tasks = state["tasks"]
    if "outline" in scope:
        outline = state.get("outline")
        _need(outline and scope["outline"] == outline["spec"]["ref"], "outline scope mismatch")
        return [tid for tid, rec in tasks.items() if rec["versions"][0]["task"].get("mode") == "outline"]
    if "group" in scope:
        group = scope["group"]
        _need(isinstance(group, str) and group.strip(), "group is required")
        selected = [tid for tid, rec in tasks.items() if rec["versions"][0]["task"].get("recap_scope") == group
                    and rec["versions"][0]["task"].get("mode") != "outline"]
        _need(selected, "unknown recap group")
        return selected
    ids = scope.get("legacy_tasks")
    _need(isinstance(ids, list) and ids and all(isinstance(t, str) and t in tasks for t in ids)
          and len(set(ids)) == len(ids), "legacy replay needs explicit existing task ids")
    _need(all(not tasks[t]["versions"][0]["task"].get("recap_scope") and
              tasks[t]["versions"][0]["task"].get("mode") != "outline" for t in ids),
          "use the complete group or outline for a scoped task")
    return ids


def collect(store, owner, epoch, scope):
    path = store.root / call_log.FILE
    if not call_log._regular(path):
        state = store.status(owner, epoch)
        selected = set(select_tasks(state, scope))
        if not call_log._regular(path):
            attempts = [a for a in state["attempts"].values() if a["task_id"] in selected]
            return state, selected, attempts, {a["attempt_id"]: [] for a in attempts}, 0, []
    lock = store.root / "calls.lock"
    call_log._regular(lock)
    with _file_lock(lock):
        state = store.status(owner, epoch)
        selected = set(select_tasks(state, scope))
        attempts = [a for a in state["attempts"].values() if a["task_id"] in selected]
        by_hash = {call_log.masked(a["attempt_id"]): a["attempt_id"] for a in attempts}
        by_prepare = {a["prepare_call_id"]: a["attempt_id"] for a in attempts if a.get("prepare_call_id")}
        task_refs = {call_log.masked(t) for t in selected}
        logs = {a["attempt_id"]: [] for a in attempts}
        routing_issues = []
        skipped = 0
        if call_log._regular(path):
            with path.open("rb") as stream:
                call_log._check_file(os.fstat(stream.fileno()))
                for number, (line, _) in enumerate(call_log._rows(stream), 1):
                    try:
                        row = call_log._parse(line)
                    except (ValueError, TypeError, RecursionError):
                        skipped += 1
                        continue
                    aid = None
                    if row["op"] == "prepare":
                        aid = by_prepare.get(row["call_id"])
                    elif row["op"] in ATTEMPT_OPS and not (row["op"] in ("retry", "record_check") and row.get("direct_ref")):
                        aid = by_hash.get(row.get("attempt_ref"))
                    if aid:
                        logs[aid].append((number, row))
                    elif ((row["op"] in {"prepare", "complete_direct", "reconcile_direct"} or
                           (row["op"] in {"add", "update"} and row.get("nested_task_ref") == row.get("task_ref")))
                          and row.get("task_ref") in task_refs and row["phase"] == "finish"
                          and row.get("outcome") in {"rejected", "blocked", "needs_attention"}):
                        routing_issues.append({"line": number, "task_ref": row["task_ref"], "op": row["op"],
                                               "id": row["call_id"], "outcome": row["outcome"],
                                               "reason": row.get("error_code") or row.get("reason_codes")})
    return state, selected, attempts, logs, skipped, routing_issues


def describe(attempt, state, selected, logs):
    tid, aid = attempt["task_id"], attempt["attempt_id"]
    task = state["tasks"][tid]
    status = attempt["status"]
    if attempt.get("agent_id"):
        dispatch = "confirmed"
    elif status in ("no_spawn", "failed"):
        dispatch = "not_started"
    else:
        dispatch = "unknown"
    # Preserve every failed/retried invocation while counting one worker execution per attempt.
    calls = {}
    for number, row in logs:
        call = calls.setdefault(row["call_id"], {"op": row["op"], "id": row["call_id"]})
        call[row["phase"]] = number
        if row["phase"] == "finish":
            call["outcome"] = row.get("outcome")
            call["result_status"] = row.get("result_status")
    pairs = [c for c in calls.values() if "start" in c and "finish" in c and c.get("outcome") != "rejected"]
    external = attempt["decision"].get("executor") == "external"
    needed = {"prepare": "dispatch_pending"}
    if dispatch == "confirmed":
        needed["external_bind" if external else "bind"] = "bound"
    if attempt.get("report"):
        needed["external_poll" if external else "report"] = "reported"
    if status == "verified":
        needed["verify"] = "verified"
    if attempt.get("recap_note"):
        needed["recap_note"] = status
    gaps = sorted(op for op, expected in needed.items()
                  if not any(c["op"] == op and c["result_status"] == expected for c in pairs))
    if any("start" not in c or "finish" not in c for c in calls.values()):
        gaps.append("incomplete_call")
    if status in ("failed", "failure_reported"):
        failure_states = {c["result_status"] for c in pairs if c["op"] in {"fail","report","record_check","external_poll"}}
        if not failure_states.intersection({"failed", "failure_reported"}):
            gaps.append("failure")
        elif (status == "failed" and "failed" not in failure_states and
              not any(c["op"] == "reconcile" and c["result_status"] == "failed" for c in pairs)):
            gaps.append("failure_stop")
    if status in ("stopped","no_spawn") and not any(
            c["op"] == "reconcile" and c["result_status"] == status for c in pairs):
        gaps.append("reconcile")
    return {"task_ref": call_log.masked(tid), "attempt_id": aid, "number": attempt["attempt_number"],
            "role": attempt["role"], "dispatch": dispatch, "status": status,
            "assignment": attempt["task_snapshot"].get("recap_assignment"),
            "actual_model": "unknown" if external else call_log.model_name(attempt.get("host_reported_model")),
            "external_model_ref": call_log.masked(attempt.get("host_reported_model")) if external else None,
            "actual_effort": call_log.pick((attempt.get("report") or {}).get("host_reported_effort"), call_log.EFFORTS) or "unknown",
            "depends_on": [call_log.masked(t) for t in attempt["task_snapshot"].get("depends_on", []) if t in selected],
            "current_input": attempt["validation_generation"] == task["validation_generation"],
            "failure_category": attempt.get("failure_category"), "decision": attempt.get("recap_note"),
            "calls": list(calls.values()), "log_gaps": gaps}


def read(store, owner, epoch, scope):
    state, selected, attempts, logs, skipped, issues = collect(store, owner, epoch, scope)
    dispatch_order = {event["data"]["attempt_id"]: event["seq"] for event in state["events"]
                      if event["type"] == "dispatch_prepared" and event.get("data", {}).get("attempt_id")}
    ordered = sorted(attempts, key=lambda a: (a["created_at"], dispatch_order.get(a["attempt_id"], 0)))
    rows = [describe(a, state, selected, logs[a["attempt_id"]]) for a in ordered]
    counts = Counter(r["dispatch"] for r in rows)
    needs_decision = [r["attempt_id"] for r in rows if r["dispatch"] == "confirmed" and
                      r["status"] in {"verified", "failed", "stopped"} and not r["decision"]]
    headline = ("本次任务使用了蜂群协作。" if counts["confirmed"] else
                "有蜂群派发记录，执行情况待核实。" if counts["unknown"] else "")
    lines = [headline] if headline else []
    labels = {r["task_ref"]: (r["decision"] or {}).get("assignment") or r["assignment"] or
              ROLES.get(r["role"], "子任务") for r in rows}
    for row in rows:
        note = row["decision"]
        role = ROLES.get(row["role"], row["role"])
        lifecycle = STATES[row["status"]]
        if row["dispatch"] == "not_started":
            lifecycle += "（未启动）"
        elif row["dispatch"] == "unknown" and row["status"] != "dispatch_pending":
            lifecycle += "（执行情况待核实）"
        if not row["current_input"]:
            lifecycle += "（历史版本，当前验收须另核）"
        detail = (f'{note["assignment"]}。{note["output"]}。总控{CHOICES[note["choice"]]}，{note["reason"]}'
                  if note else (row["assignment"] or "缺少公开分工摘要") + "。总控尚无可公开的用途记录")
        if row["depends_on"]:
            detail += "。依据前置工作：" + "、".join(labels.get(dep, "总控工作项") for dep in row["depends_on"])
        refs = [str(c.get("finish", c.get("start"))) for c in row["calls"]]
        trace = "calls.jsonl 行 " + "、".join(refs) if refs else "无调用日志行"
        lines.append(f'{role} · 第{row["number"]}次 · {lifecycle}。{detail}。{trace}。')
        if row["log_gaps"]:
            lines.append("日志证据不全：" + "、".join(row["log_gaps"]))
    if skipped:
        lines.append(f"台账日志有 {skipped} 行无法读取，归属待核实。")
    for issue in issues:
        lines.append(f'另有调度操作 {issue["op"]} 返回 {issue["outcome"]}。calls.jsonl 行 {issue["line"]}。')
    return {"format": "swarm-recap/v1", "used": bool(counts["confirmed"]), "headline": headline,
            "confirmed_attempts": counts["confirmed"], "unknown_attempts": counts["unknown"],
            "not_started_attempts": counts["not_started"], "skipped_log_lines": skipped,
            "attempts": rows, "needs_decision": needs_decision, "routing_issues": issues, "text": "\n".join(lines)}
