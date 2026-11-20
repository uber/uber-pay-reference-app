"""Independent wire scenarios with namespace-scoped local fault fixtures; no gateway imports."""

import json
import time
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from email.utils import formatdate
from html.parser import HTMLParser
from urllib.parse import urlencode, urlsplit, parse_qs, quote

from .review_suite import ReviewRegressionSuite
from .completion_suite import CompletionSuite
from .owner_suite import OwnerSuite
from . import contract
from .wire import eventually


class HiddenForm(HTMLParser):
    def __init__(self):
        super().__init__()
        self.fields = {}
        self.action = "/demo/consent"

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "form":
            self.action = attrs.get("action", self.action)
        if tag == "input" and attrs.get("type") == "hidden":
            self.fields[attrs["name"]] = attrs.get("value", "")


class GatewaySuite(
    OwnerSuite, CompletionSuite, ReviewRegressionSuite, unittest.TestCase
):
    client = mock = process = None
    client_id = "demo-client"
    client_secret = "demo-secret"

    def uid(self):
        return uuid.uuid4().hex

    def fixture(self, kind, **changes):
        text = (contract.ROOT / f"conformance/fixtures/{kind}.json").read_text()
        data = json.loads(
            text.replace("$id", self.uid()).replace(
                "$now", datetime.now(timezone.utc).isoformat()
            )
        )
        data.update(changes)
        return data

    def assert_response(self, reply, path, expected, method="post", grant=None):
        self.assertEqual(reply.status, expected, (path, reply.status, reply.raw[:500]))
        contract.response(method, path, reply.status, reply.body, grant=grant)
        return reply.body

    def charge(self, **changes):
        data = self.fixture("charge", **changes)
        reply = self.client.request("POST", "/charge", data, idem=self.uid())
        body = self.assert_response(reply, "/charge", 200)
        return data, body

    def lookup(self, transaction_id):
        r = self.client.request(
            "GET", "/find-transaction?id=" + quote(transaction_id, safe="")
        )
        return self.assert_response(r, "/find-transaction", 200, "get")

    def access(self, scope="charge refund get_balance offline_access"):
        grant = "urn:uberpay:params:oauth:grant-type:wallet-grant"
        request = {
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "grant_type": grant,
            "scope": scope,
            "country_iso2": "BR",
            "funding_method": "DEMO_SYNC",
            "wallet_reference_data": {"payment_identifier": "demo-account"},
        }
        first = self.assert_response(
            self.client.request("POST", "/token", request), "/token", 200, grant=grant
        )
        request = {
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "grant_type": "refresh_token",
            "refresh_token": first["refresh_token"],
        }
        second = self.assert_response(
            self.client.request("POST", "/token", request),
            "/token",
            200,
            grant="refresh_token",
        )
        return second["access_token"], first["refresh_token"]

    def accepted(self, name, id):
        return eventually(
            lambda: self.mock.matching(name, id), description=f"{name}/{id}"
        )

    def test_health(self):
        self.assertEqual(
            self.client.request("GET", "/health", signed=False, outside=True).status,
            200,
        )

    def test_sync_charge_lookup(self):
        data, body = self.charge()
        self.assertEqual(body["state"]["value"], "CONFIRMED")
        found = self.lookup(data["id"])
        self.assertEqual(found["amount"], data["amount"])
        self.assertEqual(found["merchant_reference"], body["psp_reference"])

    def test_idempotency_replays_response(self):
        data = self.fixture("charge")
        key = self.uid()
        one = self.client.request("POST", "/charge", data, idem=key)
        two = self.client.request("POST", "/charge", data, idem=key)
        three = self.client.request("POST", "/charge", data, idem=self.uid())
        self.assert_response(one, "/charge", 200)
        self.assertEqual((one.status, one.raw), (two.status, two.raw))
        self.assertEqual(one.body, three.body)

    def test_conflicting_idempotency(self):
        data = self.fixture("charge")
        key = self.uid()
        self.assert_response(
            self.client.request("POST", "/charge", data, idem=key), "/charge", 200
        )
        data["amount"]["value"] += 1
        self.assert_response(
            self.client.request("POST", "/charge", data, idem=key), "/charge", 400
        )

    def test_concurrent_duplicate_has_one_effect(self):
        token, _ = self.access()
        balance = {"account_id": "demo-account", "funding_method": "DEMO_SYNC"}
        before = self.assert_response(
            self.client.request("POST", "/balance", balance, token=token),
            "/balance",
            200,
        )
        data = self.fixture("charge")
        key = self.uid()
        with ThreadPoolExecutor(max_workers=8) as pool:
            replies = list(
                pool.map(
                    lambda _: self.client.request("POST", "/charge", data, idem=key),
                    range(8),
                )
            )
        self.assertTrue(
            all(r.status == 200 for r in replies), [(r.status, r.raw) for r in replies]
        )
        self.assertEqual(len({r.raw for r in replies}), 1)
        after = self.assert_response(
            self.client.request("POST", "/balance", balance, token=token),
            "/balance",
            200,
        )
        self.assertEqual(
            before["available_balance"]["value"] - after["available_balance"]["value"],
            data["amount"]["value"],
        )

    def test_missing_idempotency(self):
        self.assert_response(
            self.client.request("POST", "/charge", self.fixture("charge")),
            "/charge",
            400,
        )

    def test_exact_e5_above_safe_integer(self):
        data, body = self.charge(amount={"value": 9007199254740993, "currency": "BRL"})
        self.assertEqual(self.lookup(data["id"])["amount"]["value"], 9007199254740993)

    def test_invalid_json(self):
        self.assert_response(
            self.client.request("POST", "/charge", raw=b'{"bad":', idem=self.uid()),
            "/charge",
            400,
        )

    def test_tampered_digest(self):
        r = self.client.request(
            "POST",
            "/charge",
            self.fixture("charge"),
            idem=self.uid(),
            headers={"Digest": "SHA-256=Zm9v"},
        )
        self.assert_response(r, "/charge", 401)

    def test_signature_missing(self):
        r = self.client.request(
            "POST", "/charge", self.fixture("charge"), idem=self.uid(), signed=False
        )
        self.assert_response(r, "/charge", 401)

    def test_signature_malformed_is_bounded(self):
        r = self.client.request(
            "POST",
            "/charge",
            self.fixture("charge"),
            idem=self.uid(),
            headers={"Signature": 'keyId="unterminated'},
        )
        self.assert_response(r, "/charge", 401)

    def test_signature_wrong_target(self):
        r = self.client.request(
            "POST",
            "/charge",
            self.fixture("charge"),
            idem=self.uid(),
            signature_target="/wrong",
        )
        self.assert_response(r, "/charge", 401)

    def test_signature_expired(self):
        r = self.client.request(
            "POST",
            "/charge",
            self.fixture("charge"),
            idem=self.uid(),
            date=formatdate(time.time() - 1000, usegmt=True),
        )
        self.assert_response(r, "/charge", 401)

    def test_signature_future(self):
        r = self.client.request(
            "POST",
            "/charge",
            self.fixture("charge"),
            idem=self.uid(),
            date=formatdate(time.time() + 1000, usegmt=True),
        )
        self.assert_response(r, "/charge", 401)

    def test_manual_capture(self):
        data, _ = self.charge(capture_method="MANUAL")
        self.assertEqual(self.lookup(data["id"])["status"], "AUTHORIZED")
        self.assert_response(
            self.client.request(
                "POST",
                f"/charge/{data['id']}/capture",
                {"amount_to_capture": data["amount"]},
                idem=self.uid(),
            ),
            "/charge/{id}/capture",
            200,
        )
        self.assertEqual(self.lookup(data["id"])["status"], "CONFIRMED")

    def test_manual_cancel(self):
        data, _ = self.charge(capture_method="MANUAL")
        self.assert_response(
            self.client.request(
                "POST", f"/charge/{data['id']}/cancel", {}, idem=self.uid()
            ),
            "/charge/{id}/cancel",
            200,
        )
        self.assert_response(
            self.client.request(
                "POST",
                f"/charge/{data['id']}/capture",
                {"amount_to_capture": data["amount"]},
                idem=self.uid(),
            ),
            "/charge/{id}/capture",
            409,
        )

    def test_capture_cancel_race(self):
        data, _ = self.charge(capture_method="MANUAL")
        id = data["id"]
        with ThreadPoolExecutor(max_workers=2) as pool:
            capture = pool.submit(
                self.client.request,
                "POST",
                f"/charge/{id}/capture",
                {"amount_to_capture": data["amount"]},
                idem=self.uid(),
            )
            cancel = pool.submit(
                self.client.request, "POST", f"/charge/{id}/cancel", {}, idem=self.uid()
            )
            results = [capture.result(), cancel.result()]
        self.assertEqual(sorted(r.status for r in results), [200, 409])

    def test_partial_capture_releases_remainder(self):
        data, _ = self.charge(capture_method="MANUAL")
        r = self.client.request(
            "POST",
            f"/charge/{data['id']}/capture",
            {"amount_to_capture": {"value": 1, "currency": "BRL"}},
            idem=self.uid(),
        )
        self.assert_response(r, "/charge/{id}/capture", 200)
        found = self.lookup(data["id"])
        self.assertEqual(found["status"], "CONFIRMED")
        self.assertEqual(found["amount"], {"value": 1, "currency": "BRL"})
        with self.db() as db:
            self.assertEqual(
                db.execute(
                    "SELECT authorized_e5,amount_e5 FROM transactions WHERE client_id=%s AND kind='charge' AND id=%s",
                    (self.client_id, data["id"]),
                ).fetchone(),
                (data["amount"]["value"], 1),
            )
        self.assert_response(
            self.client.request(
                "POST",
                f"/charge/{data['id']}/capture",
                {"amount_to_capture": {"value": 1, "currency": "BRL"}},
                idem=self.uid(),
            ),
            "/charge/{id}/capture",
            409,
        )

    def test_expired_authorization(self):
        data, _ = self.charge(capture_method="MANUAL")
        self.assertEqual(
            self.client.control("POST", "expire", {"id": data["id"]}).status, 200
        )
        self.assert_response(
            self.client.request(
                "POST",
                f"/charge/{data['id']}/capture",
                {"amount_to_capture": data["amount"]},
                idem=self.uid(),
            ),
            "/charge/{id}/capture",
            409,
        )

    def test_manual_pending_rejected(self):
        data = self.fixture(
            "charge", capture_method="MANUAL", funding_method="DEMO_PENDING"
        )
        self.assert_response(
            self.client.request("POST", "/charge", data, idem=self.uid()),
            "/charge",
            400,
        )

    def test_charge_rejected(self):
        _, body = self.charge(funding_method="DEMO_REJECT")
        self.assertEqual(body["state"]["value"], "REJECTED")

    def test_charge_pending_callback(self):
        data, body = self.charge(funding_method="DEMO_PENDING")
        self.assertEqual(body["state"]["value"], "PENDING")
        event = self.accepted("chargeCallback", data["id"])[0]
        self.assertEqual(event["payload"]["state"]["value"], "CONFIRMED")
        self.assertEqual(self.lookup(data["id"])["status"], "CONFIRMED")

    def test_charge_pending_rejected_callback(self):
        data, _ = self.charge(funding_method="DEMO_PENDING_REJECT")
        self.assertEqual(
            self.accepted("chargeCallback", data["id"])[0]["payload"]["state"]["value"],
            "REJECTED",
        )

    def test_webview_and_qr_action(self):
        for method in ("DEMO_WEBVIEW", "DEMO_QR"):
            data, body = self.charge(funding_method=method)
            self.assertEqual(body["state"]["value"], "PENDING")
            self.submit_action(self.action_page(body))
            self.accepted("chargeCallback", data["id"])

    def test_callback_retry_keeps_identity(self):
        data = self.fixture("charge", funding_method="DEMO_PENDING")
        self.mock.fail_next("chargeCallback", data["id"], 503, 429)
        self.assert_response(
            self.client.request("POST", "/charge", data, idem=self.uid()),
            "/charge",
            200,
        )
        self.accepted("chargeCallback", data["id"])
        events = self.mock.matching("chargeCallback", data["id"], accepted=False)
        self.assertEqual([e["status"] for e in events[:3]], [503, 429, 200])
        self.assertEqual(len({e["idempotency_key"] for e in events}), 1)

    def test_callback_409_requires_reconciliation(self):
        data = self.fixture("charge", funding_method="DEMO_PENDING")
        self.mock.fail_next("chargeCallback", data["id"], 409)
        self.assert_response(
            self.client.request("POST", "/charge", data, idem=self.uid()),
            "/charge",
            200,
        )
        eventually(
            lambda: self.mock.matching("chargeCallback", data["id"], accepted=False)
        )
        time.sleep(0.4)
        self.assertFalse(self.mock.matching("chargeCallback", data["id"]))
        self.assertEqual(self.lookup(data["id"])["status"], "CONFIRMED")

    def callback_job(self, transaction_id, status):
        reply = self.client.control("GET", "state")
        self.assertEqual(reply.status, 200)
        return next(
            (
                job
                for job in reply.body["jobs"]
                if job["tx_id"] == transaction_id
                and job["job_type"] == "CALLBACK"
                and job["status"] == status
            ),
            None,
        )

    def test_permanent_callback_failure_retains_payment(self):
        data = self.fixture("charge", funding_method="DEMO_PENDING")
        self.mock.fail_next("chargeCallback", data["id"], 400)
        self.assert_response(
            self.client.request("POST", "/charge", data, idem=self.uid()),
            "/charge",
            200,
        )
        eventually(lambda: self.callback_job(data["id"], "FAILED"))
        self.assertEqual(self.lookup(data["id"])["status"], "CONFIRMED")
        self.assertEqual(
            len(self.mock.matching("chargeCallback", data["id"], accepted=False)), 1
        )

    def test_callback_retry_exhaustion_retains_work(self):
        data = self.fixture("charge", funding_method="DEMO_PENDING")
        self.mock.fail_next("chargeCallback", data["id"], *([503] * 10))
        self.assert_response(
            self.client.request("POST", "/charge", data, idem=self.uid()),
            "/charge",
            200,
        )
        job = eventually(lambda: self.callback_job(data["id"], "FAILED"))
        self.assertEqual(int(job["attempts"]), 5)
        self.assertEqual(
            len(self.mock.matching("chargeCallback", data["id"], accepted=False)), 5
        )
        self.assertEqual(self.lookup(data["id"])["status"], "CONFIRMED")

    def test_refund_partial_and_over_refund(self):
        original, body = self.charge()

        def refund(amount):
            return {
                "id": self.uid(),
                "original_transaction_id": original["id"],
                "original_merchant_reference": body["psp_reference"],
                "amount": {"value": amount, "currency": "BRL"},
            }

        data = refund(500000)
        result = self.assert_response(
            self.client.request("POST", "/refund", data, idem=self.uid()),
            "/refund",
            201,
        )
        self.assertEqual(result["status"], "CONFIRMED")
        self.assertEqual(self.lookup(data["id"])["amount"]["value"], 500000)
        self.assert_response(
            self.client.request("POST", "/refund", refund(500001), idem=self.uid()),
            "/refund",
            400,
        )

    def test_refund_pending_callback(self):
        original, body = self.charge(funding_method="DEMO_PENDING")
        self.accepted("chargeCallback", original["id"])
        data = {
            "id": self.uid(),
            "original_transaction_id": original["id"],
            "original_merchant_reference": body["psp_reference"],
            "amount": original["amount"],
        }
        out = self.assert_response(
            self.client.request("POST", "/refund", data, idem=self.uid()),
            "/refund",
            201,
        )
        self.assertEqual(out["status"], "PENDING")
        self.assertEqual(
            self.accepted("updateRefund", data["id"])[0]["payload"]["status"],
            "CONFIRMED",
        )

    def test_payout_sync_and_pending(self):
        for method in ["DEMO_SYNC", "DEMO_PENDING", "DEMO_PENDING_REJECT"]:
            with self.subTest(method=method):
                data = self.fixture("payout", funding_method=method)
                body = self.assert_response(
                    self.client.request("POST", "/payout", data, idem=self.uid()),
                    "/payout",
                    200,
                )
                if method != "DEMO_SYNC":
                    event = self.accepted("payoutCallback", data["id"])[0]
                    self.assertEqual(
                        event["payload"]["state"]["value"],
                        "REJECTED" if method.endswith("REJECT") else "CONFIRMED",
                    )
                else:
                    self.assertEqual(body["state"]["value"], "CONFIRMED")

    def test_wallet_grant_and_scoped_balance(self):
        token, refresh = self.access("get_balance")
        self.assert_response(
            self.client.request(
                "POST",
                "/balance",
                {"account_id": "demo-account", "funding_method": "DEMO_SYNC"},
                token=token,
            ),
            "/balance",
            200,
        )
        self.assert_response(
            self.client.request(
                "POST",
                "/balance",
                {"account_id": "demo-account", "funding_method": "DEMO_SYNC"},
                token=refresh,
            ),
            "/balance",
            401,
        )
        self.assert_response(
            self.client.request(
                "POST",
                "/balance",
                {"account_id": "demo-account", "funding_method": "DEMO_SYNC"},
            ),
            "/balance",
            401,
        )

    def test_balance_wrong_scope(self):
        token, _ = self.access("charge")
        self.assert_response(
            self.client.request(
                "POST",
                "/balance",
                {"account_id": "demo-account", "funding_method": "DEMO_SYNC"},
                token=token,
            ),
            "/balance",
            401,
        )

    def test_authorize_code_exchange_single_use(self):
        details = [
            {
                "type": "authorization_detail",
                "funding_method": "DEMO_SYNC",
                "country_iso2": "BR",
                "locale": "en-US",
            }
        ]
        params = {
            "client_id": self.client_id,
            "response_type": "code",
            "redirect_uri": "http://127.0.0.1:9090/return",
            "scope": "charge get_balance",
            "state": "opaque-state",
            "authorization_details": json.dumps(details),
        }
        page = self.client.request(
            "GET", "/authorize?" + urlencode(params), signed=False
        )
        self.assertEqual(page.status, 200, page.raw)
        form = HiddenForm()
        form.feed(page.raw.decode())
        self.assertTrue(form.fields, "consent form must contain CSRF-bound fields")
        form.fields["decision"] = "approve"
        path = urlsplit(form.action).path
        consent = self.client.request(
            "POST",
            path,
            raw=urlencode(form.fields).encode(),
            signed=False,
            outside=True,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        self.assertIn(consent.status, (302, 303), consent.raw)
        redirect = parse_qs(urlsplit(consent.headers["location"]).query)
        self.assertEqual(redirect["state"], ["opaque-state"])
        payload = {
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "grant_type": "authorization_code",
            "code": redirect["code"][0],
            "redirect_uri": params["redirect_uri"],
        }
        issued = self.assert_response(
            self.client.request("POST", "/token", payload),
            "/token",
            200,
            grant="authorization_code",
        )
        refresh_request = dict(
            client_id=self.client_id,
            client_secret=self.client_secret,
            grant_type="refresh_token",
            refresh_token=issued["refresh_token"],
        )
        access = self.assert_response(
            self.client.request("POST", "/token", refresh_request),
            "/token",
            200,
            grant="refresh_token",
        )["access_token"]
        self.assert_response(
            self.client.request("POST", "/token", payload), "/token", 400
        )
        self.assert_response(
            self.client.request("POST", "/token", refresh_request), "/token", 400
        )
        self.assert_response(
            self.client.request(
                "POST",
                "/balance",
                {"account_id": "demo-account", "funding_method": "DEMO_SYNC"},
                token=access,
            ),
            "/balance",
            401,
        )

    def test_authorize_redirect_rejected(self):
        page = self.client.request(
            "GET",
            "/authorize?"
            + urlencode(
                {
                    "client_id": self.client_id,
                    "response_type": "code",
                    "redirect_uri": "https://attacker.invalid/steal",
                    "scope": "charge",
                    "state": "x",
                    "authorization_details": "[]",
                }
            ),
            signed=False,
        )
        self.assertEqual(page.status, 400)
        self.assertNotIn("location", page.headers)

    def test_access_authorization_binds_account(self):
        token, _ = self.access("charge")
        data = self.fixture(
            "charge",
            authorization={
                "type": "access_token_authorization",
                "account_id": "other-account",
            },
        )
        self.assert_response(
            self.client.request("POST", "/charge", data, idem=self.uid(), token=token),
            "/charge",
            401,
        )

    def test_all_six_callback_contracts(self):
        payloads = {
            "chargeCallback": {
                "state": {"value": "CONFIRMED"},
                "psp_reference": "synthetic-ref",
            },
            "payoutCallback": {
                "state": {"value": "CONFIRMED"},
                "psp_reference": "synthetic-ref",
            },
            "updateRefund": {
                "status": "CONFIRMED",
                "merchant_reference": "synthetic-ref",
            },
            "confirmDeposit": {"merchant_reference": "synthetic-ref"},
            "cancelDeposit": {
                "cancel_reason": "USER_CANCELLED",
                "merchant_reference": "synthetic-ref",
            },
            "updateDeposit": {
                "merchant_reference": "synthetic-ref",
                "amount": {"value": 123456, "currency": "BRL"},
            },
        }
        for name, payload in payloads.items():
            with self.subTest(callback=name):
                id = self.uid()
                r = self.client.control(
                    "POST", "callback", {"name": name, "id": id, "payload": payload}
                )
                self.assertIn(r.status, (200, 202), r.raw)
                self.assertEqual(self.accepted(name, id)[0]["payload"], payload)

    def test_restart_pending_jobs(self):
        if not self.process:
            self.fail("Restart test requires a process-managed gateway")
        data, response = self.charge(funding_method="DEMO_WEBVIEW")
        self.process.restart(hard=True)
        self.assertEqual(self.submit_action(self.action_page(response)).status, 302)
        self.accepted("chargeCallback", data["id"])

    def test_shared_scenario_manifests(self):
        from .scenario import Scenario, load

        for file in ["scenarios.json", "recovery-scenarios.json"]:
            for scenario in load(contract.ROOT / "conformance/fixtures" / file):
                with self.subTest(scenario=scenario["name"]):
                    Scenario(self).run(scenario)

    def test_test_controls_require_secret(self):
        response = self.client.request(
            "GET", "/_test/state", signed=False, outside=True
        )
        self.assertIn(response.status, (401, 403, 404))


def invalid_test(case):
    def test(self):
        payload = self.fixture("charge")
        path = (case.get("delete") or case["set"]).split(".")
        node = payload
        for key in path[:-1]:
            node = node[key]
        if "delete" in case:
            node.pop(path[-1])
        else:
            node[path[-1]] = case["value"]
        self.assert_response(
            self.client.request("POST", "/charge", payload, idem=self.uid()),
            "/charge",
            400,
        )

    return test


for _case in json.loads(
    (contract.ROOT / "conformance/fixtures/invalid-charge.json").read_text()
):
    setattr(GatewaySuite, "test_invalid_" + _case["name"], invalid_test(_case))
