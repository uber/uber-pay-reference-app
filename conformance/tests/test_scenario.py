import unittest

from conformance.scenario import Scenario, assign, bundles, expand, field, load
from conformance.wire import Reply


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
        scenarios = [s for bundle in bundles() for s in load(bundle)]
        self.assertEqual(len({s["name"] for s in scenarios}), len(scenarios))
        self.assertTrue(scenarios)
        self.assertTrue(all(s["steps"] for s in scenarios))

    def test_browser_redirect_binding_checks_origin_state_and_unique_code(self):
        class Suite(unittest.TestCase):
            client_id, client_secret = "client", "synthetic"

            def uid(self):
                return "correlation"

        scenario = Scenario(Suite())
        step = {
            "expect": {
                "status": 302,
                "redirect": {
                    "uri": "http://127.0.0.1:9090/return",
                    "query": {"state": "${id}"},
                },
            },
            "bind_redirect": {"code": "code"},
        }
        reply = Reply(
            302,
            {"location": "http://127.0.0.1:9090/return?state=correlation&code=opaque"},
            b"",
        )
        scenario.assertions(reply, step)
        self.assertEqual(scenario.bindings["code"], "opaque")
        for location in (
            "http://127.0.0.1:9090/return?state=correlation&code=a&code=b",
            "http://127.0.0.1:9090/return?state=wrong&code=a",
            "https://invalid.example/return?state=correlation&code=a",
        ):
            with self.subTest(location=location), self.assertRaises(AssertionError):
                scenario.assertions(Reply(302, {"location": location}, b""), step)
