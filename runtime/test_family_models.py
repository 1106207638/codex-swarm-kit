import copy
import unittest
from resolve_family_models import resolve, ResolutionError


def model(mid, efforts=None, **kwargs):
    return dict(id=mid, reasoning_efforts=efforts if efforts is not None else ["medium", "high", "max"], **kwargs)


def catalog(*models):
    return {"source": "runtime-tool", "models": list(models)}


class ResolverTests(unittest.TestCase):
    def setUp(self):
        self.current = catalog(model("gpt-5.6-luna"), model("gpt-6-luna"),
                               model("gpt-6-sol"), model("gpt-6.1-sol"), model("gpt-6.1-terra"))

    def test_current_six_roles_and_preserved_efforts(self):
        routes = resolve(self.current)["routes"]
        self.assertEqual({k: (v["model"], v["reasoning_effort"]) for k, v in routes.items()}, {
            "luna_worker": ("gpt-6-luna", "medium"), "luna_deep": ("gpt-6-luna", "high"),
            "luna_general": ("gpt-6-luna", "medium"), "terra_general": ("gpt-6.1-terra", "high"),
            "sol_architect": ("gpt-6.1-sol", "max"), "sol_planner": ("gpt-6.1-sol", "high")})
        self.assertTrue(all(r["agent_type"] == "default" and r["fork_turns"] == "none" for r in routes.values()))

    def test_numeric_not_lexical_version(self):
        data = catalog(model("gpt-6.9-sol"), model("gpt-6.10-sol"))
        self.assertEqual(resolve(data, ["sol_architect"])["routes"]["sol_architect"]["model"], "gpt-6.10-sol")

    def test_future_major(self):
        self.current["models"].append(model("gpt-7-luna"))
        self.assertEqual(resolve(self.current)["routes"]["luna_worker"]["model"], "gpt-7-luna")

    def test_family_scoped_missing_unrelated_family(self):
        self.assertEqual(len(resolve(catalog(model("gpt-6-luna")), ["luna_worker"])["routes"]), 1)

    def test_no_family_no_cross_family_fallback(self):
        with self.assertRaises(ResolutionError):
            resolve(catalog(model("gpt-6-astra")), ["luna_worker"])

    def test_highest_missing_effort_does_not_downgrade(self):
        data = catalog(model("gpt-6-sol"), model("gpt-6.1-sol", ["low"]))
        with self.assertRaisesRegex(ResolutionError, "do not downgrade"):
            resolve(data, ["sol_architect"])

    def test_missing_effort_metadata(self):
        with self.assertRaises(ResolutionError):
            resolve(catalog({"id": "gpt-6-sol"}), ["sol_architect"])

    def test_hidden_and_explicit_unavailable(self):
        data = catalog(model("gpt-6-sol"), model("gpt-7-sol", visibility="hide"), model("gpt-8-sol", available=False))
        self.assertEqual(resolve(data, ["sol_architect"])["routes"]["sol_architect"]["model"], "gpt-6-sol")

    def test_no_guessing_aliases_preview_snapshots(self):
        for mid in ["gpt-latest-sol", "gpt-7-sol-preview", "gpt-7-sol-2026-11-06", "gpt-7-solar"]:
            with self.subTest(mid=mid), self.assertRaises(ResolutionError):
                resolve(catalog(model(mid)), ["sol_architect"])

    def test_duplicate_and_ambiguous_versions(self):
        for mids in [("gpt-6-sol", "gpt-6-sol"), ("gpt-6-sol", "gpt-6.0-sol")]:
            with self.subTest(mids=mids), self.assertRaises(ResolutionError):
                resolve(catalog(*(model(mid) for mid in mids)), ["sol_architect"])

    def test_unknown_family_name_alongside_old_stable_stops(self):
        for mid in ["gpt-latest-sol", "gpt-7-sol-preview", "gpt-7-sol-2026-11-06"]:
            with self.subTest(mid=mid), self.assertRaises(ResolutionError):
                resolve(catalog(model("gpt-6.1-sol"), model(mid)), ["sol_architect"])

    def test_invalid_availability_does_not_silently_select_old(self):
        for metadata in [{"available": None}, {"available": 0}, {"available": "false"}, {"visibility": None}, {"visibility": "unknown"}]:
            with self.subTest(metadata=metadata), self.assertRaises(ResolutionError):
                resolve(catalog(model("gpt-6.1-sol"), model("gpt-7-sol", **metadata)), ["sol_architect"])

    def test_cache_not_authority(self):
        data = copy.deepcopy(self.current)
        data["source"] = "models_cache"
        with self.assertRaises(ResolutionError):
            resolve(data)

    def test_malformed_input_fails_closed(self):
        for data in [None, [], {}, catalog(), catalog("gpt-6-sol"), catalog({"id": 6})]:
            with self.subTest(data=data), self.assertRaises(ResolutionError):
                resolve(data)

    def test_no_input_mutation(self):
        before = copy.deepcopy(self.current)
        resolve(self.current)
        self.assertEqual(self.current, before)

    def test_unknown_role(self):
        with self.assertRaises(ResolutionError):
            resolve(self.current, ["spark_worker"])

    def test_planner_effort_override_is_scoped(self):
        routes = resolve(self.current, ["sol_planner"], efforts={"sol_planner": "max"})["routes"]
        self.assertEqual(routes["sol_planner"]["reasoning_effort"], "max")
        for efforts in ({"sol_architect": "high"}, {"sol_planner": "ultra"}, {"luna_worker": "high"}):
            with self.subTest(efforts=efforts), self.assertRaises(ResolutionError):
                resolve(self.current, ["sol_planner"], efforts=efforts)


if __name__ == "__main__":
    unittest.main()
