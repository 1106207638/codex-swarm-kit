"""Black-box tests through separate CLI processes, authored by the controller."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from test_router_policy import CATALOG, task, whole_task


ROOT = Path(__file__).resolve().parent


class CliTests(unittest.TestCase):
    def setUp(self):
        self.scratch = Path(tempfile.mkdtemp(prefix="codex-router-cli-"))
        self.base = {"state_dir": str(self.scratch / "ledger"), "owner": "test-controller", "epoch": 1}

    def cli(self, op, expected=0, file_input=False, **kwargs):
        request = dict(self.base, op=op, **kwargs)
        command = [sys.executable, str(ROOT / "routing.py")]
        data = json.dumps(request, ensure_ascii=False)
        if file_input:
            file = self.scratch / "request.json"
            file.write_text(data, encoding="utf-8")
            command += ["--request", str(file)]
            data = None
        process = subprocess.run(command, input=data, text=True, encoding="utf-8", capture_output=True,
                                 env=dict(os.environ, PYTHONIOENCODING="utf-8"), timeout=15)
        self.assertEqual(process.returncode, expected, process.stdout + process.stderr)
        self.assertEqual(process.stderr, "")
        response = json.loads(process.stdout)
        self.assertEqual(response["ok"], expected == 0)
        return response.get("result", response)

    def initialize(self, **overrides):
        self.cli("init")
        item = task(workspace=str(self.scratch), ownership=[str(self.scratch / "owned.txt")], **overrides)
        self.cli("add", task=item)
        return item

    def prepare(self, item, **kwargs):
        return self.cli("prepare", task_id=item["task_id"], catalog=CATALOG,
                        catalog_provenance={"source": "runtime-tool", "captured_at": "fixture-time",
                                            "entrypoint": "synthetic-test-only"},
                        profiles_dir=str(ROOT / "agents"), **kwargs)

    def test_full_lifecycle_and_receipt_replay_across_processes(self):
        item = self.initialize()
        pending = self.prepare(item)
        self.assertEqual(pending["status"], "dispatch_pending")
        self.assertEqual(pending["spawn_args"]["fork_turns"], "none")
        identity = {"attempt_id": pending["attempt_id"], "agent_id": "test-worker"}
        self.cli("bind", **identity)
        self.assertTrue(self.cli("bind", **identity)["idempotent"])
        report = dict(identity, input_digest=pending["input_digest"], summary="明确结果：17",
                      evidence=["fixed fixture total 17"], host_stopped_evidence="synthetic host terminal")
        self.cli("report", file_input=True, **report)
        self.assertTrue(self.cli("report", **report)["idempotent"])
        state = self.cli("status")
        self.assertFalse(state["tasks"][item["task_id"]]["verified"])
        self.cli("verify", attempt_id=pending["attempt_id"], input_digest=pending["input_digest"],
                 verifier_context_id="test-controller", evidence=["recomputed 7+10=17"])
        self.assertTrue(self.cli("status")["tasks"][item["task_id"]]["verified"])

    def test_unknown_dispatch_never_retries_and_other_owner_cannot_write(self):
        item = self.initialize()
        self.prepare(item)
        state_file = Path(self.base["state_dir"]) / "state.json"
        before = state_file.read_bytes()
        self.prepare(item, expected=2)
        self.cli("add", expected=2, owner="other-chat", task=dict(item, task_id="other"))
        self.cli("init", expected=2, owner="other-chat")
        self.assertEqual(before, state_file.read_bytes())

    def test_gate_no_model_fallback_and_no_fake_attempt(self):
        item = self.initialize(approval={"required": True, "granted": False})
        result = self.prepare(item)
        self.assertEqual(result["decision"]["reason_codes"], ["blocked_approval"])
        self.assertNotIn("spawn_args", result)
        item["approval"]["granted"] = True
        self.cli("update", task=item)
        decision = self.cli("route", task=item, catalog={"source": "runtime-tool", "models": []})["decision"]
        self.assertEqual(decision["reason_codes"], ["blocked_model"])
        self.assertEqual(self.cli("status")["attempts"], {})

    def test_template_is_not_live_authorization(self):
        template = self.cli("template")
        self.assertFalse(template["task"]["delegation"]["authorized"])
        self.assertIn("TEMPLATE", template["task"]["current"]["model"])

    def test_direct_tool_completion_unblocks_dependents_without_spawn(self):
        item = self.initialize(features={"deterministic": True, "complexity": "simple", "risk": "low"})
        direct = self.prepare(item)
        self.assertEqual(direct["decision"]["action"], "direct")
        self.assertNotIn("spawn_args", direct)
        self.cli("complete_direct", task_id=item["task_id"], direct_id=direct["direct_id"],
                 input_digest=direct["input_digest"], verifier_context_id="test-controller",
                 execution_evidence=["ran fixed arithmetic"], acceptance_evidence=["assert total equals 17"])
        follower = task(task_id="follower", workspace=str(self.scratch),
                        ownership=[str(self.scratch / "owned.txt")], depends_on=[item["task_id"]])
        self.cli("add", task=follower)
        self.assertIn("spawn_args", self.prepare(follower))
        self.assertEqual(len(self.cli("status")["attempts"]), 1)

    def test_invalid_state_returns_error_without_reset(self):
        self.initialize()
        state_file = Path(self.base["state_dir"]) / "state.json"
        state_file.write_text("{invalid", encoding="utf-8")
        self.cli("status", expected=2)
        self.cli("init", expected=2)
        self.assertEqual(state_file.read_text(encoding="utf-8"), "{invalid")

    def test_manual_sol_main_prepares_luna_and_survives_process_restart(self):
        item = self.initialize(current={"model": "gpt-6.1-sol", "effort": "ultra"})
        pending = self.prepare(item)
        self.assertEqual((pending["spawn_args"]["model"], pending["spawn_args"]["reasoning_effort"]),
                         ("gpt-6.2-luna", "medium"))
        # Each cli call is a fresh process; recovery must preserve the attempt,
        # prohibit duplicate dispatch and reject changes while it is unknown.
        state = self.cli("status")
        self.assertEqual(len(state["attempts"]), 1)
        self.prepare(item, expected=2)
        changed = dict(item, current={"model": "gpt-6.2-terra", "effort": "high"})
        self.cli("update", task=changed, expected=2)
        identity = {"attempt_id": pending["attempt_id"], "agent_id": "manual-main-worker"}
        self.cli("bind", **identity)
        self.cli("report", **identity, input_digest=pending["input_digest"], summary="fixture result",
                 evidence=["fixture result observed"], host_stopped_evidence="synthetic terminal event")
        self.cli("verify", attempt_id=pending["attempt_id"], input_digest=pending["input_digest"],
                 verifier_context_id="manual-main-worker", evidence=["self-sign is forbidden"], expected=2)
        self.cli("verify", attempt_id=pending["attempt_id"], input_digest=pending["input_digest"],
                 verifier_context_id="test-controller", evidence=["fixture independently checked"])

    def test_legacy_override_blocks_new_dispatch_without_rewriting_history(self):
        legacy = {"model": "gpt-6.2-sol", "effort": "high"}
        item = self.initialize(current={"model": "gpt-6.2-sol", "effort": "high"}, override=legacy)
        result = self.prepare(item)
        self.assertEqual(result["decision"]["reason_codes"], ["blocked_override_scope"])
        self.assertNotIn("spawn_args", result)
        before = self.cli("status")
        self.assertEqual(before["attempts"], {})
        # The confirmed UI selection belongs only in current. Explicit update,
        # not loading/recovery, removes the ambiguous legacy override.
        item.pop("override")
        self.cli("update", task=item)
        pending = self.prepare(item)
        self.assertEqual(pending["spawn_args"]["model"], "gpt-6.2-luna")
        after = self.cli("status")
        self.assertEqual(len(after["attempts"]), 1)
        self.assertIn(legacy, [v["task"].get("override") for v in after["tasks"][item["task_id"]]["versions"]])

    def test_whole_task_sol_keeps_review_independent_and_role_effort(self):
        item = self.initialize(features={"stage": "review"},
                               current={"model": "gpt-6.2-sol", "effort": "high"},
                               override=whole_task("gpt-6.2-sol"))
        result = self.prepare(item)
        self.assertEqual(result["spawn_args"]["model"], "gpt-6.2-sol")
        self.assertEqual(result["spawn_args"]["reasoning_effort"], "max")
        self.assertEqual(result["spawn_args"]["fork_turns"], "none")
        self.assertTrue(result["decision"]["independence_required"])
        self.assertTrue(self.cli("status")["attempts"][result["attempt_id"]]["read_only"])

    def test_whole_task_override_does_not_clear_model_failure_lock(self):
        item = self.initialize(current={"model": "gpt-6.2-sol", "effort": "high"})
        pending = self.prepare(item)
        self.cli("fail", attempt_id=pending["attempt_id"], category="model",
                 evidence=["synthetic unavailable"], host_no_spawn_evidence="synthetic host rejection")
        item["override"] = whole_task("gpt-6.2-sol")
        self.cli("update", task=item)
        self.prepare(item, expected=2)
        self.assertEqual(len(self.cli("status")["attempts"]), 1)

    def test_manual_main_review_without_delegation_retains_acceptance_gate(self):
        item = self.initialize(features={"stage": "review"},
                               current={"model": "main-only-model", "effort": "low"},
                               delegation={"authorized": False, "prohibited": False})
        result = self.prepare(item)
        self.assertEqual(result["decision"]["model"], "main-only-model")
        self.cli("complete_direct", task_id=item["task_id"], direct_id=result["direct_id"],
                 input_digest=result["input_digest"], verifier_context_id="test-controller",
                 execution_evidence=["self check"], acceptance_evidence=["self check"],
                 limitation_acknowledged=True, expected=2)

    def test_legacy_scope_diagnostic_survives_precise_retry_authorization(self):
        item = self.initialize()
        pending = self.prepare(item)
        self.cli("fail", attempt_id=pending["attempt_id"], category="model",
                 evidence=["synthetic unavailable"], host_no_spawn_evidence="synthetic host rejection")
        item["override"] = {"model": "gpt-6.2-sol", "effort": "high"}
        self.cli("update", task=item)
        self.cli("retry", attempt_id=pending["attempt_id"], human_authorized=True,
                 controller_reason="synthetic explicit human authorization",
                 requested_model="gpt-6.2-sol", requested_effort="high")
        result = self.prepare(item)
        self.assertEqual(result["decision"]["reason_codes"], ["blocked_override_scope"])
        self.assertEqual(len(self.cli("status")["attempts"]), 1)


if __name__ == "__main__":
    unittest.main()
