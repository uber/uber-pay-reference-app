import unittest
from conformance import contract


class ContractTest(unittest.TestCase):
    def test_malformed_upstream_requirement_normalized(self):
        self.assertEqual(
            contract.SPEC["components"]["schemas"]["PayoutCallbackState"]["required"],
            ["value"],
        )

    def test_callback_refund_terminal_vocabulary(self):
        contract.callback("updateRefund", {"status": "CONFIRMED"})
        with self.assertRaises(Exception):
            contract.callback("updateRefund", {"status": "SUCCEEDED"})

    def test_int64_exact_range(self):
        schema = {"$ref": "#/components/schemas/CurrencyAmount"}
        contract.validate(schema, {"value": 9007199254740993, "currency": "BRL"})
        with self.assertRaises(Exception):
            contract.validate(schema, {"value": 2**63, "currency": "BRL"})

    def test_bool_not_money(self):
        with self.assertRaises(Exception):
            contract.validate(
                {"$ref": "#/components/schemas/CurrencyAmount"},
                {"value": True, "currency": "BRL"},
            )

    def test_token_response_dispatched_by_grant(self):
        p = {
            "refresh_token": "synthetic",
            "token_type": "bearer",
            "authorization_details": [
                {
                    "type": "authorized_account",
                    "account_id": "a",
                    "fingerprint": "f",
                    "display_name": "Synthetic",
                }
            ],
        }
        contract.response("post", "/token", 200, p, grant="authorization_code")
        contract.response(
            "post",
            "/token",
            200,
            p,
            grant="urn:uberpay:params:oauth:grant-type:wallet-grant",
        )

    def test_capture_success_body_absent(self):
        contract.response("post", "/charge/{id}/capture", 200, None)
        with self.assertRaises(AssertionError):
            contract.response(
                "post", "/charge/{id}/capture", 200, {"state": "CONFIRMED"}
            )

    def test_every_callback_known(self):
        self.assertEqual(len(contract.SPEC["components"]["callbacks"]), 6)

    def test_formats_are_actually_enabled(self):
        for fmt, value in [
            ("date-time", "yesterday"),
            ("date-time", "2026-11-20T10:00:00"),
            ("uri", "not a uri"),
        ]:
            with self.subTest(fmt=fmt, value=value), self.assertRaises(Exception):
                contract.validate({"type": "string", "format": fmt}, value)

    def test_transient_5xx_does_not_allow_undeclared_4xx(self):
        contract.response("post", "/refund", 500, None)
        with self.assertRaises(AssertionError):
            contract.response("post", "/refund", 503, {"error": "unsafe detail"})
        with self.assertRaises(AssertionError):
            contract.response("post", "/refund", 418, None)
