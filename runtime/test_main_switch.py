"""Exercise the switch through the same JSON contract as the desktop helper."""
import json
import tempfile
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from main_switch import ENTRYPOINT, recommend
from router_policy import decide
from routing import handle
from task_state import StateError
from test_router_policy import CATALOG, model, task, whole_task


def now():
    return datetime.now(timezone.utc).isoformat()


class MainPolicyTests(unittest.TestCase):
    def test_main_stage_and_cost_without_changing_child_role(self):
        catalog = {"source": "runtime-tool", "models": CATALOG["models"] + [
            model("gpt-6.10-sol"), model("gpt-6.9-sol")
        ]}
        samples = [
            ({"stage": "strategy"}, "gpt-6-astra", "high"),
            ({"stage": "planning", "complexity": "complex"}, "gpt-6.10-sol", "max"),
            ({"stage": "research", "multi_source": True}, "gpt-6.2-terra", "high"),
            ({"deterministic": True, "complexity": "complex"}, "gpt-6.2-luna", "high"),
            ({"deterministic": True}, "gpt-6.2-luna", "medium"),
        ]
        for features, mid, effort in samples:
            with self.subTest(features=features):
                item = task(features=features, current={"model": "manual-preference", "effort": "ultra"})
                self.assertEqual(recommend(item, catalog, {}, True)["target"], {"model": mid, "effort": effort})
                self.assertEqual(recommend(item, catalog, {}, False)["target"], item["current"])
        item = task(current={"model": "gpt-6.10-sol", "effort": "max"})
        self.assertEqual(decide(item, CATALOG)["model"], "gpt-6.2-luna")

    def test_pin_fields_are_independent(self):
        item = task(features={"deterministic": True})
        result = recommend(item, CATALOG, {"model": "gpt-6.2-sol", "source": "user fixed model"}, True)
        self.assertEqual(result["target"], {"model": "gpt-6.2-sol", "effort": "medium"})
        result = recommend(item, CATALOG, {"effort": "max", "source": "user fixed effort"}, False)
        self.assertEqual(result["target"], {"model": "gpt-6.2-luna", "effort": "max"})
        self.assertEqual(recommend(item, CATALOG, {"no_switch": True, "source": "user"}, True)["action"], "keep")
        item["override"] = whole_task("gpt-6.2-sol")
        self.assertEqual(recommend(item, CATALOG, {}, True)["target"]["model"], "gpt-6.2-sol")
        self.assertEqual(recommend(item, CATALOG, {"model": "gpt-6-astra", "source": "user"}, True)["action"], "blocked")

    def test_no_fallback_to_older_family_and_no_delegation_needed(self):
        item = task(features={"stage": "planning", "complexity": "complex"})
        catalog = {"source": "runtime-tool", "models": CATALOG["models"] + [model("gpt-6.10-sol", ("high",))]}
        outcome = recommend(item, catalog, {}, True)
        self.assertEqual((outcome["action"], outcome["reason"], outcome["target"]),
                         ("keep", "target_not_advertised", item["current"]))
        item["delegation"]["prohibited"] = True
        self.assertEqual(recommend(item, CATALOG, {}, True)["target"], {"model": "gpt-6.2-sol", "effort": "max"})
        item["approval"] = {"required": True, "granted": False}
        self.assertEqual(recommend(item, CATALOG, {}, True)["reason"], "approval_required")


class MainSwitchTests(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.root = Path(scratch.name)
        fake_home = patch("main_switch.Path.home", return_value=self.root)
        fake_home.start()
        self.addCleanup(fake_home.stop)
        self.thread, self.old = str(uuid.uuid4()), str(uuid.uuid4())
        caller = patch.dict("os.environ", {"CODEX_THREAD_ID": self.thread, "CODEX_HOME": str(self.root / '.codex')})
        caller.start()
        self.addCleanup(caller.stop)
        self.rollout = self.root / ".codex" / "sessions" / f"rollout-fixture-{self.thread}.jsonl"
        self.rollout.parent.mkdir(parents=True)
        self.base = {"state_dir": str(self.root / "ledger"), "owner": self.thread + "-root", "epoch": 1}
        self.current = {"model": "gpt-6.2-sol", "effort": "max"}
        self.checkpoint = self.root / "task_plan.md"
        self.checkpoint.write_text("verified phase handoff", encoding="utf-8")
        self.write("session_meta", id=self.thread)
        self.start(self.old, self.current)
        self.human(self.old, "允许在本聊天为当前任务自动续接1次", "grant")
        self.call("init")
        self.item = task(features={"stage": "strategy"}, current=self.current, workspace=str(self.root),
                         ownership=[str(self.checkpoint)])
        self.call("add", task=self.item)
        self.rid = None

    def write(self, entry_type, **payload):
        # Windows clock granularity can give distinct fixture events the same
        # timestamp. Model their intended strict order deterministically.
        at = datetime.now(timezone.utc)
        previous = getattr(self, "_last_event_at", None)
        if previous is not None:
            at = max(at, previous + timedelta(microseconds=1))
        at = getattr(self, "_event_time", at)
        self._last_event_at = at
        with self.rollout.open("a", encoding="utf-8") as out:
            out.write(json.dumps({"timestamp": at.isoformat(), "type": entry_type, "payload": payload}, ensure_ascii=False) + "\n")

    def event(self, name, turn, **extra):
        self.write("event_msg", type=name, turn_id=turn, **extra)

    def start(self, turn, settings):
        self.event("task_started", turn)
        self.write("turn_context", turn_id=turn, **settings)

    def human(self, turn, message, message_id):
        self.event("item_completed", turn, item={"type": "UserMessage", "id": message_id, "client_id": "ui-client",
                                                "content": [{"type": "text", "text": message}]})

    def call(self, op, **kwargs):
        return handle(dict(self.base, op=op, **kwargs))

    def prepare(self, **kwargs):
        request = dict(task_id=self.item["task_id"], thread_id=self.thread, host_id="local",
                       before_turn_id=self.old, checkpoint=str(self.checkpoint), rollout_path=str(self.rollout),
                       phase="architecture", substantial=True, catalog=CATALOG,
                       catalog_provenance={"entrypoint": ENTRYPOINT, "captured_at": now(), "source": "runtime-tool"},
                       authorization={"message_id": "grant", "answer": "允许在本聊天为当前任务自动续接1次",
                                      "purpose": "task_continuation", "max_sends": 1, "reviewed_through": "grant"})
        request.update(kwargs)
        return self.call("main_switch_prepare", **request)["request"]

    def prepare_switch(self, **kwargs):
        record = self.prepare(**kwargs)
        self.rid = record["request_id"]
        return record

    def step(self, suffix, **kwargs):
        return self.call("main_switch_" + suffix, request_id=self.rid, **kwargs)

    def claim(self):
        result = self.step("claim")
        self.permit = result["send_permit"]
        return result["native_args"]

    def accepted(self):
        return self.step("receipt", outcome="accepted", receipt_thread_id=self.thread, send_permit=self.permit)

    def native_input(self, turn, prompt, source=None):
        source = source or self.thread
        self.event("item_completed", turn, item={"type": "FunctionCallOutput", "id": "native-event",
                                                "namespace": "codex_app", "name": "send_message_to_thread",
                                                "output": f"<codex_delegation>\n  <source_thread_id>{source}</source_thread_id>\n"
                                                          f"  <input>{prompt}</input>\n</codex_delegation>"})

    def apply_settings(self, record, prompt=None):
        self.write("event_msg", type="thread_settings_applied", thread_id=self.thread,
                   thread_settings={"model": record["target"]["model"], "reasoning_effort": record["target"]["effort"]})
        self.native_input(self.old, prompt if prompt is not None else record["prompt"])

    def next_human_turn(self, settings):
        self.event("task_complete", self.old)
        new = str(uuid.uuid4())
        self.start(new, settings)
        self.human(new, "Continue", "next-message")
        return new

    def test_stage_then_claim_once_and_confirm_user_started_next_turn(self):
        record = self.prepare_switch()
        args = self.claim()
        self.assertEqual((args["threadId"], args["model"], args["thinking"]), (self.thread, "gpt-6-astra", "high"))
        with self.assertRaisesRegex(StateError, "already claimed"):
            self.step("claim")
        with self.assertRaisesRegex(StateError, "only the claimant"):
            self.step("cancel", outcome="duplicate helper", send_permit=str(uuid.uuid4()))
        self.accepted()
        self.assertEqual(self.step("confirm")["status"], "accepted")
        self.apply_settings(record)
        self.assertEqual(self.step("confirm")["status"], "staged")
        new = self.next_human_turn(record["target"])
        result = self.step("confirm")
        self.assertEqual(result["status"], "confirmed")
        self.assertEqual(result["host_evidence"]["turn_id"], new)
        self.assertEqual(self.step("confirm"), result)

    def test_self_native_input_after_old_turn_ends_does_not_pass_verification(self):
        record = self.prepare_switch()
        self.claim()
        new = self.next_human_turn(record["target"])
        self.native_input(new, record["prompt"])
        self.accepted()
        self.assertEqual(self.step("confirm")["status"], "mismatch")

    def test_compaction_before_first_human_input_is_not_task_execution(self):
        record = self.prepare_switch()
        self.claim()
        self.apply_settings(record)
        self.accepted()
        self.event("task_complete", self.old)
        new = str(uuid.uuid4())
        self.start(new, record["target"])
        self.event("item_completed", new, item={"type": "ContextCompaction", "id": "compact"})
        self.assertEqual(self.step("confirm")["status"], "staged")
        self.human(new, "Continue", "next-message")
        result = self.step("confirm")
        self.assertEqual(result["status"], "confirmed")
        self.assertTrue(result["host_evidence"]["started_by_human"])

    def test_equal_timestamps_preserve_user_settings_override_order(self):
        record = self.prepare_switch()
        self.claim()
        self._event_time = datetime.now(timezone.utc)
        self.apply_settings(record)
        different = dict(record["target"], effort="max")
        self.write("event_msg", type="thread_settings_applied", thread_id=self.thread,
                   thread_settings={"model": different["model"], "reasoning_effort": different["effort"]})
        self.next_human_turn(different)
        self.accepted()
        self.assertEqual(self.step("confirm")["status"], "superseded")

    def test_equal_timestamps_reject_marker_after_completed_old_turn(self):
        record = self.prepare_switch()
        self.claim()
        self._event_time = datetime.now(timezone.utc)
        self.event("task_complete", self.old)
        self.apply_settings(record)
        self.accepted()
        self.assertEqual(self.step("confirm")["status"], "mismatch")

    def test_legacy_staged_record_must_revalidate_settings_before_marker(self):
        record = self.prepare_switch()
        self.claim()
        self._event_time = datetime.now(timezone.utc)
        self.apply_settings(record)
        self.accepted()
        self.assertEqual(self.step("confirm")["status"], "staged")
        # Reproduce a legacy timestamp-only stage with the same evidence, but
        # actual log order marker -> settings. Neither timestamp changes.
        lines = self.rollout.read_text(encoding="utf-8").splitlines(True)
        lines[-2], lines[-1] = lines[-1], lines[-2]
        self.rollout.write_text("".join(lines), encoding="utf-8")
        self.next_human_turn(record["target"])
        self.assertEqual(self.step("confirm")["status"], "mismatch")

    def test_equal_timestamps_reject_settings_after_new_turn_start(self):
        record = self.prepare_switch()
        self.claim()
        self._event_time = datetime.now(timezone.utc)
        self.apply_settings(record)
        different = dict(record["target"], effort="max")
        self.next_human_turn(different)
        self.write("event_msg", type="thread_settings_applied", thread_id=self.thread,
                   thread_settings={"model": different["model"], "reasoning_effort": different["effort"]})
        self.accepted()
        self.assertEqual(self.step("confirm")["status"], "mismatch")

    def test_equal_timestamps_reject_old_turn_completing_after_new_start(self):
        record = self.prepare_switch()
        self.claim()
        self._event_time = datetime.now(timezone.utc)
        self.apply_settings(record)
        new = str(uuid.uuid4())
        self.start(new, record["target"])
        self.human(new, "Continue", "next-message")
        self.event("task_complete", self.old)
        self.accepted()
        self.assertEqual(self.step("confirm")["status"], "mismatch")

    def test_compaction_does_not_erase_prior_work_or_native_input(self):
        for kind in ("Reasoning", "CommandExecution", "UnknownHostItem", "native_input"):
            with self.subTest(kind=kind):
                # Inspect the parser independently, without introducing new sends.
                new = str(uuid.uuid4())
                self.start(new, self.current)
                if kind == "native_input":
                    self.native_input(new, "external trigger")
                else:
                    self.event("item_completed", new, item={"type": kind, "id": "work"})
                self.event("item_completed", new, item={"type": "ContextCompaction", "id": "compact"})
                self.human(new, "Continue", "late-message")
                result = handle(dict(op="main_observe", rollout_path=str(self.rollout), thread_id=self.thread))
                self.assertFalse(result["latest_turn"].get("started_by_human", False))

    def test_recheck_preserves_previous_false_negative_and_cannot_send_again(self):
        from main_switch import _rollout
        record = self.prepare_switch()
        self.claim()
        self.apply_settings(record)
        self.accepted()
        new = self.next_human_turn(record["target"])
        def old_parser(*args, **kwargs):
            facts = _rollout(*args, **kwargs)
            facts["turns"][new]["started_by_human"] = False
            return facts
        with patch("main_switch._rollout", side_effect=old_parser):
            old = self.step("confirm")
        self.assertEqual(old["status"], "mismatch")
        result = self.step("confirm")
        self.assertEqual(result["status"], "confirmed")
        self.assertEqual(result["evidence_correction"]["previous_host_evidence"], old["host_evidence"])
        with self.assertRaisesRegex(StateError, "already claimed"):
            self.claim()
        self.assertEqual(self.step("confirm"), result)

    def test_recheck_cannot_replace_runtime_or_changed_host_evidence(self):
        from main_switch import _rollout
        record = self.prepare_switch()
        self.claim()
        self.apply_settings(record)
        self.accepted()
        new = self.next_human_turn(record["target"])
        def old_parser(*args, **kwargs):
            facts = _rollout(*args, **kwargs)
            facts["turns"][new]["started_by_human"] = False
            return facts
        with patch("main_switch._rollout", side_effect=old_parser):
            old = self.step("confirm")
        self.write("turn_context", turn_id=new, **dict(record["target"], effort="max"))
        self.write("turn_context", turn_id=new, **record["target"])
        self.assertEqual(self.step("confirm"), old)

    def test_delayed_settings_and_marker_cannot_be_attached_to_completed_turn(self):
        record = self.prepare_switch()
        self.claim()
        self.event("task_complete", self.old)
        self.apply_settings(record)
        self.accepted()
        self.assertEqual(self.step("confirm")["status"], "mismatch")

    def test_new_user_input_in_old_turn_disables_send(self):
        self.prepare_switch()
        self.human(self.old, "Change of requirements", "new-user")
        with self.assertRaisesRegex(StateError, "chat input"):
            self.claim()

    def test_new_input_after_claim_is_caught_and_other_caller_cannot_claim(self):
        self.prepare_switch()
        with patch.dict("os.environ", {"CODEX_THREAD_ID": str(uuid.uuid4())}):
            with self.assertRaisesRegex(StateError, "target main chat"):
                self.claim()
        self.claim()
        self.human(self.old, "Do not stage a switch", "new-user")
        with self.assertRaisesRegex(StateError, "chat input"):
            self.step("check", send_permit=self.permit)

    def test_finished_successor_without_runtime_metadata_is_not_left_staged(self):
        record = self.prepare_switch()
        self.claim()
        self.apply_settings(record)
        self.accepted()
        self.event("task_complete", self.old)
        new = str(uuid.uuid4())
        self.event("task_started", new)
        self.event("turn_aborted", new)
        self.assertEqual(self.step("confirm")["status"], "mismatch")

    def test_new_user_settings_are_preserved_when_they_differ(self):
        record = self.prepare_switch()
        self.claim()
        self.apply_settings(record)
        different = dict(record["target"], effort="max")
        self.write("event_msg", type="thread_settings_applied", thread_id=self.thread,
                   thread_settings={"model": different["model"], "reasoning_effort": different["effort"]})
        self.next_human_turn(different)
        self.accepted()
        self.assertEqual(self.step("confirm")["status"], "superseded")

    def test_outline_and_persisted_pins_remain_frozen_on_wrong_next_model(self):
        import hashlib
        self.item.update(task_id='outline-switch', mode="outline", operation="controller",
                         resources=[],read_paths=[],snapshot_files={},artifacts=[],
                         outline_ref=str(self.checkpoint), outline_revision='v1', module_id='switch',
                         requirement_ids=['model'], acceptance_ids=['actual-model'])
        self.call('outline_register', outline={'ref':str(self.checkpoint),'revision':'v1',
                  'sha256':hashlib.sha256(self.checkpoint.read_bytes()).hexdigest(),'source':'test user',
                  'controller_pins':{},'override':None,'delegation':self.item['delegation'],
                  'modules':[{'id':'switch','requirement_ids':['model'],'acceptance_ids':['actual-model'],
                              'work':[{'task_id':'outline-switch','operation':'controller','stage':'strategy',
                                       'requirement_ids':['model'],'acceptance_ids':['actual-model']},
                                      {'task_id':'switch-review','operation':'business','stage':'review',
                                       'requirement_ids':['model'],'acceptance_ids':['actual-model']}]}],
                  'final_acceptance_ids':['integration']})
        self.call("add", task=self.item)
        record = self.prepare_switch()
        self.assertEqual(record["fixed_settings"], {"model": "gpt-6-astra"})
        self.claim(); self.apply_settings(record)
        wrong = {"model": "gpt-6.2-terra", "effort": "high"}
        self.write("event_msg", type="thread_settings_applied", thread_id=self.thread,
                   thread_settings={"model": wrong["model"], "reasoning_effort": wrong["effort"]})
        self.next_human_turn(wrong); self.accepted()
        self.assertEqual(self.step("confirm")["status"], "mismatch")

    def test_persisted_task_pins_are_saved_in_switch_receipt(self):
        self.item['controller_pins'] = {'model': 'gpt-6.2-sol', 'effort': 'high', 'source': 'user'}
        self.call('update', task=self.item)
        record = self.prepare_switch()
        self.assertEqual(record['pins'], self.item['controller_pins'])
        self.assertEqual(record['fixed_settings'], {'model': 'gpt-6.2-sol', 'effort': 'high'})

    def test_runtime_mismatch_without_a_settings_override_is_not_released(self):
        record = self.prepare_switch()
        self.claim()
        self.apply_settings(record)
        self.next_human_turn(dict(record["target"], effort="max"))
        self.accepted()
        result = self.step("confirm")
        self.assertEqual(result["status"], "mismatch")
        self.assertEqual(self.step("confirm"), result)

    def test_intermediate_runtime_effort_mismatch_is_not_hidden(self):
        record = self.prepare_switch()
        self.claim()
        self.apply_settings(record)
        new = self.next_human_turn(record["target"])
        self.write("turn_context", turn_id=new, **dict(record["target"], effort="max"))
        self.write("turn_context", turn_id=new, **record["target"])
        self.accepted()
        self.assertEqual(self.step("confirm")["status"], "mismatch")

    def test_quoted_input_from_unrelated_call_cannot_be_confirmed(self):
        record = self.prepare_switch()
        args = self.claim()
        self.apply_settings(record, "Other instructions quoting <input>" + args["prompt"] + "</input>")
        self.next_human_turn(record["target"])
        self.accepted()
        self.assertEqual(self.step("confirm")["status"], "mismatch")

    def test_tool_error_never_creates_second_permit(self):
        self.prepare_switch()
        self.claim()
        self.assertEqual(self.step("receipt", outcome="native_send_error_or_unknown", send_permit=self.permit)["status"], "unknown")
        with self.assertRaisesRegex(StateError, "already claimed"):
            self.step("claim")
        with self.assertRaisesRegex(StateError, "unresolved"):
            self.prepare()
        with self.assertRaisesRegex(StateError, "resolve the main-thread switch"):
            self.call("prepare", task_id=self.item["task_id"])

    def test_preparation_validates_human_grant_and_turn_identity(self):
        with self.assertRaisesRegex(StateError, "turn or observed"):
            self.prepare(before_turn_id=str(uuid.uuid4()))
        self.human(self.old, "禁止自动续接", "deny")
        with self.assertRaisesRegex(StateError, "human authorization"):
            self.prepare(authorization={"message_id": "deny", "answer": "禁止自动续接",
                                        "purpose": "task_continuation", "max_sends": 1, "reviewed_through": "deny"})
        with self.assertRaisesRegex(StateError, "human authorization"):
            self.prepare(authorization={"message_id": "grant", "answer": "允许在本聊天为当前任务自动续接2次",
                                        "purpose": "task_continuation", "max_sends": 2, "reviewed_through": "deny"})
        with self.assertRaisesRegex(StateError, "latest human input"):
            self.prepare()
        self.assertNotIn("main_switches", self.call("status"))

    def test_early_new_turn_cannot_claim_even_with_same_model(self):
        self.prepare_switch()
        self.event("task_complete", self.old)
        self.start(str(uuid.uuid4()), self.current)
        with self.assertRaisesRegex(StateError, "turn boundary changed"):
            self.step("claim")
        self.assertEqual(self.step("cancel", outcome="new user turn, no invocation")["status"], "cancelled")

    def test_switch_cannot_bypass_existing_model_failure(self):
        original = self.item
        self.call("update", task=dict(original, features=dict(original["features"], stage="execution")))
        attempt = self.call("prepare", task_id=original["task_id"], catalog=CATALOG,
                            catalog_provenance={"entrypoint": "collaboration.spawn_agent", "captured_at": now(),
                                                "source": "runtime-tool"},
                            profiles_dir=str(Path(__file__).parent / "agents"))
        self.call("fail", attempt_id=attempt["attempt_id"], category="model", evidence=["synthetic model rejection"],
                  host_no_spawn_evidence="no worker created")
        self.call("update", task=original)
        with self.assertRaisesRegex(StateError, "unresolved model failure"):
            self.prepare()

    def test_resolving_unknown_does_not_authorize_another_send(self):
        answer = "允许在本聊天为当前任务自动续接3次"
        self.human(self.old, answer, "grant3")
        grant = {"message_id": "grant3", "answer": answer, "purpose": "task_continuation",
                 "max_sends": 3, "reviewed_through": "grant3"}
        self.prepare_switch(authorization=grant)
        self.claim()
        self.step("receipt", outcome="native_send_error_or_unknown", send_permit=self.permit)
        self.old = str(uuid.uuid4())
        self.start(self.old, self.current)
        resolution = f"允许解除本聊天续接请求 {self.rid} 的冻结"
        self.human(self.old, resolution, "resolve")
        self.assertEqual(self.step("resolve", evidence="checked host and helper stopped",
                                   authorization={"message_id": "resolve", "answer": resolution, "purpose": "resolve_switch",
                                                  "max_sends": 0, "reviewed_through": "resolve"})["status"], "resolved")
        with self.assertRaisesRegex(StateError, "new human send grant"):
            self.prepare(phase="next-architecture", authorization=dict(grant, reviewed_through="resolve"))


if __name__ == "__main__":
    unittest.main()
