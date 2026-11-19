"""Cross-language completion/recovery acceptance tests; wire and SQL are independent."""

import base64
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager, nullcontext
from urllib.parse import urlencode

from . import contract
from .processor_fixture import ProcessorFixture
from .wire import eventually


class CompletionSuite:
    def feature_profile(self):
        return json.loads(
            (contract.ROOT / "contracts/reference-features.json").read_text()
        )

    def test_complete_charge_reason_enum_sync_and_async(self):
        for reason in self.feature_profile()["charge_reasons"]:
            with self.subTest(reason=reason):
                _, response = self.charge(funding_method="DEMO_REJECT_" + reason)
                self.assertEqual(
                    response["state"], {"value": "REJECTED", "detail": reason}
                )
                data, response = self.charge(
                    funding_method="DEMO_PENDING_REJECT_" + reason
                )
                self.assertEqual(response["state"]["value"], "PENDING")
                self.assertEqual(
                    self.accepted("chargeCallback", data["id"])[0]["payload"]["state"],
                    {"value": "REJECTED", "detail": reason},
                )

    def test_complete_payout_reason_enum(self):
        for reason in self.feature_profile()["payout_reasons"]:
            with self.subTest(reason=reason):
                data = self.fixture(
                    "payout", funding_method="DEMO_PENDING_REJECT_" + reason
                )
                self.assert_response(
                    self.client.request("POST", "/payout", data, idem=self.uid()),
                    "/payout",
                    200,
                )
                self.assertEqual(
                    self.accepted("payoutCallback", data["id"])[0]["payload"]["state"],
                    {"value": "REJECTED", "detail": reason},
                )

    def test_complete_idempotent_error_keeps_original_localization(self):
        for remote in (False, True):
            scope = self.http_processor() if remote else nullcontext()
            with scope:
                data = self.fixture(
                    "payout", funding_method="DEMO_REJECT_BANK_HARD_ERROR"
                )
                key = self.uid()
                first = self.client.request(
                    "POST",
                    "/payout",
                    data,
                    idem=key,
                    headers={"Accept-Language": "pt-BR"},
                )
                self.assert_response(first, "/payout", 422)
                second = self.client.request(
                    "POST",
                    "/payout",
                    data,
                    idem=key,
                    headers={"Accept-Language": "en-US"},
                )
                self.assertEqual(first.raw, second.raw)
                self.assertEqual(first.headers["content-language"], "pt-BR")
                self.assertEqual(second.headers["content-language"], "pt-BR")

    def test_complete_itemized_balance(self):
        token, _ = self.access()
        result = self.assert_response(
            self.client.request(
                "POST",
                "/balance",
                {"account_id": "demo-account", "funding_method": "DEMO_MEAL_VOUCHER"},
                token=token,
            ),
            "/balance",
            200,
        )
        item = result["itemized_balances"][0]
        self.assertEqual(item["available_balance"], result["available_balance"])
        self.assertEqual(item["balance"], result["balance"])

    def test_complete_localized_action_and_consent(self):
        data = self.fixture("charge", funding_method="DEMO_WEBVIEW")
        response = self.assert_response(
            self.client.request(
                "POST",
                "/charge",
                data,
                idem=self.uid(),
                headers={"Accept-Language": "pt-BR"},
            ),
            "/charge",
            200,
        )
        from urllib.parse import urlsplit

        u = urlsplit(response["actions"][0]["url"])
        page = self.client.request(
            "GET", u.path + "?" + u.query, signed=False, outside=True
        )
        self.assertEqual(page.headers["content-language"], "pt-BR")
        self.assertIn(
            self.feature_profile()["translations"]["pt-BR"]["payment_title"],
            page.raw.decode(),
        )
        form = self.action_page(response)
        self.assertIn(self.submit_action(form).status, (200, 302))
        query = dict(
            client_id=self.client_id,
            response_type="code",
            redirect_uri="http://127.0.0.1:9090/return",
            scope="charge refund offline_access",
            state=self.uid(),
            authorization_details=json.dumps(
                [
                    dict(
                        type="authorization_detail",
                        funding_method="DEMO_SYNC",
                        country_iso2="BR",
                        locale="pt-BR",
                    )
                ]
            ),
        )
        page = self.client.request(
            "GET", "/authorize?" + urlencode(query), signed=False
        )
        self.assertEqual(page.status, 200, page.raw)
        self.assertEqual(page.headers["content-language"], "pt-BR")
        self.assertIn("DEMO_SYNC", page.raw.decode())
        self.assertIn("BR", page.raw.decode())
        self.assertIn(
            self.feature_profile()["translations"]["pt-BR"]["consent_title"],
            page.raw.decode(),
        )

        from .suite import HiddenForm
        from urllib.parse import parse_qs, urlsplit

        form = HiddenForm()
        form.feed(page.raw.decode())
        consent = self.submit_action(form, "approve")
        self.assertEqual(consent.status, 302)
        code = parse_qs(urlsplit(consent.headers["location"]).query)["code"][0]
        refresh = self.assert_response(
            self.client.request(
                "POST",
                "/token",
                dict(
                    client_id=self.client_id,
                    client_secret=self.client_secret,
                    grant_type="authorization_code",
                    code=code,
                    redirect_uri=query["redirect_uri"],
                ),
            ),
            "/token",
            200,
            grant="authorization_code",
        )["refresh_token"]
        access = self.assert_response(
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
        )["access_token"]
        import hashlib

        with self.db() as db:
            context = json.loads(
                db.execute(
                    "SELECT state FROM tokens WHERE client_id=%s AND token_hash=%s",
                    (self.client_id, hashlib.sha256(access.encode()).hexdigest()),
                ).fetchone()[0]
            )
            self.assertEqual(context["locale"], "pt-BR")
            self.assertEqual(context["details"][0]["funding_method"], "DEMO_SYNC")
            self.assertEqual(context["details"][0]["country_iso2"], "BR")

    def test_complete_refund_pending_rejection_and_reversal(self):
        data, response = self.charge(funding_method="DEMO_REFUND_REJECT")
        request = self.refund_request(data, response)
        result = self.assert_response(
            self.client.request("POST", "/refund", request, idem=self.uid()),
            "/refund",
            201,
        )
        self.assertEqual(result["status"], "REJECTED")
        with self.db() as db:
            self.assertFalse(
                db.execute(
                    "SELECT 1 FROM jobs WHERE client_id=%s AND tx_id=%s",
                    (self.client_id, request["id"]),
                ).fetchall()
            )
        for method, expected in [
            ("DEMO_REFUND_PENDING_REJECT", ["REJECTED"]),
            ("DEMO_REFUND_REVERSAL", ["CONFIRMED", "REJECTED"]),
        ]:
            with self.subTest(method=method):
                data, response = self.charge(funding_method=method)
                request = self.refund_request(data, response)
                result = self.assert_response(
                    self.client.request("POST", "/refund", request, idem=self.uid()),
                    "/refund",
                    201,
                )
                self.assertEqual(result["status"], "PENDING")
                events = eventually(
                    lambda: (
                        items
                        if len(
                            items := self.mock.matching("updateRefund", request["id"])
                        )
                        >= len(expected)
                        else None
                    ),
                    description="refund terminal event versions",
                )
                self.assertEqual([e["payload"]["status"] for e in events], expected)
                with self.db() as db:
                    row = db.execute(
                        "SELECT amount_e5,recovered_e5,debt_e5 FROM refund_reversals WHERE client_id=%s AND refund_id=%s",
                        (self.client_id, request["id"]),
                    ).fetchone()
                    if method == "DEMO_REFUND_REVERSAL":
                        self.assertEqual(
                            row, (data["amount"]["value"], data["amount"]["value"], 0)
                        )
                self.assertEqual(
                    self.client.control(
                        "POST", "reverse-refund", {"id": request["id"]}
                    ).status,
                    200,
                )
                time.sleep(0.1)
                self.assertEqual(
                    len(self.mock.matching("updateRefund", request["id"])),
                    len(expected),
                )

    def test_complete_refund_reversal_debt_preserves_other_holds(self):
        account = "debt-" + self.uid()
        with self.db() as db:
            db.execute(
                "INSERT INTO accounts(client_id,account_id,currency,available_e5,balance_e5) VALUES(%s,%s,'BRL',1000000,1000000)",
                (self.client_id, account),
            )
        auth = {"type": "access_token_authorization", "account_id": account}
        # Insert an independently scoped fixture bearer, not a production identity bypass.
        token = self.uid()
        import hashlib

        with self.db() as db:
            db.execute(
                "INSERT INTO tokens(token_hash,kind,client_id,account_id,scope,expires_at) VALUES(%s,'ACCESS',%s,%s,'charge refund',now()+interval '10 minutes')",
                (hashlib.sha256(token.encode()).hexdigest(), self.client_id, account),
            )
        data = self.fixture("charge", authorization=auth)
        response = self.assert_response(
            self.client.request("POST", "/charge", data, idem=self.uid(), token=token),
            "/charge",
            200,
        )
        refund = self.refund_request(data, response)
        self.assert_response(
            self.client.request(
                "POST", "/refund", refund, idem=self.uid(), token=token
            ),
            "/refund",
            201,
        )
        hold = self.fixture(
            "charge",
            authorization=auth,
            capture_method="MANUAL",
            amount={"value": 400000, "currency": "BRL"},
        )
        self.assert_response(
            self.client.request("POST", "/charge", hold, idem=self.uid(), token=token),
            "/charge",
            200,
        )
        spend = self.fixture(
            "charge", authorization=auth, amount={"value": 600000, "currency": "BRL"}
        )
        self.assert_response(
            self.client.request("POST", "/charge", spend, idem=self.uid(), token=token),
            "/charge",
            200,
        )
        self.assertEqual(
            self.client.control("POST", "reverse-refund", {"id": refund["id"]}).status,
            200,
        )
        with self.db() as db:
            self.assertEqual(
                db.execute(
                    "SELECT available_e5,balance_e5 FROM accounts WHERE client_id=%s AND account_id=%s",
                    (self.client_id, account),
                ).fetchone(),
                (0, 400000),
            )
            self.assertEqual(
                db.execute(
                    "SELECT debt_e5 FROM account_debts WHERE client_id=%s AND account_id=%s",
                    (self.client_id, account),
                ).fetchone()[0],
                1000000,
            )
        self.assert_response(
            self.client.request(
                "POST", f"/charge/{hold['id']}/cancel", {}, idem=self.uid(), token=token
            ),
            "/charge/{id}/cancel",
            200,
        )
        with self.db() as db:
            self.assertEqual(
                db.execute(
                    "SELECT debt_e5 FROM account_debts WHERE client_id=%s AND account_id=%s",
                    (self.client_id, account),
                ).fetchone()[0],
                600000,
            )
            self.assertEqual(
                db.execute(
                    "SELECT available_e5,balance_e5 FROM accounts WHERE client_id=%s AND account_id=%s",
                    (self.client_id, account),
                ).fetchone(),
                (0, 0),
            )

    def test_complete_deposit_flow_and_ordered_amount_update(self):
        from .suite import HiddenForm

        for decision, expected in [
            ("approve", "confirmDeposit"),
            ("deny", "cancelDeposit"),
        ]:
            with self.subTest(decision=decision):
                data, response = self.charge(funding_method="DEMO_DEPOSIT")
                form = self.action_page(response)
                self.assertIsInstance(form, HiddenForm)
                form.fields["amount_e5"] = "500000"
                self.assertIn(self.submit_action(form, decision).status, (200, 302))
                self.accepted(expected, data["id"])
                self.assertFalse(
                    self.mock.matching("chargeCallback", data["id"], accepted=False)
                )
                if decision == "approve":
                    events = [
                        e
                        for e in self.mock.events
                        if e["id"] == data["id"] and e["status"] == 200
                    ]
                    self.assertEqual(
                        [e["name"] for e in events], ["updateDeposit", "confirmDeposit"]
                    )
                    self.assertEqual(events[0]["payload"]["amount"]["value"], 500000)
                    self.assertEqual(self.lookup(data["id"])["amount"]["value"], 500000)

    def test_complete_funding_alias_country_currency_and_wallet_grant(self):
        old = self.process.environment.get("FUNDING_METHODS_FILE")
        self.process.environment["FUNDING_METHODS_FILE"] = str(
            contract.ROOT / "contracts/funding-methods.example.json"
        )
        try:
            self.process.restart()
            self.charge(funding_method="TEST_ASSIGNED_BR")
            wrong = self.fixture(
                "charge", funding_method="TEST_ASSIGNED_BR", country_iso2="SG"
            )
            self.assert_response(
                self.client.request("POST", "/charge", wrong, idem=self.uid()),
                "/charge",
                400,
            )
            wrong = self.fixture(
                "charge",
                funding_method="TEST_ASSIGNED_BR",
                amount={"value": 1000000, "currency": "SGD"},
            )
            self.assert_response(
                self.client.request("POST", "/charge", wrong, idem=self.uid()),
                "/charge",
                400,
            )
            grant = "urn:uberpay:params:oauth:grant-type:wallet-grant"
            request = dict(
                client_id=self.client_id,
                client_secret=self.client_secret,
                grant_type=grant,
                scope="charge offline_access",
                funding_method="TEST_ASSIGNED_SG",
                country_iso2="SG",
                wallet_reference_data={"payment_identifier": "demo-account"},
            )
            self.assert_response(
                self.client.request("POST", "/token", request),
                "/token",
                200,
                grant=grant,
            )
            request["country_iso2"] = "BR"
            self.assert_response(
                self.client.request("POST", "/token", request), "/token", 400
            )
        finally:
            if old is None:
                self.process.environment.pop("FUNDING_METHODS_FILE", None)
            else:
                self.process.environment["FUNDING_METHODS_FILE"] = old
            self.process.restart()

    def test_complete_callback_ack_crash_and_multiworker_fencing(self):
        from .run import GatewayProcess, port

        data = self.fixture("charge", funding_method="DEMO_PENDING")
        first = self.mock.block_callback("chargeCallback", data["id"])
        second = self.mock.block_callback("chargeCallback", data["id"])
        self.mock.fail_next("chargeCallback", data["id"], 200, 503)
        other = None
        try:
            env = dict(self.process.environment, PORT=str(port()))
            other = GatewayProcess(
                self.process.argv,
                env,
                contract.ROOT / ".artifacts" / (self.client_id + "-second-worker.log"),
            )
            other.client = self.client
            other.start()
            # Read the second process's own listening address, not the primary health.
            from .wire import Client

            health = Client(
                "http://127.0.0.1:" + env["PORT"] + "/uberpay/v1", self.client.key
            )
            eventually(
                lambda: (
                    health.request("GET", "/health", signed=False, outside=True).status
                    == 200
                ),
                timeout=35,
            )
            # Both workers must be ready before the callback is blocked. Otherwise
            # JVM startup can consume the first worker's HTTP deadline and create
            # ordinary retries before this lease-stealing scenario even begins.
            self.assert_response(
                self.client.request("POST", "/charge", data, idem=self.uid()),
                "/charge",
                200,
            )
            self.assertTrue(first.entered.wait(4))
            with self.db() as db:
                db.execute(
                    "UPDATE jobs SET lease_until=now()-interval '1 second' WHERE client_id=%s AND tx_id=%s AND job_type='CALLBACK'",
                    (self.client_id, data["id"]),
                )
            self.assertTrue(second.entered.wait(4))
            first.release.set()
            time.sleep(0.15)
            with self.db() as db:
                attempts, status, lease = db.execute(
                    "SELECT attempts,status,lease_until FROM jobs WHERE client_id=%s AND tx_id=%s AND job_type='CALLBACK'",
                    (self.client_id, data["id"]),
                ).fetchone()
                self.assertEqual(attempts, 2)
                self.assertNotEqual(
                    status,
                    "DONE",
                    "stale successful attempt must not acknowledge the newer leased attempt",
                )
                self.assertIsNotNone(lease)
            second.release.set()

            def done():
                with self.db() as db:
                    return (
                        db.execute(
                            "SELECT status FROM jobs WHERE client_id=%s AND tx_id=%s AND job_type='CALLBACK'",
                            (self.client_id, data["id"]),
                        ).fetchone()[0]
                        == "DONE"
                    )

            eventually(done, description="multiworker fenced callback delivery")
            events = self.mock.matching("chargeCallback", data["id"], accepted=False)
            self.assertGreaterEqual(len(events), 3)
            self.assertEqual(len({e["idempotency_key"] for e in events}), 1)
            # Now kill the gateway after the receiver accepted, but before ack recording.
            crash = self.fixture("charge", funding_method="DEMO_PENDING")
            barrier = self.mock.block_callback("chargeCallback", crash["id"])
            other.close()
            other = None
            try:
                self.assert_response(
                    self.client.request("POST", "/charge", crash, idem=self.uid()),
                    "/charge",
                    200,
                )
                self.assertTrue(barrier.entered.wait(4))
                self.process.restart(hard=True)
                with self.db() as db:
                    db.execute(
                        "UPDATE jobs SET lease_until=now()-interval '1 second' WHERE client_id=%s AND tx_id=%s AND job_type='CALLBACK'",
                        (self.client_id, crash["id"]),
                    )
            finally:
                barrier.release.set()
            eventually(
                lambda: len(self.mock.matching("chargeCallback", crash["id"])) >= 2,
                description="callback retry after ack-window crash",
            )
            self.assertEqual(
                len(
                    {
                        e["idempotency_key"]
                        for e in self.mock.matching("chargeCallback", crash["id"])
                    }
                ),
                1,
            )
        finally:
            first.release.set()
            second.release.set()
            if other:
                other.close()

    @contextmanager
    def http_processor(self):
        processor = ProcessorFixture().start()
        old = self.process.environment.copy()
        self.process.environment.update(
            PROCESSOR_MODE="http",
            PROCESSOR_URL=processor.url,
            PROCESSOR_AUTH_TOKEN=processor.token,
            PROCESSOR_STATE_KEY=base64.b64encode(os.urandom(32)).decode(),
            PROCESSOR_TIMEOUT_MS="1000",
            PROCESSOR_LEASE_MS="1800",
            PROCESSOR_WAIT_MS="180",
            PROCESSOR_POLL_MS="100",
            PROCESSOR_MAX_FAILURES="8",
        )
        try:
            self.process.restart()
            yield processor
        finally:
            self.process.stop()
            self.process.environment.clear()
            self.process.environment.update(old)
            self.process.start()
            self.process.wait_ready()
            processor.close()

    def provider_entry(self, processor, resource, operation="charge"):
        return eventually(
            lambda: next(
                (
                    e
                    for e in processor.entries(self.client_id)
                    if e["resource"] == resource and e["operation"] == operation
                ),
                None,
            ),
            description="independent provider commit",
        )

    def test_complete_processor_prepare_crash_before_dispatch(self):
        with self.http_processor() as processor:
            self.process.environment["PROCESSOR_TEST_PAUSE_AFTER_PREPARE"] = "true"
            self.process.restart()
            data, key = self.fixture("charge"), self.uid()
            self.assertEqual(
                self.client.request("POST", "/charge", data, idem=key).status, 500
            )
            self.assertFalse(processor.entries(self.client_id))
            self.process.environment.pop("PROCESSOR_TEST_PAUSE_AFTER_PREPARE")
            self.process.restart(hard=True)
            eventually(
                lambda: (
                    self.client.request("POST", "/charge", data, idem=key).status == 200
                ),
                description="absent provider-key lookup then safe dispatch",
            )
            entry = self.provider_entry(processor, data["id"])
            self.assertEqual(entry["effects"], 1)
            self.assertIn((self.client_id, entry["key"]), processor.lookups)

    def test_complete_processor_success_never_becomes_cached_validation_error(self):
        with self.http_processor() as processor:
            payee = "finalize-" + self.uid()
            with self.db() as db:
                db.execute(
                    "INSERT INTO accounts(client_id,account_id,currency,available_e5,balance_e5) VALUES(%s,%s,'BRL',0,0)",
                    (self.client_id, payee),
                )
            data = self.fixture("payout")
            data["payee"]["account"]["account_id"] = payee
            key = self.uid()
            barrier = processor.block_next()
            with ThreadPoolExecutor(max_workers=1) as pool:
                pending = pool.submit(
                    self.client.request, "POST", "/payout", data, idem=key
                )
                try:
                    self.assertTrue(barrier.entered.wait(3))
                    with self.db() as db:
                        db.execute(
                            "DELETE FROM accounts WHERE client_id=%s AND account_id=%s",
                            (self.client_id, payee),
                        )
                finally:
                    barrier.release.set()
                self.assertEqual(
                    pending.result().status,
                    500,
                    "an already confirmed provider effect cannot become a final local400",
                )
            entry = self.provider_entry(processor, data["id"], "payout")
            with self.db() as db:
                self.assertFalse(
                    db.execute(
                        "SELECT 1 FROM operations WHERE client_id=%s AND idem_key=%s",
                        (self.client_id, key),
                    ).fetchall()
                )
                self.assertTrue(
                    db.execute(
                        "SELECT 1 FROM intent_reservations WHERE intent_id=%s",
                        (entry["key"],),
                    ).fetchall()
                )
                db.execute(
                    "INSERT INTO accounts(client_id,account_id,currency,available_e5,balance_e5) VALUES(%s,%s,'BRL',0,0)",
                    (self.client_id, payee),
                )
            eventually(
                lambda: (
                    self.client.request("POST", "/payout", data, idem=key).status == 200
                ),
                description="reconcile known provider success after local repair",
            )
            self.assertEqual(
                self.provider_entry(processor, data["id"], "payout")["effects"], 1
            )
            with self.db() as db:
                self.assertEqual(
                    db.execute(
                        "SELECT balance_e5 FROM accounts WHERE client_id=%s AND account_id=%s",
                        (self.client_id, payee),
                    ).fetchone()[0],
                    data["amount"]["value"],
                )

    def test_complete_processor_manual_refund_payout_and_declines(self):
        with self.http_processor() as processor:
            for method, expected in [("DEMO_SYNC", 200), ("DEMO_CAPTURE_SOFT", 422)]:
                data, response = self.charge(
                    funding_method=method, capture_method="MANUAL"
                )
                self.assertEqual(self.lookup(data["id"])["status"], "AUTHORIZED")
                result = self.client.request(
                    "POST",
                    f"/charge/{data['id']}/capture",
                    {
                        "psp_reference": response["psp_reference"],
                        "amount_to_capture": data["amount"],
                    },
                    idem=self.uid(),
                )
                self.assert_response(result, "/charge/{id}/capture", expected)
                if expected == 422:
                    self.assertEqual(result.body["detail"], "BANK_SOFT_ERROR")
                else:
                    refund = self.refund_request(data, response)
                    result = self.assert_response(
                        self.client.request("POST", "/refund", refund, idem=self.uid()),
                        "/refund",
                        201,
                    )
                    self.assertEqual(result["status"], "CONFIRMED")
            data, _ = self.charge(capture_method="MANUAL")
            self.assert_response(
                self.client.request(
                    "POST", f"/charge/{data['id']}/cancel", {}, idem=self.uid()
                ),
                "/charge/{id}/cancel",
                200,
            )
            for method, status in [
                ("DEMO_SYNC", 200),
                ("DEMO_REJECT_BANK_HARD_ERROR", 422),
            ]:
                payout = self.fixture("payout", funding_method=method)
                self.assert_response(
                    self.client.request("POST", "/payout", payout, idem=self.uid()),
                    "/payout",
                    status,
                )
            self.assertTrue(
                all(e["effects"] == 1 for e in processor.entries(self.client_id))
            )

    def test_complete_processor_refund_reversal_event_order_and_reservations(self):
        with self.http_processor() as processor:
            data, _ = self.charge(funding_method="DEMO_PENDING")
            entry = self.provider_entry(processor, data["id"])
            # An explicit same-key retry resumes an inspectable pending intent,
            # while still replaying the immutable initial PENDING response.
            with self.db() as db:
                db.execute(
                    "UPDATE provider_intents SET status='RECONCILE' WHERE id=%s",
                    (entry["key"],),
                )
                key = db.execute(
                    "SELECT idem_key FROM provider_keys WHERE intent_id=%s",
                    (entry["key"],),
                ).fetchone()[0]
            processor.set_result(self.client_id, entry["key"], "CONFIRMED", version=2)
            replay = self.assert_response(
                self.client.request("POST", "/charge", data, idem=key), "/charge", 200
            )
            self.assertEqual(replay["state"]["value"], "PENDING")
            self.accepted("chargeCallback", data["id"])
            with self.db() as db:
                self.assertEqual(
                    db.execute(
                        "SELECT status FROM provider_intents WHERE id=%s",
                        (entry["key"],),
                    ).fetchone()[0],
                    "DONE",
                )
            original = {"psp_reference": self.lookup(data["id"])["merchant_reference"]}
            refund = self.refund_request(data, original)
            response = self.assert_response(
                self.client.request("POST", "/refund", refund, idem=self.uid()),
                "/refund",
                201,
            )
            self.assertEqual(response["status"], "PENDING")
            entry = self.provider_entry(processor, data["id"], "initRefund")
            with self.db() as db:
                self.assertEqual(
                    db.execute(
                        "SELECT credit_e5 FROM intent_reservations WHERE intent_id=%s",
                        (entry["key"],),
                    ).fetchone()[0],
                    data["amount"]["value"],
                )
            processor.set_result(
                self.client_id, entry["key"], "CONFIRMED", version=2, terminal=False
            )
            self.accepted("updateRefund", refund["id"])
            value = processor.set_result(
                self.client_id, entry["key"], "REJECTED", version=3
            )

            def send():
                return (
                    self.client.request(
                        "POST",
                        "/adapter/events",
                        value,
                        signed=False,
                        outside=True,
                        headers={"Authorization": "Bearer " + processor.token},
                    ).status
                    == 204
                )

            eventually(send, description="refund reversal provider event")
            events = eventually(
                lambda: (
                    es
                    if len(es := self.mock.matching("updateRefund", refund["id"])) == 2
                    else None
                )
            )
            self.assertEqual(
                [e["payload"]["status"] for e in events], ["CONFIRMED", "REJECTED"]
            )
            self.assertTrue(send())
            with self.db() as db:
                self.assertEqual(
                    db.execute(
                        "SELECT count(*) FROM refund_reversals WHERE client_id=%s AND refund_id=%s",
                        (self.client_id, refund["id"]),
                    ).fetchone()[0],
                    1,
                )
                self.assertFalse(
                    db.execute(
                        "SELECT 1 FROM intent_reservations WHERE intent_id=%s",
                        (entry["key"],),
                    ).fetchall()
                )

    def test_complete_processor_parallel_io_and_bounded_duplicate(self):
        with self.http_processor() as processor:
            # Keep provider responses blocked long enough to observe lock lifetime,
            # independently of a loaded CI runner's scheduling latency.
            self.process.environment.update(
                PROCESSOR_TIMEOUT_MS="4000", PROCESSOR_LEASE_MS="6000"
            )
            self.process.restart()
            barriers = [processor.block_next(), processor.block_next()]
            requests = [self.fixture("charge"), self.fixture("charge")]
            keys = [self.uid(), self.uid()]
            with ThreadPoolExecutor(max_workers=3) as pool:
                futures = [
                    pool.submit(self.client.request, "POST", "/charge", b, idem=k)
                    for b, k in zip(requests, keys)
                ]
                try:
                    self.assertTrue(
                        all(b.entered.wait(2) for b in barriers),
                        "same-client independent charges must both enter processor before either is released",
                    )
                    # Background workers may legitimately open short transactions.
                    # Reject a transaction that survives across the provider barrier,
                    # rather than a random instantaneous snapshot of unrelated work.
                    with self.db() as db:
                        boundary = db.execute("SELECT clock_timestamp()").fetchone()[0]
                    time.sleep(0.15)
                    with self.db() as db:
                        active = db.execute(
                            "SELECT pid FROM pg_stat_activity WHERE application_name=%s AND xact_start<=%s AND state IN ('idle in transaction','active')",
                            (self.client_id, boundary),
                        ).fetchall()
                        self.assertEqual(
                            active,
                            [],
                            "no SQL transaction may span the blocked provider interval",
                        )
                        # A lock held across HTTP would prevent this independent row
                        # lock. Permit short worker activity, not a full provider wait.
                        with db.transaction():
                            db.execute("SET LOCAL lock_timeout='150ms'")
                            db.execute(
                                "SELECT account_id FROM accounts WHERE client_id=%s AND account_id='demo-account' AND currency='BRL' FOR UPDATE",
                                (self.client_id,),
                            ).fetchall()
                        self.assertEqual(
                            db.execute(
                                "SELECT count(*) FROM intent_reservations WHERE client_id=%s AND debit_e5>0",
                                (self.client_id,),
                            ).fetchone()[0],
                            2,
                        )
                    started = time.monotonic()
                    duplicate = self.client.request(
                        "POST", "/charge", requests[0], idem=keys[0]
                    )
                    self.assertEqual(duplicate.status, 500)
                    self.assertLess(time.monotonic() - started, 0.8)
                finally:
                    for barrier in barriers:
                        barrier.release.set()
                for future in futures:
                    self.assert_response(future.result(), "/charge", 200)
            self.assertEqual(
                [e["effects"] for e in processor.entries(self.client_id)], [1, 1]
            )
            self.assert_response(
                self.client.request("POST", "/charge", requests[0], idem=keys[0]),
                "/charge",
                200,
            )

    def test_complete_processor_commit_then_gateway_crash_recovers(self):
        with self.http_processor() as processor:
            barrier = processor.block_next()
            data, key = self.fixture("charge"), self.uid()
            with ThreadPoolExecutor(max_workers=1) as pool:
                pending = pool.submit(
                    self.client.request, "POST", "/charge", data, idem=key
                )
                try:
                    self.assertTrue(barrier.entered.wait(3))
                    self.process.restart(hard=True)
                finally:
                    barrier.release.set()
                try:
                    pending.result()
                except OSError:
                    pass
            response = eventually(
                lambda: (
                    r
                    if (
                        r := self.client.request("POST", "/charge", data, idem=key)
                    ).status
                    == 200
                    else None
                ),
                description="fenced provider-key recovery",
            )
            self.assertEqual(response.body["state"]["value"], "CONFIRMED")
            entry = self.provider_entry(processor, data["id"])
            self.assertEqual(entry["effects"], 1)
            self.assertIn((self.client_id, entry["key"]), processor.lookups)
            self.assertEqual(
                self.lookup(data["id"])["merchant_reference"],
                entry["result"]["psp_reference"],
            )

    def test_complete_processor_lost_response_reconciles_without_frozen_5xx(self):
        with self.http_processor() as processor:
            processor.drop_responses = 1
            data, key = self.fixture("charge"), self.uid()
            self.assertEqual(
                self.client.request("POST", "/charge", data, idem=key).status, 500
            )
            result = eventually(
                lambda: (
                    r
                    if (
                        r := self.client.request("POST", "/charge", data, idem=key)
                    ).status
                    == 200
                    else None
                ),
                description="lookup after transport loss",
            )
            entry = self.provider_entry(processor, data["id"])
            self.assertEqual(entry["effects"], 1)
            self.assertEqual(
                result.body["psp_reference"], entry["result"]["psp_reference"]
            )
            with self.db() as db:
                encrypted = db.execute(
                    "SELECT request_encrypted FROM provider_intents WHERE client_id=%s AND provider_key=%s",
                    (self.client_id, entry["key"]),
                ).fetchone()[0]
                self.assertNotIn(data["id"], encrypted)
                self.assertFalse(
                    db.execute(
                        "SELECT 1 FROM operations WHERE client_id=%s AND status>=500",
                        (self.client_id,),
                    ).fetchall()
                )

    def test_complete_processor_pending_events_and_stale_versions(self):
        with self.http_processor() as processor:
            data, response = self.charge(funding_method="DEMO_PENDING")
            self.assertEqual(response["state"]["value"], "PENDING")
            entry = self.provider_entry(processor, data["id"])
            value = processor.set_result(
                self.client_id,
                entry["key"],
                "REJECTED",
                version=2,
                detail="PAYMENT_NETWORK_ERROR",
            )

            def event(payload):
                return self.client.request(
                    "POST",
                    "/adapter/events",
                    payload,
                    signed=False,
                    outside=True,
                    headers={"Authorization": "Bearer " + processor.token},
                )

            self.assertEqual(
                self.client.request(
                    "POST", "/adapter/events", value, signed=False, outside=True
                ).status,
                404,
            )
            with self.db() as db:
                generation = db.execute(
                    "UPDATE provider_intents SET lease_until=now()+interval '30 seconds' WHERE id=%s RETURNING generation",
                    (entry["key"],),
                ).fetchone()[0]
            for malformed in (
                dict(value, version="2"),
                dict(value, version=True),
                dict(value, version=0),
                dict(value, terminal="false"),
            ):
                self.assertEqual(event(malformed).status, 400)
            with self.db() as db:
                self.assertEqual(
                    db.execute(
                        "SELECT generation FROM provider_intents WHERE id=%s",
                        (entry["key"],),
                    ).fetchone()[0],
                    generation,
                )
                db.execute(
                    "UPDATE provider_intents SET lease_until=now()-interval '1 second' WHERE id=%s",
                    (entry["key"],),
                )

            eventually(
                lambda: event(value).status == 204,
                description="authenticated provider event",
            )
            callback = self.accepted("chargeCallback", data["id"])[0]
            self.assertEqual(
                callback["payload"]["state"],
                {"value": "REJECTED", "detail": "PAYMENT_NETWORK_ERROR"},
            )
            self.assertEqual(event(value).status, 204)
            self.assertEqual(event(dict(value, state="PENDING", version=1)).status, 204)
            self.assertEqual(self.lookup(data["id"])["status"], "REJECTED")
            self.assertEqual(len(self.mock.matching("chargeCallback", data["id"])), 1)
