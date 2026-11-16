import json
import tempfile
import unittest
from pathlib import Path

from router_policy import PolicyError, build_packet, decide


def model(mid, efforts=("medium", "high", "max"), **kwargs):
    return {"id": mid, "reasoning_efforts": list(efforts), **kwargs}


CATALOG = {"source": "runtime-tool", "models": [
    model("gpt-6.2-luna"), model("gpt-6.2-sol"), model("gpt-6.2-terra"),
    model("gpt-6-astra", ("low", "medium", "high")),
]}


def task(**overrides):
    value = {
        "task_id": "feature-42", "goal": "完成目标", "input": "有上下文的输入", "workspace": str(Path.cwd()),
        "ownership": [str(Path.cwd() / "router_policy.py")], "constraints": ["保留兼容"],
        "forbidden": ["不得改范围"], "success": ["可观察结果"], "verification": ["运行单测"],
        "escalation": ["影响扩大时停止"],
        "features": {"stage": "execution", "ambiguity": "clear", "risk": "normal",
                     "complexity": "normal", "deterministic": False, "multi_source": False,
                     "plan_ready": True, "required_tools": [], "available_tools": []},
        "delegation": {"authorized": True, "prohibited": False, "source": "user request"},
        "approval": {"required": False, "granted": False},
        "current": {"model": "gpt-6-astra", "effort": "high"},
    }
    for key, value_override in overrides.items():
        if key == "features":
            value["features"].update(value_override)
        elif key in ("delegation", "approval", "current"):
            value[key].update(value_override)
        else:
            value[key] = value_override
    return value


def whole_task(model, **kwargs):
    return {"scope": "all_tasks", "source": "User explicitly requests the whole task use this model", "model": model, **kwargs}


class DecisionTests(unittest.TestCase):
    def test_simple_low_deterministic_direct(self):
        result = decide(task(features={"risk": "low", "complexity": "simple", "deterministic": True}), {})
        self.assertEqual((result["action"], result["role"], result["model"]), ("direct", "direct_tool", "gpt-6-astra"))

    def test_plan_not_ready_and_explicit_planning(self):
        result = decide(task(features={"plan_ready": False}), CATALOG)
        self.assertEqual((result["role"], result["effort"]), ("sol_planner", "high"))
        complex_plan = decide(task(features={"stage": "planning", "complexity": "complex"}), CATALOG)
        self.assertEqual(complex_plan["effort"], "max")

    def test_review_independent_and_research_multi_source(self):
        review = decide(task(features={"stage": "review"}), CATALOG)
        self.assertEqual(review["role"], "sol_architect")
        self.assertTrue(review["independence_required"])
        research = decide(task(features={"stage": "research", "multi_source": True}), CATALOG)
        self.assertEqual(research["role"], "terra_general")

    def test_complex_execution(self):
        result = decide(task(features={"complexity": "complex"}), CATALOG)
        self.assertEqual((result["role"], result["effort"]), ("luna_deep", "high"))

    def test_high_risk_execution_stays_with_controller(self):
        result = decide(task(features={"risk": "high"}), CATALOG)
        self.assertEqual((result["action"], result["role"], result["model"]), ("direct", "astra_controller", "gpt-6-astra"))
        manual = decide(task(features={"risk": "high"}, current={"model": "gpt-6-luna", "effort": "medium"}), {})
        self.assertEqual((manual["action"], manual["model"], manual["effort"]), ("direct", "gpt-6-luna", "medium"))

    def test_manual_main_model_and_effort_do_not_override_child_roles(self):
        cases = [({}, "luna_worker", "gpt-6.2-luna", "medium"),
                 ({"complexity": "complex"}, "luna_deep", "gpt-6.2-luna", "high"),
                 ({"stage": "planning"}, "sol_planner", "gpt-6.2-sol", "high"),
                 ({"stage": "planning", "complexity": "complex"}, "sol_planner", "gpt-6.2-sol", "max"),
                 ({"stage": "review"}, "sol_architect", "gpt-6.2-sol", "max"),
                 ({"stage": "research", "multi_source": True}, "terra_general", "gpt-6.2-terra", "high")]
        for main in ("gpt-6-astra", "gpt-6.1-sol", "gpt-6-luna", "main-only-model"):
            for effort in ("low", "medium", "high", "max", "ultra"):
                for features, role, model_id, role_effort in cases:
                    with self.subTest(main=main, effort=effort, role=role, features=features):
                        result = decide(task(current={"model": main, "effort": effort}, features=features), CATALOG)
                        self.assertEqual((result["action"], result["role"], result["model"], result["effort"]),
                                         ("delegate", role, model_id, role_effort))
                        self.assertEqual(result["independence_required"], role == "sol_architect")

    def test_direct_override_cannot_hot_switch_main_model(self):
        result = decide(task(features={"risk": "high"},
                             override=whole_task("gpt-6-astra", effort="high"),
                             current={"model": "gpt-6-luna", "effort": "medium"}), CATALOG)
        self.assertEqual(result["reason_codes"], ["blocked_user_model"])

    def test_tools_and_approval_block(self):
        tools = decide(task(features={"required_tools": ["browser"]}), CATALOG)
        self.assertEqual(tools["reason_codes"], ["blocked_environment"])
        approval = decide(task(approval={"required": True, "granted": False}), CATALOG)
        self.assertEqual(approval["reason_codes"], ["blocked_approval"])

    def test_missing_model_or_effort_blocks_without_downgrade(self):
        for value, catalog in ((task(features={"complexity": "complex"}), {"source": "runtime-tool", "models": [model("gpt-6.2-luna", ("medium",))]}),
                               (task(features={"stage": "planning", "complexity": "complex"}), {"source": "runtime-tool", "models": [model("gpt-6.2-sol", ("high",))]})):
            with self.subTest(catalog=catalog):
                result = decide(value, catalog)
                self.assertEqual(result["reason_codes"], ["blocked_model"])

    def test_override_exact_catalog_and_does_not_erase_risk_route(self):
        risky = task(features={"risk": "high"}, override=whole_task("gpt-6-astra", effort="high"))
        result = decide(risky, {})
        self.assertEqual((result["role"], result["model"]), ("astra_controller", "gpt-6-astra"))
        wrong_effort = decide(task(current={"model": "gpt-6.2-luna", "effort": "ultra"},
                                  override=whole_task("gpt-6.2-luna", effort="ultra")), CATALOG)
        self.assertEqual(wrong_effort["reason_codes"], ["blocked_user_model"])

    def test_prohibited_delegation_direct_with_limit_or_user_model_block(self):
        value = task(delegation={"authorized": False, "prohibited": True})
        result = decide(value, CATALOG)
        self.assertEqual(result["action"], "direct")
        self.assertTrue(result["limitations"])
        value["override"] = whole_task("gpt-6.2-sol", effort="high")
        self.assertEqual(decide(value, CATALOG)["reason_codes"], ["blocked_user_model"])

    def test_whole_task_model_preserves_role_effort_unless_explicit(self):
        for features, expected in [({}, "medium"), ({"stage": "review"}, "max"),
                                   ({"stage": "planning", "complexity": "complex"}, "max")]:
            item = task(features=features, current={"model": "gpt-6.2-sol", "effort": "high"},
                        override=whole_task("gpt-6.2-sol"))
            result = decide(item, CATALOG)
            self.assertEqual((result["model"], result["effort"]), ("gpt-6.2-sol", expected))
            item["override"]["effort"] = "high"
            self.assertEqual(decide(item, CATALOG)["effort"], "high")

    def test_legacy_and_missing_scope_source_fail_closed(self):
        for override in [{"model": "gpt-6.2-sol", "effort": "high"},
                         whole_task("gpt-6.2-sol", scope="main_thread"),
                         whole_task("gpt-6.2-sol", source=" ")]:
            result = decide(task(override=override), CATALOG)
            self.assertEqual(result["reason_codes"], ["blocked_override_scope"])

    def test_whole_task_unavailable_model_or_effort_never_falls_back(self):
        item = task(current={"model": "gpt-6.2-sol"}, override=whole_task("gpt-6.2-sol"))
        for entries in [[], [model("gpt-6.2-sol", ("high",))],
                        [model("gpt-6.2-sol", available=False)],
                        [model("gpt-6.2-sol", visibility="hidden")]]:
            self.assertEqual(decide(item, {"source": "runtime-tool", "models": entries})["reason_codes"], ["blocked_user_model"])

    def test_main_direct_settings_need_no_subagent_catalog(self):
        current = {"model": "main-only-model", "effort": "ultra"}
        for features in [{"stage": "strategy"}, {"ambiguity": "conflict"},
                         {"risk": "low", "complexity": "simple", "deterministic": True}]:
            result = decide(task(current=current, features=features), {})
            self.assertEqual((result["action"], result["model"], result["effort"]), ("direct", current["model"], current["effort"]))
        result = decide(task(current=current, features={"stage": "review"},
                             delegation={"authorized": False, "prohibited": True}), {})
        self.assertEqual(result["action"], "direct")
        self.assertTrue(result["independence_required"])

    def test_schema_requires_real_booleans_and_complete_task(self):
        value = task(features={"plan_ready": 1})
        with self.assertRaises(PolicyError):
            decide(value, CATALOG)
        value = task()
        del value["approval"]["granted"]
        with self.assertRaises(PolicyError):
            decide(value, CATALOG)


class PacketTests(unittest.TestCase):
    def test_packet_preserves_controller_boundary_and_explicit_restriction(self):
        for override in (None, whole_task("gpt-6.2-sol")):
            item = task(features={"stage": "review"}, current={"model": "gpt-6.2-sol", "effort": "high"}, override=override)
            packet = build_packet(item, decide(item, CATALOG), Path(__file__).parent / "agents", "scope-check")
            body = json.loads(packet["spawn_args"]["message"].split("\n", 1)[1])
            self.assertEqual(body["controller_settings"], item["current"])
            self.assertEqual(body["whole_task_model_restriction"], override)
            self.assertEqual(body["requested_effort"], "max")
            self.assertTrue(body["read_only"])

    def setUp(self):
        self.root = Path(__file__).parent
        self.value = task(features={"stage": "review"})
        self.decision = decide(self.value, CATALOG)

    def test_packet_contains_full_profile_and_explicit_spawn_contract(self):
        self.value["depends_on"] = ["previous"]
        self.value["input_version"] = "rev-3"
        packet = build_packet(self.value, self.decision, str(self.root / "agents"), "try-1")
        args = packet["spawn_args"]
        self.assertEqual((args["agent_type"], args["model"], args["reasoning_effort"], args["fork_turns"]),
                         ("default", self.decision["model"], self.decision["effort"], "none"))
        self.assertRegex(args["task_name"], r"^[a-z0-9_]+$")
        self.assertTrue(packet["read_only"])
        self.assertIn("developer_instructions", args["message"])
        body = json.loads(args["message"].split("\n", 1)[1])
        self.assertEqual((body["depends_on"], body["input_version"]), (["previous"], "rev-3"))
        self.assertTrue(any("不代表 OS/宿主沙箱已提供强制隔离" in item for item in body["execution_constraints"]))

    def test_rejects_workspace_escape_and_unknown_profile_restrictions(self):
        self.value["ownership"] = [str(Path(self.value["workspace"]).parent / "outside.py")]
        with self.assertRaisesRegex(PolicyError, "escapes workspace"):
            build_packet(self.value, self.decision, str(self.root / "agents"), "try-1")
        with tempfile.TemporaryDirectory() as temp:
            profile = Path(temp) / "sol-architect.toml"
            profile.write_text('name="sol_architect"\nmodel="m"\nmodel_reasoning_effort="max"\ndescription="d"\nsandbox_mode="read-only"\ndeveloper_instructions="只读，不修改文件"\nextra_permission="yes"\n', encoding="utf-8")
            with self.assertRaisesRegex(PolicyError, "Unsupported profile"):
                build_packet(task(), self.decision, temp, "try-2")

    def test_profile_effort_and_planner_read_only_contract_are_enforced(self):
        decision = decide(task(features={"stage": "planning"}), CATALOG)
        with tempfile.TemporaryDirectory() as temp:
            profile = Path(temp) / "sol-planner.toml"
            profile.write_text('name="sol_planner"\nmodel="gpt-6.2-sol"\nmodel_reasoning_effort="max"\ndescription="d"\nsandbox_mode="read-only"\ndeveloper_instructions="只读，但不得修改文件"\n', encoding="utf-8")
            with self.assertRaisesRegex(PolicyError, "effort conflicts"):
                build_packet(task(features={"stage": "planning"}), decision, temp, "try-3")
            profile.write_text('name="sol_planner"\nmodel="gpt-6.2-sol"\nmodel_reasoning_effort="high"\ndescription="d"\nsandbox_mode="read-only"\ndeveloper_instructions="做规划"\n', encoding="utf-8")
            with self.assertRaisesRegex(PolicyError, "prohibit file edits"):
                build_packet(task(features={"stage": "planning"}), decision, temp, "try-4")

    def test_non_sol_profile_cannot_add_sandbox_restrictions(self):
        value = task()
        decision = decide(value, CATALOG)
        with tempfile.TemporaryDirectory() as temp:
            profile = Path(temp) / "luna-worker.toml"
            profile.write_text('name="luna_worker"\nmodel="gpt-6.2-luna"\nmodel_reasoning_effort="medium"\ndescription="d"\nsandbox_mode="read-only"\ndeveloper_instructions="instructions"\n', encoding="utf-8")
            with self.assertRaisesRegex(PolicyError, "unexpected sandbox"):
                build_packet(value, decision, temp, "try-5")

    def test_rejects_non_delegate_decision(self):
        with self.assertRaises(PolicyError):
            build_packet(self.value, {"action": "direct"}, str(self.root / "agents"), "try-1")


if __name__ == "__main__":
    unittest.main()
