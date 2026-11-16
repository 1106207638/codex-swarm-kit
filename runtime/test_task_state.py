import multiprocessing
import tempfile
import unittest
import json
from unittest.mock import patch
from pathlib import Path

from task_state import StateError, Store, input_digest


CATALOG = {"source": "runtime-tool", "models": [
    {"id": "gpt-6.2-luna", "reasoning_efforts": ["medium", "high", "max"]},
    {"id": "gpt-6.2-sol", "reasoning_efforts": ["high", "max"]},
    {"id": "gpt-6.2-terra", "reasoning_efforts": ["high"]},
]}
PROFILES = str(Path(__file__).parent / "agents")


def make_task(root, task_id="task", *, deps=None, path=None, stage="execution", plan_ready=True):
    owned = path or (Path(root) / "src" / "shared.py")
    return {"task_id": task_id, "goal": "Implement scoped change", "input": "Initial input",
            "workspace": str(Path(root).resolve()), "ownership": [str(owned)],
            "constraints": ["Keep existing behavior"], "forbidden": ["Do not expand scope"],
            "success": ["Observable behavior"], "verification": ["Check runtime behavior"],
            "escalation": ["Stop on scope expansion"], "depends_on": deps or [], "input_version": "v1",
            "features": {"stage": stage, "ambiguity": "clear", "risk": "normal", "complexity": "normal",
                         "deterministic": False, "multi_source": False, "plan_ready": plan_ready,
                         "required_tools": [], "available_tools": []},
            "delegation": {"authorized": True, "prohibited": False, "source": "test authorization"},
            "approval": {"required": False, "granted": False},
            "current": {"model": "gpt-6-astra", "effort": "high"}}


def provenance():
    return {"source": "runtime-tool", "captured_at": "2026-11-09T13:00:00Z", "entrypoint": "runtime-test"}


def prepare(store, owner, epoch, task_id="task", catalog=CATALOG):
    return store.apply("prepare", owner, epoch, task_id=task_id, catalog=catalog,
                       catalog_provenance=provenance(), profiles_dir=PROFILES, host_slots_available=2)


def _race_prepare(barrier, output, root, owner, epoch, task_id):
    store = Store(root)
    barrier.wait(timeout=15)
    try:
        result = prepare(store, owner, epoch, task_id)
        output.put((task_id, "ok", result["attempt_id"]))
    except StateError as exc:
        output.put((task_id, "blocked", str(exc)))


class TaskStateTests(unittest.TestCase):
    def legacy_direct(self, *, override=False, review=False, override_metadata=None, current_metadata=None):
        """Persist the documented 3.0 prepared-decision shape, then reopen it."""
        from router_policy import decide
        item = make_task(self.workspace, stage="review" if review else "strategy")
        item["current"].update(current_metadata or {})
        if review:
            item["delegation"] = {"authorized": False, "prohibited": False, "source": "missing authorization"}
        old = decide(item, CATALOG)
        if override:
            item["override"] = {key: item["current"][key] for key in ("model", "effort")}
            item["override"].update(override_metadata or {})
        elif not review:
            old["effort"] = "inherit"
        self.store.apply("add", self.owner, 1, task=item)
        with patch("router_policy.decide", return_value=old):
            pending = prepare(self.store, self.owner, 1)
        state = self.store.status(self.owner, 1)
        state["tasks"]["task"]["direct_pending"].pop("policy_contract")
        self.store.path.write_text(json.dumps(state), encoding="utf-8")
        self.store = Store(self.state_dir)
        return pending, old

    def complete(self, pending, **kwargs):
        return self.store.apply("complete_direct", self.owner, 1, task_id="task",
                                direct_id=pending["direct_id"], input_digest=pending["input_digest"],
                                execution_evidence=["existing operation observed"], acceptance_evidence=["existing artifact checked"],
                                verifier_context_id="coord-A", **kwargs)

    def test_upgrade_accepts_equivalent_old_controller_receipt(self):
        pending, old = self.legacy_direct()
        self.assertEqual(self.complete(pending)["status"], "verified")
        self.assertTrue(self.complete(pending)["idempotent"])
        completion = self.store.status(self.owner, 1)["tasks"]["task"]["direct_completions"][-1]
        self.assertEqual(completion["prepared_decision"], old)
        self.assertEqual(completion["decision"]["effort"], "high")

    def test_upgrade_accepts_equivalent_old_override_but_blocks_new_dispatch(self):
        pending, old = self.legacy_direct(override=True)
        self.assertEqual(self.complete(pending)["status"], "verified")
        item = self.store.status(self.owner, 1)["tasks"]["task"]["versions"][-1]["task"]
        item["input"] = "new work needs new decision"
        self.store.apply("update", self.owner, 1, task=item)
        result = prepare(self.store, self.owner, 1)
        self.assertEqual(result["decision"]["reason_codes"], ["blocked_override_scope"])

    def test_upgrade_does_not_waive_old_review_independence(self):
        pending, old = self.legacy_direct(override=True, review=True)
        with self.assertRaisesRegex(StateError, "explicitly prohibited delegation"):
            self.complete(pending, limitation_acknowledged=True)

    def test_upgrade_accepts_legacy_source_metadata(self):
        pending, old = self.legacy_direct(override=True, override_metadata={"source": "old user instruction"},
                                          current_metadata={"source": "old host metadata"})
        self.assertEqual(self.complete(pending)["status"], "verified")

    def test_upgrade_accepts_already_scoped_equivalent_legacy_decision(self):
        pending, old = self.legacy_direct(override=True,
                                          override_metadata={"scope": "all_tasks", "source": "old explicit instruction"})
        self.assertEqual(self.complete(pending)["status"], "verified")

    def test_upgrade_rejects_mismatched_or_new_contract_receipts(self):
        pending, old = self.legacy_direct()
        state = self.store.status(self.owner, 1)
        record = state["tasks"]["task"]["direct_pending"]
        record["policy_contract"] = "3.1"
        self.store.path.write_text(json.dumps(state), encoding="utf-8")
        with self.assertRaisesRegex(StateError, "no longer"):
            self.complete(pending)
        record.pop("policy_contract")
        record["decision"]["model"] = "different-model"
        self.store.path.write_text(json.dumps(state), encoding="utf-8")
        with self.assertRaisesRegex(StateError, "matching controller"):
            self.complete(pending)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temp.name) / "workspace"
        self.workspace.mkdir()
        self.state_dir = Path(self.temp.name) / "state"
        self.store = Store(self.state_dir)
        self.store.init("coord-A")
        self.owner, self.epoch = "coord-A", 1

    def tearDown(self):
        self.temp.cleanup()

    def add(self, task_id="task", **kwargs):
        return self.store.apply("add", self.owner, self.epoch, task=make_task(self.workspace, task_id, **kwargs))

    def test_owner_epoch_and_init_are_explicit(self):
        with self.assertRaisesRegex(StateError, "already exists"):
            self.store.init("other")
        with self.assertRaisesRegex(StateError, "owner mismatch"):
            self.store.status("other", 1)
        with self.assertRaisesRegex(StateError, "stale"):
            self.store.status(self.owner, 2)

    def test_prepare_bind_report_verify_and_idempotent_receipts(self):
        self.add()
        prepared = prepare(self.store, self.owner, self.epoch)
        attempt, digest = prepared["attempt_id"], prepared["input_digest"]
        self.assertEqual(prepared["status"], "dispatch_pending")
        self.assertEqual(prepared["usage"], "unknown")
        bound = self.store.apply("bind", self.owner, 1, attempt_id=attempt, agent_id="worker-1")
        self.assertEqual(bound["status"], "bound")
        self.assertTrue(self.store.apply("bind", self.owner, 1, attempt_id=attempt, agent_id="worker-1")["idempotent"])
        with self.assertRaisesRegex(StateError, "different|mismatch|only a pending"):
            self.store.apply("bind", self.owner, 1, attempt_id=attempt, agent_id="worker-2")
        report = {"attempt_id": attempt, "input_digest": digest, "agent_id": "worker-1", "summary": "done",
                  "evidence": ["observed result"], "host_reported_model": "gpt-6.2-luna",
                  "host_stopped_evidence": "host confirms stopped"}
        self.assertEqual(self.store.apply("report", self.owner, 1, **report)["status"], "reported")
        self.assertTrue(self.store.apply("report", self.owner, 1, **report)["idempotent"])
        verify = {"attempt_id": attempt, "input_digest": digest, "verifier_context_id": "reviewer-context",
                  "evidence": ["independent acceptance"]}
        self.assertEqual(self.store.apply("verify", self.owner, 1, **verify)["status"], "verified")
        self.assertTrue(self.store.apply("verify", self.owner, 1, **verify)["idempotent"])
        restored = Store(self.state_dir).status(self.owner, 1)
        self.assertTrue(restored["tasks"]["task"]["verified"])
        self.assertEqual(restored["attempts"][attempt]["host_reported_model"], "gpt-6.2-luna")
        changed = make_task(self.workspace, "task")
        changed["input"] = "New task version"
        self.store.apply("update", self.owner, 1, task=changed)
        second = prepare(self.store, self.owner, 1)
        with self.assertRaisesRegex(StateError, "historical attempt"):
            self.store.apply("bind", self.owner, 1, attempt_id=second["attempt_id"], agent_id="worker-1")
        self.store.apply("bind", self.owner, 1, attempt_id=second["attempt_id"], agent_id="worker-2")

    def test_digest_replay_and_verification_require_stopped_worker(self):
        self.add()
        prepared = prepare(self.store, self.owner, 1)
        aid, digest = prepared["attempt_id"], prepared["input_digest"]
        self.store.apply("bind", self.owner, 1, attempt_id=aid, agent_id="worker")
        with self.assertRaisesRegex(StateError, "input_digest"):
            self.store.apply("report", self.owner, 1, attempt_id=aid, input_digest="stale", agent_id="worker",
                             summary="done", evidence=["x"])
        self.store.apply("report", self.owner, 1, attempt_id=aid, input_digest=digest, agent_id="worker",
                         summary="done", evidence=["x"])
        with self.assertRaisesRegex(StateError, "stopped"):
            self.store.apply("verify", self.owner, 1, attempt_id=aid, input_digest=digest,
                             verifier_context_id="reviewer", evidence=["accept"])
        self.store.apply("reconcile", self.owner, 1, attempt_id=aid, action="stopped", evidence=["host stopped worker"])
        self.assertEqual(self.store.apply("verify", self.owner, 1, attempt_id=aid, input_digest=digest,
                                          verifier_context_id="reviewer", evidence=["accept"])["status"], "verified")

    def test_dependency_must_exist_verified_and_update_invalidates_descendants(self):
        with self.assertRaisesRegex(StateError, "already exist"):
            self.add("child", deps=["missing"])
        self.add("parent")
        self.add("child", deps=["parent"], path=Path(self.workspace) / "child.py")
        with self.assertRaisesRegex(StateError, "not verified"):
            prepare(self.store, self.owner, 1, "child")
        parent = prepare(self.store, self.owner, 1, "parent")
        self.store.apply("bind", self.owner, 1, attempt_id=parent["attempt_id"], agent_id="parent-worker")
        self.store.apply("report", self.owner, 1, attempt_id=parent["attempt_id"], input_digest=parent["input_digest"],
                         agent_id="parent-worker", summary="done", evidence=["parent evidence"], host_stopped_evidence="stopped")
        self.store.apply("verify", self.owner, 1, attempt_id=parent["attempt_id"], input_digest=parent["input_digest"],
                         verifier_context_id="parent-verifier", evidence=["verified"])
        child = prepare(self.store, self.owner, 1, "child")
        self.store.apply("bind", self.owner, 1, attempt_id=child["attempt_id"], agent_id="child-worker")
        self.store.apply("report", self.owner, 1, attempt_id=child["attempt_id"], input_digest=child["input_digest"],
                         agent_id="child-worker", summary="done", evidence=["child evidence"], host_stopped_evidence="stopped")
        self.store.apply("verify", self.owner, 1, attempt_id=child["attempt_id"], input_digest=child["input_digest"],
                         verifier_context_id="child-verifier", evidence=["verified"])
        changed = make_task(self.workspace, "parent")
        changed["input"] = "Revised parent input"
        result = self.store.apply("update", self.owner, 1, task=changed)
        self.assertEqual(result["invalidated_downstream"], ["child"])
        state = Store(self.state_dir).status(self.owner, 1)
        self.assertFalse(state["tasks"]["child"]["verified"])
        with self.assertRaisesRegex(StateError, "dependencies changed|old task input version|invalidated validation generation"):
            self.store.apply("verify", self.owner, 1, attempt_id=child["attempt_id"], input_digest=child["input_digest"],
                             verifier_context_id="another-verifier", evidence=["stale result"])
        parent2 = prepare(self.store, self.owner, 1, "parent")
        self.store.apply("bind", self.owner, 1, attempt_id=parent2["attempt_id"], agent_id="parent-worker-2")
        self.store.apply("report", self.owner, 1, attempt_id=parent2["attempt_id"], input_digest=parent2["input_digest"],
                         agent_id="parent-worker-2", summary="done again", evidence=["revised parent evidence"], host_stopped_evidence="stopped")
        self.store.apply("verify", self.owner, 1, attempt_id=parent2["attempt_id"], input_digest=parent2["input_digest"],
                         verifier_context_id="parent-verifier-2", evidence=["reverified"])
        child2 = prepare(self.store, self.owner, 1, "child")
        self.assertEqual(child2["status"], "dispatch_pending")

    def test_pending_attempt_freezes_until_explicit_reconcile_then_retry_reason(self):
        self.add()
        first = prepare(self.store, self.owner, 1)
        recovered = Store(self.state_dir)
        with self.assertRaisesRegex(StateError, "active or unknown"):
            prepare(recovered, self.owner, 1)
        self.store.apply("reconcile", self.owner, 1, attempt_id=first["attempt_id"], action="no_spawn", evidence=["host confirms spawn not accepted"])
        with self.assertRaisesRegex(StateError, "retry authorization"):
            prepare(self.store, self.owner, 1)
        self.store.apply("retry", self.owner, 1, attempt_id=first["attempt_id"], controller_reason="Corrected launch configuration",
                         human_authorized=False)
        second = prepare(self.store, self.owner, 1)
        self.assertEqual(second["status"], "dispatch_pending")
        self.assertNotEqual(first["attempt_id"], second["attempt_id"])

    def test_model_failure_is_human_only_and_task_budget_survives_input_update(self):
        self.add()
        first = prepare(self.store, self.owner, 1)
        self.store.apply("fail", self.owner, 1, attempt_id=first["attempt_id"], category="model", evidence=["host model unavailable"],
                         host_no_spawn_evidence="host confirms no spawn")
        with self.assertRaisesRegex(StateError, "human-only"):
            self.store.apply("retry", self.owner, 1, attempt_id=first["attempt_id"], controller_reason="retry", human_authorized=False)
        changed = make_task(self.workspace, "task")
        changed["input"] = "Changed input does not authorize model fallback"
        self.store.apply("update", self.owner, 1, task=changed)
        with self.assertRaisesRegex(StateError, "human-only"):
            prepare(self.store, self.owner, 1)
        self.store.apply("retry", self.owner, 1, attempt_id=first["attempt_id"], controller_reason="Human approves retry",
                         human_authorized=True)
        next_attempt = prepare(self.store, self.owner, 1)
        self.assertEqual(next_attempt["status"], "dispatch_pending")

    def test_reviewer_cannot_reuse_dependency_implementation_context(self):
        self.add("implementation")
        review = make_task(self.workspace, "review", deps=["implementation"], path=Path(self.workspace) / "review.py", stage="review")
        self.store.apply("add", self.owner, 1, task=review)
        impl = prepare(self.store, self.owner, 1, "implementation")
        self.store.apply("bind", self.owner, 1, attempt_id=impl["attempt_id"], agent_id="same-context")
        self.store.apply("report", self.owner, 1, attempt_id=impl["attempt_id"], input_digest=impl["input_digest"],
                         agent_id="same-context", summary="done", evidence=["implementation"], host_stopped_evidence="stopped")
        self.store.apply("verify", self.owner, 1, attempt_id=impl["attempt_id"], input_digest=impl["input_digest"],
                         verifier_context_id="separate", evidence=["verified"])
        reviewer = prepare(self.store, self.owner, 1, "review")
        with self.assertRaisesRegex(StateError, "differ from implementation|historical attempt"):
            self.store.apply("bind", self.owner, 1, attempt_id=reviewer["attempt_id"], agent_id="same-context")
        self.store.apply("bind", self.owner, 1, attempt_id=reviewer["attempt_id"], agent_id="fresh-review-context")

    def test_direct_action_can_verify_dependency_without_fake_spawn(self):
        direct = make_task(self.workspace, "direct")
        direct["features"].update({"risk": "low", "complexity": "simple", "deterministic": True})
        self.store.apply("add", self.owner, 1, task=direct)
        self.add("child", deps=["direct"], path=Path(self.workspace) / "child.py")
        prepared = prepare(self.store, self.owner, 1, "direct")
        self.assertIsNone(prepared["dispatch"])
        self.assertEqual(prepared["status"], "direct_pending")
        state = self.store.status(self.owner, 1)
        self.assertEqual(state["attempts"], {})
        with self.assertRaisesRegex(StateError, "awaiting completion"):
            prepare(self.store, self.owner, 1, "direct")
        completed = {"task_id": "direct", "direct_id": prepared["direct_id"], "input_digest": prepared["input_digest"],
                     "execution_evidence": ["tool output"], "acceptance_evidence": ["behavior checked"],
                     "verifier_context_id": "coordinator-self-check"}
        self.assertEqual(self.store.apply("complete_direct", self.owner, 1, **completed)["status"], "verified")
        self.assertTrue(self.store.apply("complete_direct", self.owner, 1, **completed)["idempotent"])
        child = prepare(self.store, self.owner, 1, "child")
        self.assertEqual(child["status"], "dispatch_pending")

    def test_direct_pending_blocks_update_and_handoff_until_reconciled(self):
        direct = make_task(self.workspace, "direct")
        direct["features"].update({"risk": "low", "complexity": "simple", "deterministic": True})
        self.store.apply("add", self.owner, 1, task=direct)
        prepared = prepare(self.store, self.owner, 1, "direct")
        changed = make_task(self.workspace, "direct")
        changed["input"] = "new input"
        with self.assertRaisesRegex(StateError, "direct action may still be active"):
            self.store.apply("update", self.owner, 1, task=changed)
        with self.assertRaisesRegex(StateError, "handoff requires"):
            self.store.apply("handoff", self.owner, 1, new_owner="coord-B", evidence=["handoff"])
        self.store.apply("reconcile_direct", self.owner, 1, task_id="direct", direct_id=prepared["direct_id"],
                         action="no_action", evidence=["host confirms command not run"])
        self.store.apply("update", self.owner, 1, task=changed)

    def test_direct_review_can_downgrade_independence_only_on_explicit_prohibition(self):
        review = make_task(self.workspace, "review", stage="review")
        review["delegation"] = {"authorized": False, "prohibited": False, "source": "tool-unavailable"}
        self.store.apply("add", self.owner, 1, task=review)
        prepared = prepare(self.store, self.owner, 1, "review")
        with self.assertRaisesRegex(StateError, "explicitly prohibited delegation"):
            self.store.apply("complete_direct", self.owner, 1, task_id="review", direct_id=prepared["direct_id"],
                             input_digest=prepared["input_digest"], execution_evidence=["review done"],
                             acceptance_evidence=["checked"], verifier_context_id="self", limitation_acknowledged=True)

    def test_actual_host_model_mismatch_cannot_be_verified(self):
        self.add()
        prepared = prepare(self.store, self.owner, 1)
        self.store.apply("bind", self.owner, 1, attempt_id=prepared["attempt_id"], agent_id="worker")
        self.store.apply("report", self.owner, 1, attempt_id=prepared["attempt_id"], input_digest=prepared["input_digest"],
                         agent_id="worker", summary="done", evidence=["reported"], host_stopped_evidence="stopped",
                         host_reported_model="gpt-6.99-luna")
        with self.assertRaisesRegex(StateError, "host-reported model differs"):
            self.store.apply("verify", self.owner, 1, attempt_id=prepared["attempt_id"], input_digest=prepared["input_digest"],
                             verifier_context_id="verifier", evidence=["accept"])

    def test_task_id_whitespace_is_rejected_without_corrupting_state(self):
        task = make_task(self.workspace, " spaced ")
        with self.assertRaisesRegex(StateError, "task_id must not contain"):
            self.store.apply("add", self.owner, 1, task=task)
        self.assertEqual(self.store.status(self.owner, 1)["tasks"], {})


    def test_write_overlap_rejected_and_read_only_parallel(self):
        self.add("one")
        self.add("two", path=Path(self.workspace) / "src" / "shared.py")
        one = prepare(self.store, self.owner, 1, "one")
        with self.assertRaisesRegex(StateError, "overlaps active"):
            prepare(self.store, self.owner, 1, "two")

    def test_capacity_and_same_agent_context(self):
        self.add("one")
        self.add("two", path=Path(self.workspace) / "two.py")
        self.add("three", path=Path(self.workspace) / "three.py")
        first = prepare(self.store, self.owner, 1, "one")
        self.store.apply("bind", self.owner, 1, attempt_id=first["attempt_id"], agent_id="agent-one")
        second = prepare(self.store, self.owner, 1, "two")
        self.store.apply("bind", self.owner, 1, attempt_id=second["attempt_id"], agent_id="agent-two")
        with self.assertRaisesRegex(StateError, "limit reached"):
            prepare(self.store, self.owner, 1, "three")

    def test_corrupt_json_and_nested_schema_fail_closed(self):
        self.add()
        prepared = prepare(self.store, self.owner, 1)
        state = Store(self.state_dir).status(self.owner, 1)
        state["attempts"][prepared["attempt_id"]]["task_snapshot"] = {"task_id": "tampered"}
        self.store.path.write_text(__import__("json").dumps(state), encoding="utf-8")
        with self.assertRaisesRegex(StateError, "digest mismatch|does not match"):
            Store(self.state_dir).status(self.owner, 1)
        self.store.path.write_text("{", encoding="utf-8")
        with self.assertRaisesRegex(StateError, "corrupt"):
            Store(self.state_dir).status(self.owner, 1)

    def test_handoff_requires_resolved_attempt_and_invalidates_old_epoch(self):
        self.add()
        pending = prepare(self.store, self.owner, 1)
        with self.assertRaisesRegex(StateError, "handoff requires"):
            self.store.apply("handoff", self.owner, 1, new_owner="coord-B", evidence=["handoff note"])
        self.store.apply("reconcile", self.owner, 1, attempt_id=pending["attempt_id"], action="no_spawn", evidence=["no spawn"])
        result = self.store.apply("handoff", self.owner, 1, new_owner="coord-B", evidence=["complete handoff"])
        self.assertEqual(result["epoch"], 2)
        with self.assertRaisesRegex(StateError, "owner mismatch"):
            self.store.status(self.owner, 1)

    def test_two_processes_cannot_reserve_same_write_scope(self):
        self.add("race-a")
        self.add("race-b")
        ctx = multiprocessing.get_context("spawn")
        barrier, output = ctx.Barrier(2), ctx.Queue()
        processes = [ctx.Process(target=_race_prepare, args=(barrier, output, str(self.state_dir), self.owner, self.epoch, task_id))
                     for task_id in ("race-a", "race-b")]
        for process in processes:
            process.start()
        results = [output.get(timeout=30) for _ in processes]
        for process in processes:
            process.join(timeout=30)
            self.assertEqual(process.exitcode, 0)
        self.assertEqual(sum(result[1] == "ok" for result in results), 1, results)
        self.assertEqual(sum(result[1] == "blocked" for result in results), 1, results)


if __name__ == "__main__":
    unittest.main()
