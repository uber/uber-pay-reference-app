import unittest

from conformance.scenario import assign, expand, field, load
from conformance.contract import ROOT


class ScenarioTests(unittest.TestCase):
    def test_exact_expansion_preserves_large_integer_and_structures(self):
        bindings = {"amount": 9223372036854775807, "nested": {"values": [1, 2]}}
        self.assertEqual(expand("${amount}", bindings), 9223372036854775807)
        self.assertEqual(
            expand("/charge/${amount}", bindings), "/charge/9223372036854775807"
        )
        copy = expand("${nested}", bindings)
        copy["values"].append(3)
        self.assertEqual(bindings["nested"]["values"], [1, 2])

    def test_unknown_binding_fails_instead_of_silently_masking(self):
        with self.assertRaises(KeyError):
            expand("${missing}", {})

    def test_paths_support_arrays(self):
        value = {"actions": [{"type": "qr_code_input_action"}]}
        assign(value, "actions.0.type", "web_view_user_input_action")
        self.assertEqual(field(value, "actions.0.type"), "web_view_user_input_action")

    def test_manifests_are_versioned_unique_and_have_assertions(self):
        scenarios = [
            s
            for f in ("scenarios.json", "recovery-scenarios.json")
            for s in load(ROOT / "conformance/fixtures" / f)
        ]
        self.assertEqual(len({s["name"] for s in scenarios}), len(scenarios))
        self.assertEqual(len(scenarios), 10)
        self.assertTrue(all(s["steps"] for s in scenarios))
