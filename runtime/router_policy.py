"""Conservative policy selection and complete, constrained delegation packets."""
from __future__ import annotations

import hashlib
import json
import re
import tomllib
from pathlib import Path

from resolve_family_models import ResolutionError, resolve


class PolicyError(ValueError):
    """The task or runtime catalog is incomplete or violates policy."""


_ENUMS = {
    "stage": {"execution", "planning", "review", "research", "strategy"},
    "ambiguity": {"clear", "unknown", "conflict"},
    "risk": {"low", "normal", "high"},
    "complexity": {"simple", "normal", "complex"},
}
_LIST_FIELDS = ("constraints", "forbidden", "success", "verification", "escalation")
_PROFILE_FIELDS = {"name", "description", "model", "model_reasoning_effort", "sandbox_mode", "developer_instructions"}
_DELEGATE_ROLES = {"luna_worker", "luna_deep", "luna_general", "terra_general", "sol_architect", "sol_planner"}
_PROFILE_EFFORTS = {"sol_architect": "max", "sol_planner": "high", "luna_worker": "medium",
                    "luna_general": "medium", "luna_deep": "high", "terra_general": "high"}


def _need(condition, message):
    if not condition:
        raise PolicyError(message)


def _strings(value, name, *, nonempty=True):
    _need(isinstance(value, list) and (not nonempty or bool(value)), f"{name} must be a {'nonempty ' if nonempty else ''}list of strings")
    _need(all(isinstance(item, str) and item.strip() for item in value), f"{name} must contain only nonempty strings")


def _validate_task(task):
    _need(isinstance(task, dict), "task must be an object")
    _need(task.get('packet_transport','inline') in ('inline','file'), 'packet_transport must be inline or file')
    _need(task.get("mode", "normal") in ("normal", "outline"), "mode must be normal or outline")
    if task.get("mode") == "outline":
        _need(task.get("operation") in ("business", "controller"), "outline operation must be business or controller")
    elif "operation" in task:
        _need(False, "operation requires explicit outline mode")
    validate_pins(task.get("controller_pins", {}))
    for name in ("task_id", "goal", "input"):
        _need(isinstance(task.get(name), str) and task[name].strip(), f"{name} must be a nonempty string")
    _need(task["task_id"] == task["task_id"].strip(), "task_id must not contain leading or trailing whitespace")
    for name in _LIST_FIELDS:
        _strings(task.get(name), name)
    _need(isinstance(task.get("input"), str), "input must be a string")
    workspace = task.get("workspace")
    _need(isinstance(workspace, str) and Path(workspace).is_absolute(), "workspace must be an absolute directory")
    _strings(task.get("ownership"), "ownership")
    features = task.get("features")
    _need(isinstance(features, dict), "features must be an object")
    for key, values in _ENUMS.items():
        _need(features.get(key) in values, f"features.{key} is missing or invalid")
    if task.get("operation") == "controller":
        _need(features["stage"] == "strategy", "controller operation is only for strategy/coordination decisions")
    for key in ("deterministic", "multi_source", "plan_ready"):
        _need(type(features.get(key)) is bool, f"features.{key} must be a boolean")
    _strings(features.get("required_tools"), "features.required_tools", nonempty=False)
    _strings(features.get("available_tools"), "features.available_tools", nonempty=False)
    delegation = task.get("delegation")
    _need(isinstance(delegation, dict), "delegation must be an object")
    for key in ("authorized", "prohibited"):
        _need(type(delegation.get(key)) is bool, f"delegation.{key} must be a boolean")
    _need(isinstance(delegation.get("source"), str) and delegation["source"].strip(), "delegation.source must be a nonempty string")
    approval = task.get("approval")
    _need(isinstance(approval, dict), "approval must be an object")
    for key in ("required", "granted"):
        _need(type(approval.get(key)) is bool, f"approval.{key} must be a boolean")
    current = task.get("current")
    _need(isinstance(current, dict), "current must be an object")
    for key in ("model", "effort"):
        _need(isinstance(current.get(key), str) and current[key].strip(), f"current.{key} must be a nonempty string")
    override = task.get("override")
    if override is not None:
        _need(isinstance(override, dict), "override must be an object")
        for key in ("model",):
            _need(isinstance(override.get(key), str) and override[key].strip(), f"override.{key} must be explicitly supplied")
        if "effort" in override:
            _need(isinstance(override["effort"], str) and override["effort"].strip(), "override.effort must be nonempty when supplied")
    kind = task.get("kind")
    if kind is not None:
        _need(isinstance(kind, str) and kind.strip(), "kind must be a nonempty string when supplied")
    if "depends_on" in task:
        _strings(task["depends_on"], "depends_on", nonempty=False)
    if "input_version" in task:
        _need(isinstance(task["input_version"], str) and task["input_version"].strip(), "input_version must be a nonempty string")
    from work_evidence import validate_task
    from task_state import StateError
    try: validate_task(task)
    except StateError as exc: raise PolicyError(str(exc)) from exc
    if 'check_plan' in task:
        from check_protocol import validate_plan
        try: validate_plan(task['check_plan'])
        except StateError as exc: raise PolicyError(str(exc)) from exc
    return features


def _blocked(code, reason, *, role=None, limitations=None):
    result = {"action": "blocked", "reason_codes": [code], "role": role, "model": None,
              "effort": None, "independence_required": False,
              "limitations": limitations or [], "block_reason": reason}
    return result


def validate_pins(pins):
    _need(isinstance(pins, dict) and set(pins) <= {"model", "effort", "no_switch", "source"},
          "controller pins must contain only explicit model/effort/no_switch and source")
    if pins:
        _need(isinstance(pins.get("source"), str) and bool(pins["source"].strip()) and set(pins) != {"source"},
              "controller pins require a source and at least one restriction")
    for key in ("model", "effort"):
        if key in pins:
            _need(isinstance(pins[key], str) and bool(pins[key].strip()), f"pins.{key} must be nonempty")
    _need(type(pins.get("no_switch", False)) is bool, "no_switch must be boolean")
    return pins


def merged_pins(task, supplied=None):
    """Persisted restrictions cannot be silently replaced by a call argument."""
    result = dict(validate_pins(task.get("controller_pins", {})))
    for key, value in validate_pins({} if supplied is None else supplied).items():
        if key != "source":
            _need(key not in result or result[key] == value, "conflicting explicit controller pins")
        result[key] = value
    return result


def outline_collaboration(task):
    return task.get("mode") == "outline" and not task["delegation"]["prohibited"]


def controller_restriction(task, supplied=None):
    pins = merged_pins(task, supplied)
    locked = {key: pins[key] for key in ("model", "effort") if key in pins}
    whole = task.get("override")
    if whole is not None:
        for key in ("model", "effort"):
            if key in whole:
                _need(key not in locked or locked[key] == whole[key], "conflicting whole-task and controller pins")
                locked[key] = whole[key]
    if outline_collaboration(task) and not pins.get("no_switch"):
        locked.setdefault("model", "gpt-6-astra")
    return locked


def _catalog_model(catalog, model, effort):
    _need(isinstance(catalog, dict) and catalog.get("source") == "runtime-tool", "current runtime catalog is required")
    models = catalog.get("models")
    _need(isinstance(models, list), "runtime model list is missing")
    matches = [item for item in models if isinstance(item, dict) and item.get("id") == model]
    _need(len(matches) == 1, "override model must match exactly one catalog entry")
    item = matches[0]
    _need(item.get("visibility", "list") == "list" and item.get("available", True) is True,
          "override model is hidden or unavailable")
    efforts = item.get("reasoning_efforts")
    _need(isinstance(efforts, list) and effort in efforts, "override effort is not advertised for the exact model")


def decide(task: dict, catalog: dict) -> dict:
    """Return a direct/delegate/blocked decision without invoking a model."""
    features = _validate_task(task)
    missing = sorted(set(features["required_tools"]) - set(features["available_tools"]))
    if missing:
        return _blocked("blocked_environment", "Required tools are unavailable: " + ", ".join(missing))
    if task["approval"]["required"] and not task["approval"]["granted"]:
        return _blocked("blocked_approval", "Required approval has not been granted")

    override = task.get("override")
    if override is not None:
        # Legacy records remain readable, but their ambiguous authority must not
        # silently become a whole-task model restriction during recovery.
        if override.get("scope") != "all_tasks" or not isinstance(override.get("source"), str) or not override["source"].strip():
            return _blocked("blocked_override_scope", "Model override requires scope=all_tasks and an explicit user instruction source; main-thread selection belongs only in current. Update the task before dispatch.")
        if override["model"] != task["current"]["model"] or ("effort" in override and override["effort"] != task["current"]["effort"]):
            return _blocked("blocked_user_model", "Whole-task restriction differs from the actual main-thread model/effort; select the requested main-thread settings first. Routing cannot hot-switch them.")
    stage = features["stage"]
    try:
        locked = controller_restriction(task)
    except PolicyError as exc:
        return _blocked("blocked_controller_constraint", str(exc))
    if any(task["current"][key] != value for key, value in locked.items()):
        return _blocked("blocked_controller_settings", "Actual main-thread settings do not satisfy controller requirements; routing cannot hot-switch them")
    outline = outline_collaboration(task)
    if outline and task["operation"] == "business" and (stage == "strategy" or features["ambiguity"] == "conflict" or (stage == "execution" and features["risk"] == "high")):
        return _blocked("blocked_controller_decision", "Return strategy/conflict/high-risk execution to the controller; delegated high-risk execution is not supported in this increment")
    if (outline and task["operation"] == "controller") or stage == "strategy" or features["ambiguity"] == "conflict" or (stage == "execution" and features["risk"] == "high"):
        # Keep the historical role identifier for ledger compatibility. The
        # actual controller is the user's current main-thread model.
        role, action, reason = "astra_controller", "direct", "controller_required"
    elif stage == "review":
        role, action, reason = "sol_architect", "delegate", "independent_review"
    elif stage == "planning" or features["ambiguity"] == "unknown":
        role, action, reason = "sol_planner", "delegate", "planning_or_unknowns"
    elif stage == "research" and features["multi_source"]:
        role, action, reason = "terra_general", "delegate", "multi_source_research"
    elif not outline and stage == "execution" and features["deterministic"] and features["complexity"] == "simple" and features["risk"] == "low":
        role, action, reason = "direct_tool", "direct", "deterministic_simple_execution"
    elif stage == "execution" and not features["plan_ready"]:
        role, action, reason = "sol_planner", "delegate", "plan_required"
    elif stage == "execution" and features["complexity"] == "complex":
        role, action, reason = "luna_deep", "delegate", "complex_execution"
    elif stage == "execution":
        role = "luna_general" if task.get("kind") == "general" else "luna_worker"
        action, reason = "delegate", "planned_execution"
    else:
        role, action, reason = "sol_planner", "delegate", "conservative_default"

    limitations = []
    if outline and action == "delegate" and not task["delegation"]["authorized"] and not task["delegation"]["prohibited"]:
        return _blocked("blocked_delegation_authority", "Outline business delegation requires explicit authority; missing authority is not a user prohibition")
    if action == "delegate" and (task["delegation"]["prohibited"] or not task["delegation"]["authorized"]):
        return {"action": "direct", "reason_codes": [reason, "delegation_unavailable"], "role": role,
                "model": task["current"]["model"], "effort": task["current"]["effort"],
                "independence_required": role == "sol_architect",
                "limitations": ["Independent delegation unavailable; main-thread execution reduces separation"]}

    if action == "direct":
        # The subagent catalog does not establish availability of the already
        # running main thread. Never validate it against that different entrypoint.
        return {"action": "direct", "reason_codes": [reason], "role": role,
                "model": task["current"]["model"], "effort": task["current"]["effort"],
                "independence_required": False, "limitations": limitations}

    role_effort = "max" if role == "sol_planner" and features["complexity"] == "complex" else _PROFILE_EFFORTS[role]
    if override is not None:
        model, effort = override["model"], override.get("effort", role_effort)
        try:
            _catalog_model(catalog, model, effort)
        except PolicyError as exc:
            return _blocked("blocked_user_model", str(exc), role=role)
    else:
        try:
            planner_efforts = {"sol_planner": "max" if features["complexity"] == "complex" else "high"} if role == "sol_planner" else None
            route = resolve(catalog, [role], efforts=planner_efforts)["routes"][role]
        except (ResolutionError, TypeError, KeyError) as exc:
            return _blocked("blocked_model", str(exc), role=role)
        model, effort = route["model"], route["reasoning_effort"]

    return {"action": "delegate", "reason_codes": [reason], "role": role, "model": model,
            "effort": effort, "independence_required": role == "sol_architect", "limitations": limitations}


def direct_completion_decision(task, prepared, catalog, policy_contract):
    """Validate a prepared receipt, including the precisely defined 3.0 format.

    This does not authorize new work. Callers must first validate the stored
    task version, input digest, dependency generations and direct receipt.
    """
    _need(policy_contract in (None, "3.1"), "unknown prepared policy contract")
    current = decide(task, catalog)
    if current.get("action") == "direct" and current == prepared:
        return current
    candidate = task
    normalized = dict(prepared)
    if policy_contract is None:
        override = task.get("override")
        if override is not None:
            _need({"model", "effort"} <= set(override) <= {"model", "effort", "source"} and
                  all(override[key] == task["current"][key] for key in ("model", "effort")),
                  "legacy direct override is not equivalent to the recorded main-thread settings")
            candidate = {key: value for key, value in task.items() if key != "override"}
        if normalized.get("effort") == "inherit":
            _need(normalized.get("role") == "astra_controller" and normalized.get("model") == task["current"]["model"],
                  "legacy inherited effort is not a matching controller decision")
            normalized["effort"] = task["current"]["effort"]
    current = decide(candidate, catalog)
    _need(current.get("action") == "direct" and current == normalized,
          "prepared decision is no longer the current direct decision")
    return current


def _safe_paths(task):
    workspace = Path(task["workspace"]).resolve(strict=False)
    _need(workspace.is_absolute(), "workspace must resolve to an absolute path")
    for raw in task["ownership"]:
        path = Path(raw)
        _need(path.is_absolute(), "every ownership path must be absolute")
        resolved = path.resolve(strict=False)
        try:
            resolved.relative_to(workspace)
        except ValueError as exc:
            raise PolicyError("ownership path escapes workspace: " + raw) from exc
    return workspace


def _profile(profile_path, role):
    try:
        with profile_path.open("rb") as stream:
            profile = tomllib.load(stream)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise PolicyError(f"Could not read role profile {profile_path}: {exc}") from exc
    unknown = set(profile) - _PROFILE_FIELDS
    _need(not unknown, "Unsupported profile restriction fields: " + ", ".join(sorted(unknown)))
    _need(profile.get("name") == role, "profile name does not match selected role")
    _need(isinstance(profile.get("developer_instructions"), str) and profile["developer_instructions"].strip(),
          "profile must contain complete developer_instructions")
    for key in ("description", "model", "model_reasoning_effort"):
        _need(isinstance(profile.get(key), str) and profile[key].strip(), f"profile {key} is required")
    _need(profile["model_reasoning_effort"] == _PROFILE_EFFORTS[role],
          f"profile effort conflicts with role contract: expected {_PROFILE_EFFORTS[role]}")
    if role in {"sol_architect", "sol_planner"}:
        _need(profile.get("sandbox_mode") == "read-only", f"{role} profile must preserve read-only restriction")
    else:
        _need("sandbox_mode" not in profile, "unexpected sandbox restriction field; cannot drop or reinterpret profile permissions")
    if role == "sol_architect":
        instructions = profile["developer_instructions"].lower()
        _need("只读" in instructions and "不修改文件" in instructions, "sol_architect instructions must explicitly remain read-only")
    if role == "sol_planner":
        instructions = profile["developer_instructions"].lower()
        _need("修改文件" in instructions and any(marker in instructions for marker in ("不修改", "不得", "禁止")),
              "sol_planner instructions must explicitly prohibit file edits")
    _need(profile.get("sandbox_mode") in (None, "read-only"), "Unsupported sandbox restriction; cannot drop profile permissions")
    return profile


def build_packet(task: dict, decision: dict, profiles_dir: str, attempt_id: str) -> dict:
    """Build an explicit spawn package for one already-approved delegate decision."""
    _validate_task(task)
    _need(isinstance(decision, dict) and decision.get("action") == "delegate", "build_packet only accepts delegate decisions")
    role, model, effort = decision.get("role"), decision.get("model"), decision.get("effort")
    _need(isinstance(role, str) and role and isinstance(model, str) and model and isinstance(effort, str) and effort,
          "delegate decision must include role, model, and effort")
    _need(role in _DELEGATE_ROLES, "decision role is not an approved delegate profile")
    _need(isinstance(attempt_id, str) and attempt_id.strip(), "attempt_id must be a nonempty string")
    workspace = _safe_paths(task)
    profiles = Path(profiles_dir).resolve(strict=False)
    profile = _profile(profiles / (role.replace("_", "-") + ".toml"), role)
    if role == "sol_architect":
        _need(decision.get("independence_required") is True, "sol_architect review must require an independent context")

    body = {
        "task_id": task["task_id"], "attempt_id": attempt_id, "goal": task["goal"], "input": task["input"],
        "workspace": str(workspace), "ownership": task["ownership"], "constraints": task["constraints"],
        "forbidden": task["forbidden"], "success_criteria": task["success"],
        "verification": task["verification"], "escalation_conditions": task["escalation"],
        "features": task["features"], "delegation": task["delegation"], "approval": task["approval"],
        "controller_settings": task["current"],
        "whole_task_model_restriction": task.get("override"),
        "selected_role": role, "requested_model": model, "requested_effort": effort,
        "role_description": profile["description"], "role_developer_instructions": profile["developer_instructions"],
        "read_only": profile.get("sandbox_mode") == "read-only",
        "execution_constraints": ["只改 ownership 范围内的文件；不得恢复或覆盖同库他人编辑。",
                                  "controller_settings 仅描述父主线程；不将其模型或强度当作子任务覆盖。角色中 Astra 的主线程职责由该实际主线程承担。",
                                  "whole_task_model_restriction 非空时保留其明确的全任务范围与出处；后续委派不得丢失该限制，也不得从父主线程设置自行生成限制。",
                                  "只读要求是任务约束，不代表 OS/宿主沙箱已提供强制隔离。",
                                  "自报实际模型与推理档位仅供记录，不是实际调用型号的独立证据。",
                                  "遇到歧义、范围扩大、公共契约/架构/安全边界变化或非预期验证失败，停止并升级；禁止 silent fallback。"],
    }
    for key in ("check_plan", "depends_on", "input_version", "mode", "operation", "controller_pins", "outline_ref", "outline_revision",
                "module_id", "requirement_ids", "acceptance_ids", "resources", "read_paths", "snapshot_files", "artifacts"):
        if key in task:
            body[key] = task[key]
    if task.get('mode')=='outline':
        body['execution_constraints'].extend([
            '只读取read_paths、snapshot_files或ownership内的任务文件；大纲引用是中枢追溯元数据，不是读取整份大纲或其他模块的授权。不得读取其他试点组产物。',
            '只执行当前module_id的工作，不预写后续模块；发现其他模块需求交回中枢。不得另派代理绕过宿主名额。',
            '交付提供artifacts路径/hash、checks命令/实际及预期退出码/evidence_ref、remaining未验项、risks和scope_evidence；摘要不替代实际证据。'])
    body['execution_constraints'].append(
        'check_plan随输入冻结；按id报告真实结果，不得事后修改预期。普通探查的已声明无匹配/尚未创建不算业务失败；未声明或无法区分的异常先交中枢。业务验收失败及检查器自身错误均先报告中枢并停止扩大操作，不能自行修后只报绿灯。角色只读与原审批保持。'
        if 'check_plan' in task else
        '本回执未分类检查(unclassified_legacy)，不具备新检查分类门禁；已知验收命令应由中枢在新prepare前填写check_plan。普通探查不冒充验收，非预期验收失败/检查器异常先交中枢，禁止只报修复后成功。')
    message = "委派任务包（JSON）\n" + json.dumps(body, ensure_ascii=False, indent=2)
    safe_id = re.sub(r"[^a-z0-9_]+", "_", task["task_id"].lower()).strip("_") or "task"
    digest = hashlib.sha256((task["task_id"] + "\0" + attempt_id).encode("utf-8")).hexdigest()[:10]
    task_name = (safe_id[:40] + "_" + digest).strip("_")
    return {"spawn_args": {"task_name": task_name, "agent_type": "default", "model": model,
                           "reasoning_effort": effort, "fork_turns": "none", "message": message},
            "read_only": body["read_only"], "ownership": task["ownership"],
            "task_id": task["task_id"], "attempt_id": attempt_id, "body": body}
