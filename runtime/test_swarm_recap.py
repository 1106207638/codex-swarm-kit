import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from routing import run
from test_router_policy import task, CATALOG


class RecapTests(unittest.TestCase):
    def setUp(self):
        self.folder = Path(self.enterContext(TemporaryDirectory()))
        self.root = self.folder / "ledger"
        self.call("init")

    def call(self, op, *, expected=0, **args):
        result, code = run({"state_dir": str(self.root), "owner": "root", "epoch": 1, "op": op, **args})
        self.assertEqual(code, expected, result)
        return result.get("result", result)

    def item(self, name="one", group="g", **kw):
        t = task(task_id=name, recap_scope=group, input="private body", workspace=str(self.folder),
                 ownership=[str(self.folder/name)], **kw)
        self.call("add", task=t)
        return t

    def prepare(self, tid="one"):
        return self.call("prepare", task_id=tid, catalog=CATALOG,
                         catalog_provenance=dict(source="runtime-tool", captured_at="fixture",entrypoint="test"),
                         profiles_dir=str(Path(__file__).parent/"agents"))

    def recap(self):
        return self.call("swarm_recap", scope={"group":"g"})

    def worker(self):
        self.item()
        p = self.prepare()
        ids = dict(attempt_id=p["attempt_id"], input_digest=p["input_digest"])
        self.call("bind", **ids, agent_id="opaque")
        self.call("report", **ids, agent_id="opaque", summary="private report",
                  evidence=["private trace"], host_stopped_evidence="host receipt")
        return ids

    def note(self, ids, choice="adopted", **more):
        return self.call("recap_note", **ids, choice=choice, assignment="检查分类",output="三项一致",
                         reason="逐项对照原样例，作为验收依据", **more)

    def test_scope_direct_only_and_unknown_then_no_spawn(self):
        self.item("direct", features={"risk":"low","complexity":"simple","deterministic":True})
        self.assertEqual(self.prepare("direct")["decision"]["action"],"direct")
        self.assertEqual(self.recap()["text"],"")
        self.item()
        p = self.prepare()
        self.assertEqual(self.recap()["unknown_attempts"],1)
        self.call("reconcile",attempt_id=p["attempt_id"],action="no_spawn",evidence=["host confirmed"])
        r = self.recap()
        self.assertFalse(r["used"])
        self.assertEqual(r["not_started_attempts"],1)

    def test_bound_reported_verified_does_not_imply_adoption(self):
        ids = self.worker()
        self.note(ids,expected=2)
        self.call("verify",**ids,verifier_context_id="root",evidence=["private acceptance"])
        self.assertIsNone(self.recap()["attempts"][0]["decision"])
        self.note(ids,"partial")
        self.assertTrue(self.note(ids,"partial")["idempotent"])
        self.note(ids,"adopted",expected=2)
        before = [(self.root/f).read_bytes() for f in ("state.json","calls.jsonl")]
        r = self.recap()
        self.assertEqual(before, [(self.root/f).read_bytes() for f in ("state.json","calls.jsonl")])
        self.assertEqual(r["confirmed_attempts"],1)
        row = r["attempts"][0]
        self.assertEqual(row["actual_model"],"unknown")
        self.assertEqual(row["log_gaps"],[])
        self.assertIn("部分采纳",r["text"])
        self.assertNotIn("private",json.dumps(r))
        self.assertNotIn("逐项",(self.root/"calls.jsonl").read_text())
        logs=(self.root/"calls.jsonl").read_text().splitlines()
        for call in row["calls"]:
            self.assertEqual(json.loads(logs[call["finish"]-1])["call_id"],call["id"])

    def test_scoped_failure_and_evidence_gap(self):
        ids = self.worker()
        self.call("fail", **ids, agent_id="opaque",category="validation",evidence=["bad output"],host_stopped_evidence="done")
        self.note(ids,"returned")
        self.item("other","elsewhere")
        self.prepare("other")
        self.call("swarm_recap",scope={"legacy_tasks":["one"]},expected=2)
        self.call("swarm_recap",scope={},expected=2)
        self.call("swarm_recap",scope={"group":"g"},owner="stranger",expected=2)
        log=self.root/"calls.jsonl"
        rows=log.read_bytes().splitlines(keepends=True)
        log.write_bytes(b"".join(line for line in rows if json.loads(line)["op"]!="bind")+b"damaged")
        r=self.recap()
        self.assertEqual(len(r["attempts"]),1)
        self.assertTrue(r["used"])
        self.assertEqual(r["skipped_log_lines"],1)
        self.assertIn("bind",r["attempts"][0]["log_gaps"])

    def test_new_version_preserves_history_and_scope(self):
        ids=self.worker()
        self.call("verify",**ids,verifier_context_id="root",evidence=["ok"])
        self.note(ids)
        old=self.call("status")["tasks"]["one"]["versions"][-1]["task"]
        self.call("update",task={**old,"recap_scope":"elsewhere"},expected=2)
        self.call("update",task={**old,"input":"replacement","input_version":"2"})
        self.assertFalse(self.recap()["attempts"][0]["current_input"])

    def test_prepare_ignores_extraneous_attempt_and_nested_task_owns_registration(self):
        self.item()
        aid=self.prepare()["attempt_id"]
        self.item("two","other",features={"risk":"low","complexity":"simple","deterministic":True})
        self.call("prepare",task_id="two",attempt_id=aid,catalog=CATALOG,
                  catalog_provenance=dict(source="runtime-tool", captured_at="fixture",entrypoint="test"))
        two=self.call("status")["tasks"]["two"]["versions"][-1]["task"]
        self.call("add",task=two,task_id="one",expected=2)
        row=self.recap()["attempts"][0]
        self.assertEqual([c["op"] for c in row["calls"]],["prepare"])
        self.assertEqual(self.recap()["routing_issues"],[])
        self.assertEqual(self.call("swarm_recap",scope={"group":"other"})["routing_issues"][0]["op"],"add")

    def test_host_model_metadata_never_echoes_raw_worker_strings(self):
        self.item()
        p=self.prepare()
        ids=dict(attempt_id=p["attempt_id"],input_digest=p["input_digest"],agent_id="worker")
        self.call("bind",**ids)
        self.call("report",**ids,summary="private",evidence=["private"],host_reported_model="PRIVATE-MODEL",
                  host_reported_effort="PRIVATE-EFFORT")
        r=self.recap()
        self.assertNotIn("PRIVATE",json.dumps(r))
        self.assertEqual(r["attempts"][0]["actual_model"],"unknown")

    def test_earlier_success_cannot_supply_missing_failure_or_reconciliation(self):
        ids=self.worker()
        self.call("fail",**ids,category="validation",agent_id="opaque",evidence=["mismatch"])
        log=self.root/"calls.jsonl"
        lines=log.read_bytes().splitlines(keepends=True)
        log.write_bytes(b"".join(line for line in lines if json.loads(line)["op"]!="fail"))
        self.assertIn("failure",self.recap()["attempts"][0]["log_gaps"])
        self.item("second")
        aid=self.prepare("second")["attempt_id"]
        self.call("reconcile",attempt_id=aid,action="no_spawn",evidence=["host rejection"])
        lines=log.read_bytes().splitlines(keepends=True)
        log.write_bytes(b"".join(line for line in lines if json.loads(line)["op"]!="reconcile"))
        self.assertIn("reconcile", self.recap()["attempts"][1]["log_gaps"])
        log.write_bytes(b"".join(lines[:-1]))
        self.assertIn("incomplete_call",self.recap()["attempts"][1]["log_gaps"])

    def test_read_legacy_ledger_does_not_create_journal_files(self):
        from task_state import Store
        root=self.folder/"old"
        store=Store(root)
        store.init("owner")
        store.apply("add","owner",1,task=task())
        before={p.name:p.read_bytes() for p in root.iterdir()}
        result,code=run(dict(op="swarm_recap",state_dir=str(root),owner="owner",epoch=1,
                             scope={"legacy_tasks":["feature-42"]}))
        self.assertEqual(code,0,result)
        self.assertEqual(before,{p.name:p.read_bytes() for p in root.iterdir()})
