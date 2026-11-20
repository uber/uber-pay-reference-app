import ast
import json
import unittest

from conformance.contract import ROOT, SPEC


class OwnerDecisionTest(unittest.TestCase):
    def test_every_decision_is_accepted_with_implemented_test_evidence(self):
        register = json.loads((ROOT / "contracts/decisions.json").read_text())
        self.assertEqual(register["status"], "accepted")
        self.assertEqual(
            {r["id"] for r in register["decisions"]}, {f"U{i}" for i in range(1, 16)}
        )
        tests = {
            n.name
            for p in (ROOT / "conformance").glob("*suite.py")
            for n in ast.walk(ast.parse(p.read_text()))
            if isinstance(n, ast.FunctionDef)
        }
        for decision in register["decisions"]:
            self.assertEqual(decision["status"], "accepted")
            self.assertIn(decision["acceptance_test"], tests)
            self.assertTrue(decision["choice"] and decision["rationale"])
        self.assertTrue(register["upstream_pin_unchanged"])

    def test_cancelled_is_a_visible_reference_extension_only(self):
        upstream = json.loads((ROOT / "contracts/uberpay-openapi.json").read_text())

        def statuses(s):
            return s["paths"]["/find-transaction"]["get"]["responses"]["200"][
                "content"
            ]["application/json"]["schema"]["properties"]["status"]["enum"]

        self.assertNotIn("CANCELLED", statuses(upstream))
        self.assertEqual(set(statuses(SPEC)) - set(statuses(upstream)), {"CANCELLED"})
