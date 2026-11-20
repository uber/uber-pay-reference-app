"""Independent regressions for PR1-review; faults scoped to a disposable client namespace."""

import json
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlencode, urlsplit

import psycopg

from . import contract


class ReviewRegressionSuite:
    def db(self):
        # Runner accepts only a disposable local database. Never ALTER ROLE or
        # terminate arbitrary database sessions to inject a per-client fault.
        if urlsplit(self.database_url).hostname not in {
            "localhost",
            "127.0.0.1",
            "::1",
            "db",
            "postgres",
        } or not self.client_id.startswith("test-"):
            raise RuntimeError(
                "Fault fixtures require a disposable local test namespace"
            )
        return psycopg.connect(self.database_url, autocommit=True)

    def refund_request(self, original, response, **extra):
        return (
            dict(
                id=self.uid(),
                original_transaction_id=original["id"],
                original_merchant_reference=response["psp_reference"],
                amount=original["amount"],
            )
            | extra
        )

    def action_page(self, response):
        from .suite import HiddenForm

        action = response["actions"][0]
        target = action.get("url") or "/demo/action?token=" + action[
            "code"
        ].removeprefix("local-qr:")
        parts = urlsplit(target)
        page = self.client.request(
            "GET", parts.path + "?" + parts.query, signed=False, outside=True
        )
        self.assertEqual(page.status, 200, page.raw)
        form = HiddenForm()
        form.feed(page.raw.decode())
        self.assertTrue(form.fields)
        return form

    def submit_action(self, form, decision="approve"):
        return self.client.request(
            "POST",
            form.action,
            raw=urlencode(dict(form.fields, decision=decision)).encode(),
            signed=False,
            outside=True,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )

    def test_review_account_examples_do_not_require_undefined_name(self):
        data = self.fixture("charge")
        del data["source"]["name"]
        del data["destination"]["name"]
        self.assert_response(
            self.client.request("POST", "/charge", data, idem=self.uid()),
            "/charge",
            200,
        )
        payout = self.fixture("payout")
        del payout["payer"]["name"]
        self.assert_response(
            self.client.request("POST", "/payout", payout, idem=self.uid()),
            "/payout",
            200,
        )

    def test_review_real_actions_approve_deny_reuse(self):
        for funding in ("DEMO_WEBVIEW", "DEMO_QR"):
            for decision, state in [("approve", "CONFIRMED"), ("deny", "REJECTED")]:
                with self.subTest(funding=funding, decision=decision):
                    data, response = self.charge(funding_method=funding)
                    form = self.action_page(response)
                    result = self.submit_action(form, decision)
                    self.assertEqual(result.status, 302, result.raw)
                    self.assertEqual(
                        result.headers["location"], "http://127.0.0.1:9090/return"
                    )
                    self.assertEqual(self.submit_action(form, decision).status, 400)
                    event = self.accepted("chargeCallback", data["id"])[0]
                    self.assertEqual(event["payload"]["state"]["value"], state)
                    if decision == "deny":
                        self.assertEqual(
                            event["payload"]["state"]["detail"], "USER_CANCELLED"
                        )

    def test_review_forged_action_does_not_change_payment(self):
        data, _ = self.charge(funding_method="DEMO_WEBVIEW")
        r = self.client.request(
            "GET", "/demo/action?id=" + data["id"], signed=False, outside=True
        )
        self.assertIn(r.status, (400, 404))
        plain, _ = self.charge()
        r = self.client.request(
            "GET", "/demo/action?id=" + plain["id"], signed=False, outside=True
        )
        self.assertIn(r.status, (400, 404))
        self.assertEqual(self.lookup(plain["id"])["status"], "CONFIRMED")

    def test_review_action_expiry_releases_hold_and_notifies(self):
        # Test expiry with a short lifetime only here. A global short TTL makes
        # unrelated browser/restart tests depend on JVM boot time and runner load.
        previous = self.process.environment.get("ACTION_TTL_MS")
        try:
            self.process.environment["ACTION_TTL_MS"] = "2500"
            self.process.restart(hard=True)
            data, response = self.charge(funding_method="DEMO_QR")
            form = self.action_page(response)
            event = self.accepted("chargeCallback", data["id"])[0]
            self.assertEqual(
                event["payload"]["state"],
                {"value": "REJECTED", "detail": "MERCHANT_EXPIRED"},
            )
            self.assertEqual(self.submit_action(form).status, 400)
        finally:
            if previous is None:
                self.process.environment.pop("ACTION_TTL_MS", None)
            else:
                self.process.environment["ACTION_TTL_MS"] = previous
            self.process.restart(hard=True)

    def test_review_unregistered_return_url_never_redirected(self):
        data, response = self.charge(
            funding_method="DEMO_WEBVIEW", return_url="https://attacker.invalid/"
        )
        result = self.submit_action(self.action_page(response))
        self.assertEqual(result.status, 200)
        self.assertNotIn("location", result.headers)
        self.accepted("chargeCallback", data["id"])

    def test_review_callback_401_refreshes_same_delivery_identity(self):
        data = self.fixture("charge", funding_method="DEMO_PENDING")
        self.mock.fail_next("chargeCallback", data["id"], 401)
        self.assert_response(
            self.client.request("POST", "/charge", data, idem=self.uid()),
            "/charge",
            200,
        )
        self.accepted("chargeCallback", data["id"])
        events = self.mock.matching("chargeCallback", data["id"], accepted=False)
        self.assertEqual([e["status"] for e in events], [401, 200])
        self.assertEqual(len({e["idempotency_key"] for e in events}), 1)

    def test_review_callback_revoked_token_not_reused(self):
        data, _ = self.charge(funding_method="DEMO_PENDING")
        self.accepted("chargeCallback", data["id"])
        with self.mock.lock:
            self.mock.tokens.clear()
        for _ in range(2):
            data, _ = self.charge(funding_method="DEMO_PENDING")
            self.accepted("chargeCallback", data["id"])

    def test_review_rejected_callbacks_explain_reason(self):
        for kind, name in [("charge", "chargeCallback"), ("payout", "payoutCallback")]:
            data = self.fixture(kind, funding_method="DEMO_PENDING_REJECT")
            self.assert_response(
                self.client.request("POST", "/" + kind, data, idem=self.uid()),
                "/" + kind,
                200,
            )
            self.assertEqual(
                self.accepted(name, data["id"])[0]["payload"]["state"],
                {"value": "REJECTED", "detail": "MERCHANT_REJECTED"},
            )

    def test_review_bearer_ownership_for_manual_and_refund(self):
        token, _ = self.access("charge refund")
        # Change only this synthetic token's account; never create a publicly usable identity.
        with self.db() as db:
            import hashlib

            db.execute(
                "UPDATE tokens SET account_id='other-account' WHERE client_id=%s AND token_hash=%s",
                (self.client_id, hashlib.sha256(token.encode()).hexdigest()),
            )
        for op in ("capture", "cancel", "refund"):
            data, response = self.charge(
                capture_method="AUTOMATIC" if op == "refund" else "MANUAL"
            )
            path = "/refund" if op == "refund" else f"/charge/{data['id']}/{op}"
            payload = (
                self.refund_request(data, response)
                if op == "refund"
                else {"psp_reference": response["psp_reference"]}
            )
            if op == "capture":
                payload["amount_to_capture"] = data["amount"]
            self.assert_response(
                self.client.request(
                    "POST", path, payload, idem=self.uid(), token=token
                ),
                "/refund" if op == "refund" else "/charge/{id}/" + op,
                400,
            )
            self.assertEqual(
                self.lookup(data["id"])["status"],
                "CONFIRMED" if op == "refund" else "AUTHORIZED",
            )

    def test_review_refresh_reusable_no_undeclared_rotation(self):
        _, refresh = self.access()
        for _ in range(2):
            result = self.assert_response(
                self.client.request(
                    "POST",
                    "/token",
                    dict(
                        client_id=self.client_id,
                        client_secret=self.client_secret,
                        grant_type="refresh_token",
                        refresh_token=refresh,
                    ),
                ),
                "/token",
                200,
                grant="refresh_token",
            )
            self.assertNotIn("refresh_token", result)

    def test_review_token_errors_match_enum(self):
        payload = dict(
            client_id=self.client_id,
            client_secret=self.client_secret,
            grant_type="refresh_token",
            refresh_token="unknown",
        )
        for change, signed, error in [
            ({}, False, "invalid_request"),
            ({}, True, "invalid_grant"),
            ({"client_secret": "wrong"}, True, "invalid_client"),
            (
                {
                    "grant_type": "urn:uberpay:params:oauth:grant-type:wallet-grant",
                    "scope": "wrong",
                    "country_iso2": "BR",
                    "funding_method": "DEMO_SYNC",
                    "wallet_reference_data": {"payment_identifier": "demo-account"},
                },
                True,
                "invalid_scope",
            ),
        ]:
            value = self.assert_response(
                self.client.request(
                    "POST", "/token", dict(payload, **change), signed=signed
                ),
                "/token",
                400,
            )
            self.assertEqual(value["error"], error)

    def test_review_required_headers_and_malformed_inputs(self):
        data = self.fixture("charge")
        self.assert_response(
            self.client.request(
                "POST",
                "/charge",
                data,
                idem=self.uid(),
                headers={"Accept-Language": ""},
            ),
            "/charge",
            400,
        )
        self.assert_response(
            self.client.request(
                "GET", "/find-transaction?id=missing", headers={"Content-Type": ""}
            ),
            "/find-transaction",
            400,
            "get",
        )
        self.assert_response(
            self.client.request("GET", "/find-transaction?id=%zz", signed=False),
            "/find-transaction",
            400,
            "get",
        )
        self.assertEqual(
            self.client.request(
                "GET", "/health?x=%zz", signed=False, outside=True
            ).status,
            200,
        )
        data["initiated_at"] = "2026-11-20T10:00:00"
        self.assert_response(
            self.client.request("POST", "/charge", data, idem=self.uid()),
            "/charge",
            400,
        )
        data = self.fixture("charge")
        raw = json.dumps(data).replace("1000000", "01000000").encode()
        self.assert_response(
            self.client.request("POST", "/charge", raw=raw, idem=self.uid()),
            "/charge",
            400,
        )

    def test_review_reference_mismatch_and_missing_resources(self):
        for op in ("capture", "cancel"):
            data, _ = self.charge(capture_method="MANUAL")
            payload = {"psp_reference": "wrong"}
            if op == "capture":
                payload["amount_to_capture"] = data["amount"]
            self.assert_response(
                self.client.request(
                    "POST", f"/charge/{data['id']}/{op}", payload, idem=self.uid()
                ),
                "/charge/{id}/" + op,
                400,
            )
            self.assert_response(
                self.client.request(
                    "POST", "/charge/missing/" + op, payload, idem=self.uid()
                ),
                "/charge/{id}/" + op,
                404,
            )
        data, response = self.charge()
        self.assert_response(
            self.client.request(
                "GET", f"/find-transaction?id={data['id']}&merchant_reference=wrong"
            ),
            "/find-transaction",
            404,
            "get",
        )
        self.assert_response(
            self.client.request("GET", "/find-transaction"),
            "/find-transaction",
            400,
            "get",
        )
        self.assert_response(
            self.client.request("GET", "/find-transaction?id=missing"),
            "/find-transaction",
            404,
            "get",
        )
        refund = self.refund_request(data, response)
        refund["original_transaction_id"] = "missing"
        self.assert_response(
            self.client.request("POST", "/refund", refund, idem=self.uid()),
            "/refund",
            404,
        )

    def test_review_capture_declines_do_not_debit(self):
        token, _ = self.access("get_balance")

        def balance():
            return self.assert_response(
                self.client.request(
                    "POST",
                    "/balance",
                    {"account_id": "demo-account", "funding_method": "DEMO_SYNC"},
                    token=token,
                ),
                "/balance",
                200,
            )

        for funding, detail in [
            ("DEMO_CAPTURE_HARD", "BANK_HARD_ERROR"),
            ("DEMO_CAPTURE_SOFT", "BANK_SOFT_ERROR"),
        ]:
            data, _ = self.charge(capture_method="MANUAL", funding_method=funding)
            before = balance()
            result = self.assert_response(
                self.client.request(
                    "POST",
                    f"/charge/{data['id']}/capture",
                    {"amount_to_capture": data["amount"]},
                    idem=self.uid(),
                ),
                "/charge/{id}/capture",
                422,
            )
            self.assertEqual(result["detail"], detail)
            after = balance()
            self.assertEqual(before["balance"], after["balance"])
            self.assertEqual(
                before["available_balance"]["value"] + data["amount"]["value"],
                after["available_balance"]["value"],
            )

    def test_review_payout_declared_conflicts_declines_and_ledger(self):
        cases = json.loads((contract.ROOT / "contracts/demo-outcomes.json").read_text())
        for key, status in [("payout_payee_400", 400), ("payout_payee_422", 422)]:
            for account, detail in cases[key].items():
                data = self.fixture("payout")
                data["payee"]["account"]["account_id"] = account
                value = self.assert_response(
                    self.client.request("POST", "/payout", data, idem=self.uid()),
                    "/payout",
                    status,
                )
                self.assertEqual(value["detail"], detail)
        data = self.fixture("payout", funding_method="DEMO_REJECT")
        self.assert_response(
            self.client.request("POST", "/payout", data, idem=self.uid()),
            "/payout",
            400,
        )
        data = self.fixture("payout")
        key = self.uid()
        self.assert_response(
            self.client.request("POST", "/payout", data, idem=key), "/payout", 200
        )
        data["amount"]["value"] += 1
        for idem in (key, self.uid()):
            self.assertEqual(
                self.assert_response(
                    self.client.request("POST", "/payout", data, idem=idem),
                    "/payout",
                    409,
                )["detail"],
                "DUPLICATE_REQUEST_ERROR",
            )
        for funding in ("DEMO_SYNC", "DEMO_WEBVIEW", "DEMO_QR"):
            with self.db() as db:
                before = dict(
                    db.execute(
                        "SELECT account_id,balance_e5 FROM accounts WHERE client_id=%s AND account_id IN (%s,%s)",
                        (self.client_id, "demo-settlement", "demo-account"),
                    ).fetchall()
                )
            data = self.fixture("payout", funding_method=funding)
            result = self.assert_response(
                self.client.request("POST", "/payout", data, idem=self.uid()),
                "/payout",
                200,
            )
            self.assertNotIn("actions", result)
            if result["state"]["value"] == "PENDING":
                self.accepted("payoutCallback", data["id"])
            with self.db() as db:
                after = dict(
                    db.execute(
                        "SELECT account_id,balance_e5 FROM accounts WHERE client_id=%s AND account_id IN (%s,%s)",
                        (self.client_id, "demo-settlement", "demo-account"),
                    ).fetchall()
                )
            self.assertEqual(
                after["demo-settlement"],
                before["demo-settlement"] - data["amount"]["value"],
            )
            self.assertEqual(
                after["demo-account"], before["demo-account"] + data["amount"]["value"]
            )

    def test_review_balance_missing_owned_account_and_validation(self):
        token, _ = self.access("get_balance")
        import hashlib

        with self.db() as db:
            db.execute(
                "UPDATE tokens SET account_id='deleted-account' WHERE client_id=%s AND token_hash=%s",
                (self.client_id, hashlib.sha256(token.encode()).hexdigest()),
            )
        data = {"account_id": "deleted-account", "funding_method": "DEMO_SYNC"}
        self.assert_response(
            self.client.request("POST", "/balance", data, token=token), "/balance", 404
        )
        token, _ = self.access("get_balance")
        self.assert_response(
            self.client.request(
                "POST",
                "/balance",
                {"account_id": "demo-account", "funding_method": "unknown"},
                token=token,
            ),
            "/balance",
            400,
        )

    def test_review_transient_errors_all_routes_retry_same_key(self):
        original, response = self.charge()
        manual, _ = self.charge(capture_method="MANUAL")
        cancel, _ = self.charge(capture_method="MANUAL")
        token, refresh = self.access()
        requests = [
            ("POST", "/charge", "/charge", self.fixture("charge"), None),
            ("POST", "/payout", "/payout", self.fixture("payout"), None),
            (
                "POST",
                f"/charge/{manual['id']}/capture",
                "/charge/{id}/capture",
                {"amount_to_capture": manual["amount"]},
                None,
            ),
            ("POST", f"/charge/{cancel['id']}/cancel", "/charge/{id}/cancel", {}, None),
            (
                "POST",
                "/refund",
                "/refund",
                self.refund_request(original, response),
                None,
            ),
            (
                "GET",
                "/find-transaction?id=" + original["id"],
                "/find-transaction",
                None,
                None,
            ),
            (
                "POST",
                "/balance",
                "/balance",
                {"account_id": "demo-account", "funding_method": "DEMO_SYNC"},
                token,
            ),
            (
                "POST",
                "/token",
                "/token",
                dict(
                    client_id=self.client_id,
                    client_secret=self.client_secret,
                    grant_type="refresh_token",
                    refresh_token=refresh,
                ),
                None,
            ),
        ]
        for method, path, template, payload, bearer in requests:
            with self.subTest(path=template):
                key = self.uid()
                with self.db() as lock:
                    lock.execute(
                        "SELECT pg_advisory_lock(hashtext(%s))", (self.client_id,)
                    )
                    started = time.monotonic()
                    try:
                        result = self.client.request(
                            method, path, payload, idem=key, token=bearer
                        )
                        self.assert_response(result, template, 500, method.lower())
                        self.assertLess(time.monotonic() - started, 6)
                    finally:
                        lock.execute(
                            "SELECT pg_advisory_unlock(hashtext(%s))", (self.client_id,)
                        )
                result = self.client.request(
                    method, path, payload, idem=key, token=bearer
                )
                self.assert_response(
                    result,
                    template,
                    201 if path == "/refund" else 200,
                    method.lower(),
                    grant="refresh_token" if path == "/token" else None,
                )

    def test_review_database_connection_loss_recovers(self):
        with self.db() as db:
            db.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE application_name=%s AND pid<>pg_backend_pid()",
                (self.client_id,),
            )
        self.process.wait_ready()
        data, _ = self.charge()
        self.assertEqual(self.lookup(data["id"])["status"], "CONFIRMED")

    def test_review_concurrent_refunds_never_exceed_original(self):
        original, response = self.charge()
        requests = [self.refund_request(original, response) for _ in range(6)]
        with ThreadPoolExecutor(max_workers=6) as pool:
            replies = list(
                pool.map(
                    lambda data: self.client.request(
                        "POST", "/refund", data, idem=data["id"]
                    ),
                    requests,
                )
            )
        self.assertEqual(sum(r.status == 201 for r in replies), 1)
        self.assertEqual(sum(r.status == 400 for r in replies), 5)
        for r in replies:
            contract.response("post", "/refund", r.status, r.body)

    def test_review_cancelled_lookup_and_refund_of_action_payment(self):
        data, _ = self.charge(capture_method="MANUAL")
        self.assert_response(
            self.client.request(
                "POST", f"/charge/{data['id']}/cancel", {}, idem=self.uid()
            ),
            "/charge/{id}/cancel",
            200,
        )
        self.assert_response(
            self.client.request("GET", "/find-transaction?id=" + data["id"]),
            "/find-transaction",
            200,
            "get",
        )
        self.assertEqual(self.lookup(data["id"])["status"], "CANCELLED")
        original, response = self.charge(funding_method="DEMO_WEBVIEW")
        self.submit_action(self.action_page(response))
        self.accepted("chargeCallback", original["id"])
        data = self.refund_request(original, response)
        result = self.assert_response(
            self.client.request("POST", "/refund", data, idem=self.uid()),
            "/refund",
            201,
        )
        self.assertEqual(result["status"], "CONFIRMED")
        self.assertEqual(self.lookup(data["id"])["status"], "CONFIRMED")

    def test_review_declared_auth_errors_exact(self):
        cases = [
            ("/charge", "/charge", self.fixture("charge"), 401, "POST"),
            ("/payout", "/payout", self.fixture("payout"), 401, "POST"),
            (
                "/balance",
                "/balance",
                {"account_id": "demo-account", "funding_method": "DEMO_SYNC"},
                401,
                "POST",
            ),
            (
                "/charge/missing/capture",
                "/charge/{id}/capture",
                {"amount_to_capture": {"value": 1, "currency": "BRL"}},
                400,
                "POST",
            ),
            ("/charge/missing/cancel", "/charge/{id}/cancel", {}, 400, "POST"),
            ("/refund", "/refund", {}, 400, "POST"),
            ("/find-transaction?id=missing", "/find-transaction", None, 400, "GET"),
            ("/token", "/token", {}, 400, "POST"),
        ]
        for path, template, payload, status, method in cases:
            self.assert_response(
                self.client.request(
                    method, path, payload, idem=self.uid(), signed=False
                ),
                template,
                status,
                method.lower(),
            )

    def test_review_bearer_scheme_is_case_insensitive(self):
        token, _ = self.access("get_balance")
        self.assert_response(
            self.client.request(
                "POST",
                "/balance",
                {"account_id": "demo-account", "funding_method": "DEMO_SYNC"},
                headers={"Authorization": "bEaReR " + token},
            ),
            "/balance",
            200,
        )

    def test_review_pending_restart_without_control_completion(self):
        data, _ = self.charge(funding_method="DEMO_PENDING")
        self.process.restart(hard=True)
        self.accepted("chargeCallback", data["id"])
        self.assertEqual(self.lookup(data["id"])["status"], "CONFIRMED")

    def test_review_payout_payee_not_an_insufficient_funds_trigger(self):
        data = self.fixture("payout")
        data["payee"]["account"]["account_id"] = "insufficient-account"
        self.assert_response(
            self.client.request("POST", "/payout", data, idem=self.uid()),
            "/payout",
            200,
        )
        for account in ("constructor", "toString", "__proto__"):
            unknown = self.fixture("payout")
            unknown["payee"]["account"]["account_id"] = account
            self.assert_response(
                self.client.request("POST", "/payout", unknown, idem=self.uid()),
                "/payout",
                400,
            )

    def test_review_payment_metadata_reaches_demo_policy_and_redacts(self):
        data = self.fixture("charge")
        data["payment_data"] = [
            {
                "type": "meal_voucher_payment_data",
                "meal_voucher_merchant_id": "demo-merchant",
                "meal_voucher_merchant_secret": "demo-merchant-secret",
            },
            {
                "type": "point_conversion_payment_data",
                "points_to_charge_e5": data["amount"]["value"],
            },
            {"type": "order_payment_data", "order_id": "synthetic-order-" + data["id"]},
        ]
        response = self.assert_response(
            self.client.request("POST", "/charge", data, idem=self.uid()),
            "/charge",
            200,
        )
        with self.db() as db:
            row = db.execute(
                "SELECT source_account_id,order_id FROM transactions WHERE client_id=%s AND kind=%s AND id=%s",
                (self.client_id, "charge", data["id"]),
            ).fetchone()
        self.assertEqual(
            row, (data["source"]["id"], data["payment_data"][2]["order_id"])
        )
        refund = self.refund_request(data, response)
        self.assert_response(
            self.client.request("POST", "/refund", refund, idem=self.uid()),
            "/refund",
            201,
        )
        with self.db() as db:
            source = db.execute(
                "SELECT source_account_id FROM transactions WHERE client_id=%s AND kind='refund' AND id=%s",
                (self.client_id, refund["id"]),
            ).fetchone()[0]
        self.assertEqual(source, data["source"]["id"])
        state = self.client.control("GET", "state")
        self.assertNotIn("demo-merchant-secret", state.raw.decode())
        for invalid in [
            ("meal_voucher_merchant_id", "unknown"),
            ("points_to_charge_e5", 1),
        ]:
            change = json.loads(json.dumps(data))
            change["id"] = self.uid()
            change["payment_data"][0 if invalid[0].startswith("meal") else 1][
                invalid[0]
            ] = invalid[1]
            self.assert_response(
                self.client.request("POST", "/charge", change, idem=self.uid()),
                "/charge",
                400,
            )
        token, _ = self.access("get_balance")
        payload = {
            "account_id": "demo-account",
            "funding_method": "DEMO_SYNC",
            "payment_data": [
                {
                    "type": "meal_voucher_payment_data",
                    "meal_voucher_merchant_id": "unknown",
                }
            ],
        }
        self.assert_response(
            self.client.request("POST", "/balance", payload, token=token),
            "/balance",
            400,
        )

    def test_review_registered_oauth_errors_redirect_with_state(self):
        from urllib.parse import parse_qs

        params = dict(
            client_id=self.client_id,
            response_type="code",
            redirect_uri="http://127.0.0.1:9090/return",
            scope="invalid",
            state="opaque-review-state",
            authorization_details="[]",
        )
        result = self.client.request(
            "GET", "/authorize?" + urlencode(params), signed=False
        )
        self.assertEqual(result.status, 302, result.raw)
        redirect = parse_qs(urlsplit(result.headers["location"]).query)
        self.assertEqual(redirect["error"], ["invalid_scope"])
        self.assertEqual(redirect["state"], ["opaque-review-state"])

    def test_review_strict_account_name_is_explicit_opt_in(self):
        payload = self.fixture("charge")
        del payload["source"]["name"]
        del payload["destination"]["name"]
        old = self.process.environment.get("STRICT_ACCOUNT_NAME")
        try:
            self.process.environment["STRICT_ACCOUNT_NAME"] = "true"
            self.process.restart(hard=True)
            self.assert_response(
                self.client.request("POST", "/charge", payload, idem=self.uid()),
                "/charge",
                400,
            )
        finally:
            if old is None:
                self.process.environment.pop("STRICT_ACCOUNT_NAME", None)
            else:
                self.process.environment["STRICT_ACCOUNT_NAME"] = old
            self.process.restart(hard=True)
        self.assert_response(
            self.client.request("POST", "/charge", payload, idem=self.uid()),
            "/charge",
            200,
        )

    def test_review_async_refund_rejection_and_lookup_states(self):
        data, response = self.charge(funding_method="DEMO_PENDING")
        self.assertEqual(self.lookup(data["id"])["status"], "PENDING")
        self.accepted("chargeCallback", data["id"])
        refund = self.refund_request(data, response)
        self.assert_response(
            self.client.request("POST", "/refund", refund, idem=self.uid()),
            "/refund",
            201,
        )
        self.assertEqual(
            self.client.control(
                "POST",
                "complete",
                {"id": refund["id"], "kind": "refund", "state": "REJECTED"},
            ).status,
            200,
        )
        event = self.accepted("updateRefund", refund["id"])[0]
        self.assertEqual(event["payload"]["status"], "REJECTED")
        self.assertGreater(len(event["payload"]["failed_reason"].split()), 1)
        self.assertEqual(self.lookup(refund["id"])["status"], "REJECTED")

    def test_review_signature_time_extensions_and_imf_date(self):
        now = int(time.time())
        good = {"created": str(now), "expires": str(now + 60)}
        self.assert_response(
            self.client.request(
                "POST",
                "/charge",
                self.fixture("charge"),
                idem=self.uid(),
                signature_parameters=good,
            ),
            "/charge",
            200,
        )
        for value in (
            {"created": str(now + 9999)},
            {"expires": str(now - 1)},
            {"created": "invalid"},
        ):
            self.assert_response(
                self.client.request(
                    "POST",
                    "/charge",
                    self.fixture("charge"),
                    idem=self.uid(),
                    signature_parameters=value,
                ),
                "/charge",
                401,
            )
        obsolete = time.strftime("%A, %d-%b-%y %H:%M:%S GMT", time.gmtime())
        self.assert_response(
            self.client.request(
                "POST",
                "/charge",
                self.fixture("charge"),
                idem=self.uid(),
                date=obsolete,
            ),
            "/charge",
            401,
        )
        # Signature key lookup must not resolve inherited Object properties in Node.
        r = self.client.request(
            "POST",
            "/charge",
            self.fixture("charge"),
            idem=self.uid(),
            key_id_override="toString",
        )
        self.assert_response(r, "/charge", 401)
