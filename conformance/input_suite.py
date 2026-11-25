"""Systematic wire validation and optional-data preservation, shared by all runtimes."""

import hashlib
import json
from urllib.parse import parse_qs, urlencode, urlsplit

from . import contract
from .validation import (
    PROFILES,
    authorize_cases,
    case_payload,
    mutations,
    query_samples,
    request_schema,
    required_headers,
    samples,
)
from .wire import encode


class InputCoverageSuite:
    def input_authorize_query(self):
        return query_samples()["authorize"] | {
            "client_id": self.client_id,
            "state": self.uid(),
        }

    def input_receipt(self, profile, case):
        self.client.validation_observed.add(profile + "|" + case)

    def input_money_state(self):
        with self.db() as db:
            return db.execute(
                """SELECT account_id,currency,available_e5,balance_e5 FROM accounts
                WHERE client_id=%s ORDER BY account_id,currency""",
                (self.client_id,),
            ).fetchall()

    def input_authorization_code(self):
        from .suite import HiddenForm

        query = self.input_authorize_query()
        with self.db() as db:
            db.execute(
                "DELETE FROM public_buckets WHERE client_id=%s", (self.client_id,)
            )
        page = self.client.request(
            "GET", "/authorize?" + urlencode(query), signed=False
        )
        self.assertEqual(page.status, 200)
        form = HiddenForm()
        form.feed(page.raw.decode())
        self.assertTrue(form.fields)
        result = self.submit_action(form)
        self.assertEqual(result.status, 302)
        parameters = parse_qs(urlsplit(result.headers["location"]).query)
        self.assertEqual(parameters["state"], [query["state"]])
        return parameters["code"][0]

    def input_baseline(self, profile):
        data = samples()[profile]
        token, resource = None, None
        if "id" in data:
            data["id"] = self.uid()
        if profile == "charge_access_token":
            token, _ = self.access()
        elif profile in ("capture", "cancel"):
            original, response = self.charge(capture_method="MANUAL")
            resource = original["id"]
            data["psp_reference"] = response["psp_reference"]
        elif profile == "refund":
            original, response = self.charge(
                payment_data=[
                    {"type": "order_payment_data", "order_id": "synthetic-order"}
                ]
            )
            data.update(
                original_transaction_id=original["id"],
                original_merchant_reference=response["psp_reference"],
            )
        elif profile == "balance":
            token, _ = self.access()
        elif profile.startswith("token_"):
            data.update(client_id=self.client_id, client_secret=self.client_secret)
            if profile == "token_code":
                data["code"] = self.input_authorization_code()
            elif profile == "token_refresh":
                _, data["refresh_token"] = self.access()
        return data, token, resource

    def assert_input_profile(self, profile):
        method, template, _ = PROFILES[profile]
        data, token, resource = self.input_baseline(profile)
        target = template.replace("{id}", resource or "unused")
        before = self.input_money_state()
        cases = mutations(request_schema(profile), data)
        for case in cases:
            with self.subTest(profile=profile, case=case["id"]):
                bad = case_payload(data, case)
                reply = self.client.request(
                    method, target, raw=encode(bad), token=token, idem=self.uid()
                )
                self.assert_response(reply, template, 400)
                self.input_receipt(profile, case["id"])
        money_field = "amount_to_capture" if profile == "capture" else "amount"
        if money_field in data:
            for amount in (0, -1):
                with self.subTest(profile=profile, positive_money=amount):
                    bad = json.loads(json.dumps(data))
                    bad[money_field]["value"] = amount
                    self.assert_response(
                        self.client.request(
                            method, target, bad, token=token, idem=self.uid()
                        ),
                        template,
                        400,
                    )
                    self.input_receipt(profile, "positive_money:" + str(amount))
        # Required headers are independently omitted/blanked per endpoint.
        for name in required_headers(profile):
            auth = name.lower() in ("date", "digest", "signature")
            expected = (
                401
                if auth and "401" in contract.operation(method, template)["responses"]
                else 400
            )
            for kind in ("missing", "blank"):
                with self.subTest(profile=profile, header=name, kind=kind):
                    self.assert_response(
                        self.client.request(
                            method,
                            target,
                            data,
                            token=token,
                            idem=self.uid(),
                            omit_headers=(name,) if kind == "missing" else (),
                            headers={name: ""} if kind == "blank" else {},
                        ),
                        template,
                        expected,
                    )
                    self.input_receipt(profile, "header_" + kind + ":" + name)
        self.assertEqual(
            self.input_money_state(), before, "Invalid inputs must not move funds"
        )
        # A valid control must succeed with the same context after all mutations.
        positive = self.client.request(
            method, target, data, token=token, idem=self.uid()
        )
        self.assert_response(
            positive,
            template,
            201 if profile == "refund" else 200,
            grant=data.get("grant_type"),
        )
        self.input_receipt(profile, "control")
        if resource:
            self.assertEqual(
                self.lookup(resource)["status"],
                "CONFIRMED" if profile == "capture" else "REJECTED",
            )

    def test_validation_positive_boundaries(self):
        for size, expected in ((256, 200), (257, 400)):
            data = self.fixture("charge", description="é" * size)
            self.assert_response(
                self.client.request("POST", "/charge", data, idem=self.uid()),
                "/charge",
                expected,
            )
            self.input_receipt("boundaries", "description:" + str(size))
        for amount in (1, 2**63 - 1):
            data = self.fixture("charge", amount={"value": amount, "currency": "BRL"})
            self.assert_response(
                self.client.request("POST", "/charge", data, idem=self.uid()),
                "/charge",
                200,
            )
            self.assertEqual(self.lookup(data["id"])["amount"]["value"], amount)
            self.input_receipt("boundaries", "money:" + str(amount))

    def test_validation_lookup_parameters(self):
        data, result = self.charge()
        for name, query, expected in [
            ("required", {}, 400),
            ("empty", {"id": ""}, 400),
            ("not_found", {"id": self.uid()}, 404),
            (
                "reference_mismatch",
                {"id": data["id"], "merchant_reference": "not-the-reference"},
                404,
            ),
        ]:
            with self.subTest(query=query):
                self.assert_response(
                    self.client.request("GET", "/find-transaction?" + urlencode(query)),
                    "/find-transaction",
                    expected,
                    "GET",
                )
                self.input_receipt("lookup", name)
        reply = self.client.request(
            "GET",
            "/find-transaction?"
            + urlencode(
                {
                    "id": data["id"],
                    "merchant_reference": result["psp_reference"],
                }
            ),
        )
        self.assertEqual(
            self.assert_response(reply, "/find-transaction", 200, "GET")["id"],
            data["id"],
        )
        self.input_receipt("lookup", "control")

    def test_validation_authorize_parameters(self):
        query = self.input_authorize_query()
        cases = authorize_cases(query)
        with self.db() as db:
            db.execute(
                "DELETE FROM public_buckets WHERE client_id=%s", (self.client_id,)
            )
        for name, bad in cases:
            with self.subTest(case=name):
                reply = self.client.request(
                    "GET", "/authorize?" + urlencode(bad), signed=False
                )
                trusted = (
                    bad.get("client_id") == self.client_id
                    and bad.get("redirect_uri") == query["redirect_uri"]
                )
                self.assertEqual(reply.status, 302 if trusted else 400, reply.raw)
                if trusted:
                    params = parse_qs(
                        urlsplit(reply.headers["location"]).query,
                        keep_blank_values=True,
                    )
                    expected = (
                        "invalid_scope"
                        if name.startswith("scope:")
                        else "unsupported_response_type"
                        if name == "response_type:enum"
                        else "invalid_request"
                    )
                    self.assertEqual(params.get("error"), [expected])
                    self.assertEqual(
                        params.get("state"), [bad["state"]] if "state" in bad else None
                    )
                self.input_receipt("authorize", name)
        self.input_authorization_code()
        self.input_receipt("authorize", "control")

    def test_optional_processor_fields_are_preserved(self):
        with self.http_processor() as processor:
            data = samples()["charge"]
            data["id"] = self.uid()
            reply = self.client.request("POST", "/charge", data, idem=self.uid())
            response = self.assert_response(reply, "/charge", 200)
            requests = [("charge", data, self.provider_entry(processor, data["id"]))]
            refund = self.refund_request(
                data,
                response,
                description="Partial item refund — sintético",
                order_refund_data=json.dumps(
                    {"order_id": "synthetic-order", "items": ["synthetic-item"]}
                ),
            )
            self.assert_response(
                self.client.request("POST", "/refund", refund, idem=self.uid()),
                "/refund",
                201,
            )
            requests.append(
                (
                    "initRefund",
                    refund,
                    self.provider_entry(processor, data["id"], "initRefund"),
                )
            )
            for document_type in ("RUT", "CUIT"):
                payout = samples()["payout"]
                payout["id"] = self.uid()
                payout["payee"]["document"]["type"] = document_type
                self.assert_response(
                    self.client.request("POST", "/payout", payout, idem=self.uid()),
                    "/payout",
                    200,
                )
                requests.append(
                    (
                        "payout",
                        payout,
                        self.provider_entry(processor, payout["id"], "payout"),
                    )
                )
            for operation, payload, entry in requests:
                command = {
                    "operation": operation,
                    "request": payload,
                    "resource": entry["resource"],
                }
                expected = hashlib.sha256(
                    json.dumps(
                        command,
                        sort_keys=True,
                        separators=(",", ":"),
                        ensure_ascii=False,
                    ).encode()
                ).hexdigest()
                self.assertEqual(entry["command_hash"], expected, operation)
                self.assertEqual(entry["effects"], 1)
            state = self.client.control("GET", "state")
            self.assertNotIn(b"synthetic-document", state.raw)
            self.assertNotIn(b"demo-merchant-secret", state.raw)


def install_inputs(cls):
    for name in PROFILES:

        def test(self, profile=name):
            self.assert_input_profile(profile)

        setattr(cls, f"test_validation_{name}", test)
