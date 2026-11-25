import copy
import unittest
from unittest.mock import patch

from conformance import contract, coverage, suite
from conformance.validation import expected_cases, mutations, request_schema, samples


class CoverageTest(unittest.TestCase):
    def qualify(self, observed, events, cases, **kwargs):
        return coverage.qualification(
            observed,
            events,
            cases,
            validation_observed=kwargs.pop("validation_observed", expected_cases()),
            **kwargs,
        )

    def evidence(self):
        names = {name for name in dir(suite.GatewaySuite) if name.startswith("test_")}
        cases = [{"name": "Runtime." + name, "status": "passed"} for name in names]
        observed = {
            (method.upper(), path, int(status))
            for path, item in contract.SPEC["paths"].items()
            for method, op in item.items()
            if method in ("get", "post")
            for status in op.get("responses", {})
            if status.isdecimal()
        } | {("GET", "/authorize", 200)}
        events = [
            {"name": name, "status": status, "error": None}
            for name, status in coverage.callback_pairs()
        ]
        return names, cases, observed, events

    def test_matrix_references_real_tests_and_every_public_operation(self):
        names, _, _, _ = self.evidence()
        coverage.integrity(names)

    def test_inbound_status_hits_cannot_hide_missing_callback_or_authorize(self):
        _, cases, observed, events = self.evidence()
        self.assertTrue(
            self.qualify(observed, events, cases, full_run=True)["qualified"]
        )
        for omitted in ("callback", "authorize"):
            current_events = [] if omitted == "callback" else events
            current_observed = (
                observed - {("GET", "/authorize", 200)}
                if omitted == "authorize"
                else observed
            )
            result = self.qualify(
                current_observed, current_events, cases, full_run=True
            )
            self.assertFalse(result["qualified"])
            self.assertFalse(result["responses"]["missing"])

    def test_missing_failed_skipped_and_filtered_evidence_never_qualifies(self):
        _, cases, observed, events = self.evidence()
        required = coverage.matrix()["requirements"][0]["tests"][0]
        for status in (None, "failed", "skipped"):
            current = [row for row in cases if row["name"] != "Runtime." + required]
            if status:
                current.append({"name": "Runtime." + required, "status": status})
            self.assertFalse(
                self.qualify(observed, events, current, full_run=True)["qualified"]
            )
        self.assertFalse(
            self.qualify(observed, events, cases, full_run=False)["qualified"]
        )

    def test_passing_test_names_without_validation_receipts_do_not_qualify(self):
        _, cases, observed, events = self.evidence()
        result = self.qualify(
            observed, events, cases, full_run=True, validation_observed=()
        )
        self.assertFalse(result["qualified"])
        self.assertEqual(result["request_validation"]["exercised"], 0)
        self.assertTrue(result["request_validation"]["missing"])

    def test_spec_drift_cannot_silently_leave_new_endpoint_or_callback_ungated(self):
        names, _, _, _ = self.evidence()
        for dimension in ("operation", "callback"):
            changed = copy.deepcopy(contract.SPEC)
            if dimension == "operation":
                changed["paths"]["/new-operation"] = {"get": {"responses": {"200": {}}}}
            else:
                callback = next(
                    iter(changed["components"]["callbacks"]["chargeCallback"].values())
                )
                callback["post"]["responses"]["502"] = {"description": "new response"}
            with (
                self.subTest(dimension=dimension),
                patch.object(coverage, "SPEC", changed),
                self.assertRaises(ValueError),
            ):
                coverage.integrity(names)

    def test_new_optional_request_field_requires_an_executable_input_case(self):
        names, _, _, _ = self.evidence()
        changed = copy.deepcopy(contract.SPEC)
        changed["paths"]["/charge"]["post"]["requestBody"]["content"][
            "application/json"
        ]["schema"]["properties"]["new_context"] = {"type": "string"}
        with (
            patch.object(contract, "SPEC", changed),
            patch.object(coverage, "SPEC", changed),
        ):
            result = coverage.field_report(names)
            self.assertIn(["POST", "/charge", "new_context"], result["missing"])

    def test_generated_mutations_are_unique_independently_invalid_and_nonempty(self):
        for profile, sample in samples().items():
            with self.subTest(profile=profile):
                cases = mutations(request_schema(profile), sample)
                self.assertTrue(cases)
                self.assertEqual(len(cases), len({case["id"] for case in cases}))
        fields = {
            case["id"]
            for case in mutations(request_schema("payout"), samples()["payout"])
        }
        self.assertIn("payee.address.post_code:null", fields)
        self.assertIn("payee.document.type:enum", fields)

    def test_int64_validation_reaches_fields_inside_anyof_array_items(self):
        schema = {"$ref": "#/components/schemas/PaymentDataArray"}
        contract.validate(
            schema,
            [
                {
                    "type": "point_conversion_payment_data",
                    "points_to_charge_e5": 2**63 - 1,
                }
            ],
        )
        with self.assertRaises(AssertionError):
            contract.validate(
                schema,
                [
                    {
                        "type": "point_conversion_payment_data",
                        "points_to_charge_e5": 2**63,
                    }
                ],
            )
