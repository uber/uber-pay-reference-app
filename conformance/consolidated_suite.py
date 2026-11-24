"""Independent regressions for the November 24 consolidated review."""

import hashlib
import json
import socket
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager

from psycopg.rows import dict_row

from . import contract
from .wire import eventually


class ConsolidatedSuite:
    def test_consolidated_oauth_grants_instrument_identity_and_error_redirects(self):
        import hmac
        from urllib.parse import urlencode, urlsplit, parse_qs

        grant = "urn:uberpay:params:oauth:grant-type:wallet-grant"
        body = {
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "grant_type": grant,
            "scope": "charge",
            "country_iso2": "BR",
            "funding_method": "DEMO_SYNC",
            "wallet_reference_data": {
                "payment_identifier": "demo-account",
                "display_name": "My wallet",
            },
        }
        responses = [
            self.assert_response(
                self.client.request("POST", "/token", body), "/token", 200, grant=grant
            )
            for _ in range(2)
        ]
        expected = hmac.new(
            self.client_secret.encode(),
            b"uberpay-instrument-v1|demo-account",
            hashlib.sha256,
        ).hexdigest()
        for response in responses:
            detail = response["authorization_details"][0]
            self.assertEqual(detail["fingerprint"], expected)
            self.assertEqual(detail["display_name"], "My wallet")
            self.assertIn("expires_in", response)
        for value, error in [
            (None, "invalid_request"),
            ("", "invalid_request"),
            (3, "invalid_request"),
            ("password", "unsupported_grant_type"),
        ]:
            request = {"client_id": self.client_id, "client_secret": self.client_secret}
            if value is not None:
                request["grant_type"] = value
            self.assertEqual(
                self.client.request("POST", "/token", request).body["error"], error
            )
        query = {
            "client_id": self.client_id,
            "response_type": "token",
            "scope": "charge",
            "redirect_uri": "http://127.0.0.1:9090/return",
            "authorization_details": json.dumps(
                [
                    {
                        "type": "payment_method",
                        "funding_method": "DEMO_SYNC",
                        "country_iso2": "BR",
                    }
                ]
            ),
        }
        result = self.client.request(
            "GET", "/authorize?" + urlencode(query), signed=False
        )
        params = parse_qs(
            urlsplit(result.headers["location"]).query, keep_blank_values=True
        )
        self.assertEqual(params["error"], ["unsupported_response_type"])
        self.assertNotIn("state", params)

    def test_consolidated_s8_signing_key_overlap(self):
        import tempfile
        from pathlib import Path

        old_id = self.client.key_id
        with tempfile.TemporaryDirectory() as directory:
            manifest = Path(directory) / "signing-keys.json"
            manifest.write_text(
                json.dumps(
                    {"rotation-key": self.process.environment["UBER_PUBLIC_KEY_FILE"]}
                )
            )
            with self.configured(UBER_SIGNING_KEYS_FILE=str(manifest)):
                try:
                    for key_id in (old_id, "rotation-key"):
                        self.client.key_id = key_id
                        self.charge()
                    self.client.key_id = "unknown-rotation-key"
                    data = self.fixture("charge")
                    self.assertEqual(
                        self.client.request(
                            "POST", "/charge", data, idem=self.uid()
                        ).status,
                        401,
                    )
                finally:
                    self.client.key_id = old_id

    @contextmanager
    def configured(self, **settings):
        before = self.process.environment.copy()
        for key, value in settings.items():
            if value is None:
                self.process.environment.pop(key, None)
            else:
                self.process.environment[key] = str(value)
        try:
            self.process.restart()
            yield
        finally:
            self.process.stop()
            self.process.environment.clear()
            self.process.environment.update(before)
            self.process.start()
            self.process.wait_ready()

    def test_consolidated_r1_transient_outage_survives_default_budget(self):
        with self.configured(CALLBACK_MAX_ATTEMPTS=None, CALLBACK_RETRY_MS="100"):
            data = self.fixture("charge", funding_method="DEMO_PENDING")
            self.mock.unavailable_until[("chargeCallback", data["id"])] = (
                time.monotonic() + 10
            )
            self.assert_response(
                self.client.request("POST", "/charge", data, idem=self.uid()),
                "/charge",
                200,
            )
            eventually(
                lambda: self.mock.matching("chargeCallback", data["id"]),
                timeout=23,
                description="callback after ten-second Uber outage",
            )
            events = self.mock.matching("chargeCallback", data["id"], accepted=False)
            self.assertGreater(len(events), 5)
            self.assertEqual(events[-1]["status"], 200)
            self.assertEqual(len({e["idempotency_key"] for e in events}), 1)

    def test_consolidated_r1_blocked_chain_operator_redrive(self):
        from tools.operations import inspect, redrive

        with self.configured(CALLBACK_MAX_ATTEMPTS="1"):
            data, charge = self.charge(funding_method="DEMO_REFUND_REVERSAL")
            refund = self.refund_request(data, charge)
            self.mock.fail_next("updateRefund", refund["id"], 503)
            self.assert_response(
                self.client.request("POST", "/refund", refund, idem=self.uid()),
                "/refund",
                201,
            )

            def blocked():
                with self.db() as db:
                    db.row_factory = dict_row
                    return next(
                        (
                            r
                            for r in inspect(db, self.client_id)[
                                "callback_chain_blocked"
                            ]
                            if r["tx_id"] == refund["id"] and r["blocked_events"] > 0
                        ),
                        None,
                    )

            first = eventually(
                blocked, description="inspectable blocked callback chain"
            )
            self.assertFalse(self.mock.matching("updateRefund", refund["id"]))
            with self.db() as db:
                db.row_factory = dict_row
                redrive(
                    db,
                    self.client_id,
                    "callback",
                    first["id"],
                    "Synthetic outage cleared",
                )
            events = eventually(
                lambda: (
                    events
                    if len(events := self.mock.matching("updateRefund", refund["id"]))
                    == 2
                    else None
                ),
                description="ordered confirmation and reversal after re-drive",
            )
            self.assertEqual(
                [e["payload"]["status"] for e in events], ["CONFIRMED", "REJECTED"]
            )

    def test_consolidated_s3_retry_after_and_sync_refund_reason(self):
        data = self.fixture("charge", funding_method="DEMO_PENDING")
        self.mock.fail_next("chargeCallback", data["id"], 429)
        self.mock.retry_after[("chargeCallback", data["id"])] = "2"
        self.assert_response(
            self.client.request("POST", "/charge", data, idem=self.uid()),
            "/charge",
            200,
        )
        self.accepted("chargeCallback", data["id"])
        events = self.mock.matching("chargeCallback", data["id"], accepted=False)
        self.assertGreaterEqual(events[1]["time"] - events[0]["time"], 1.9)
        original, charge = self.charge(funding_method="DEMO_REFUND_REJECT")
        refund = self.refund_request(original, charge)
        result = self.assert_response(
            self.client.request("POST", "/refund", refund, idem=self.uid()),
            "/refund",
            201,
        )
        self.assertEqual(result["status"], "REJECTED")
        self.assertTrue(0 < len(result["failed_reason"]) <= 256)

    def test_consolidated_r2_more_than_four_pending_are_fair(self):
        with self.http_processor() as processor:
            payments = [self.charge(funding_method="DEMO_PENDING")[0] for _ in range(7)]
            newest = payments[-1]
            entry = self.provider_entry(processor, newest["id"])
            processor.set_result(self.client_id, entry["key"], "CONFIRMED", version=2)
            self.accepted("chargeCallback", newest["id"])
            self.assertEqual(self.lookup(newest["id"])["status"], "CONFIRMED")
            self.assertGreater(len(set(processor.lookups)), 4)

    def test_consolidated_r3_provider_outage_recovers_automatically(self):
        with self.http_processor() as processor:
            data = [self.charge(funding_method="DEMO_PENDING")[0] for _ in range(3)]
            processor.unavailable_until = time.monotonic() + 3.5
            for request in data:
                entry = self.provider_entry(processor, request["id"])
                processor.set_result(
                    self.client_id, entry["key"], "CONFIRMED", version=2
                )
            time.sleep(3.6)
            for request in data:
                self.accepted("chargeCallback", request["id"])
                self.assertEqual(self.lookup(request["id"])["status"], "CONFIRMED")
            with self.db() as db:
                self.assertEqual(
                    db.execute(
                        "SELECT count(*) FROM provider_intents WHERE client_id=%s AND status='RECONCILE'",
                        (self.client_id,),
                    ).fetchone()[0],
                    0,
                )

    def test_consolidated_processor_finite_budget_retains_holds_and_redrives(self):
        from tools.operations import redrive

        with self.http_processor() as processor:
            with self.configured(PROCESSOR_RETRY_BUDGET_MS="800"):
                data, _ = self.charge(funding_method="DEMO_PENDING")
                entry = self.provider_entry(processor, data["id"])
                processor.unavailable_until = time.monotonic() + 10

                def reconciles():
                    with self.db() as db:
                        return db.execute(
                            "SELECT id FROM provider_intents WHERE client_id=%s AND provider_key=%s AND status='RECONCILE'",
                            (self.client_id, entry["key"]),
                        ).fetchone()

                intent = eventually(
                    reconciles, description="natural elapsed-budget exhaustion"
                )[0]
                with self.db() as db:
                    self.assertGreater(
                        db.execute(
                            "SELECT count(*) FROM resource_claims WHERE intent_id=%s",
                            (intent,),
                        ).fetchone()[0],
                        0,
                    )
                    self.assertEqual(
                        db.execute(
                            "SELECT state FROM transactions WHERE client_id=%s AND kind='charge' AND id=%s",
                            (self.client_id, data["id"]),
                        ).fetchone()[0],
                        "PENDING",
                    )
                processor.set_result(
                    self.client_id, entry["key"], "CONFIRMED", version=2
                )
                processor.unavailable_until = 0
                with self.db() as db:
                    db.row_factory = dict_row
                    redrive(
                        db,
                        self.client_id,
                        "intent",
                        intent,
                        "Provider restored after finite-budget test",
                    )
                self.accepted("chargeCallback", data["id"])
                self.assertEqual(self.lookup(data["id"])["status"], "CONFIRMED")
                self.assertEqual(
                    self.provider_entry(processor, data["id"])["effects"], 1
                )
                with self.db() as db:
                    self.assertEqual(
                        db.execute(
                            "SELECT count(*) FROM resource_claims WHERE intent_id=%s",
                            (intent,),
                        ).fetchone()[0],
                        0,
                    )

    def test_consolidated_r4_injected_authorization_cannot_override_owner(self):
        token, _ = self.access("charge refund")
        with self.db() as db:
            db.execute(
                "UPDATE tokens SET account_id='other-account' WHERE client_id=%s AND token_hash=%s",
                (self.client_id, hashlib.sha256(token.encode()).hexdigest()),
            )
        for op in ("capture", "cancel", "refund"):
            data, charge = self.charge(
                capture_method="AUTOMATIC" if op == "refund" else "MANUAL"
            )
            body = (
                self.refund_request(data, charge)
                if op == "refund"
                else {"psp_reference": charge["psp_reference"]}
            )
            body["authorization"] = {
                "type": "access_token_authorization",
                "account_id": "other-account",
            }
            if op == "capture":
                body["amount_to_capture"] = data["amount"]
            path = "/refund" if op == "refund" else f"/charge/{data['id']}/{op}"
            self.assertEqual(
                self.client.request(
                    "POST", path, body, idem=self.uid(), token=token
                ).status,
                400,
            )
            self.assertEqual(
                self.lookup(data["id"])["status"],
                "CONFIRMED" if op == "refund" else "AUTHORIZED",
            )

    def test_consolidated_r5_concurrent_key_conflict_has_one_provider_effect(self):
        with self.http_processor() as processor:
            for _ in range(6):
                key = self.uid()
                requests = [self.fixture("payout"), self.fixture("payout")]
                with ThreadPoolExecutor(2) as pool:
                    results = list(
                        pool.map(
                            lambda body: self.client.request(
                                "POST", "/payout", body, idem=key
                            ),
                            requests,
                        )
                    )
                self.assertEqual(sorted(r.status for r in results), [200, 409])
                ids = {r["id"] for r in requests}
                entries = [
                    e for e in processor.entries(self.client_id) if e["resource"] in ids
                ]
                self.assertEqual(len(entries), 1)
                self.assertEqual(entries[0]["effects"], 1)
                self.assertEqual(
                    sum(key == entries[0]["key"] for _, key in processor.posts), 1
                )
                with self.db() as db:
                    self.assertEqual(
                        db.execute(
                            "SELECT count(*) FROM transactions WHERE client_id=%s AND kind='payout' AND id=ANY(%s)",
                            (self.client_id, list(ids)),
                        ).fetchone()[0],
                        1,
                    )

    def test_consolidated_r6_published_payload_and_each_erratum(self):
        # Upstream's literal curl ALSO omits required id/authorization. Preserve
        # it as evidence; do not invent unsafe defaults for these required fields.
        original = json.loads(
            (
                contract.ROOT
                / "conformance/fixtures/published-authentication-charge.json"
            ).read_text()
        )
        self.assertNotIn("id", original)
        self.assertNotIn("authorization", original)
        literal = dict(original, funding_method="DEMO_SYNC")
        self.assertEqual(
            self.client.request("POST", "/charge", literal, idem=self.uid()).status, 400
        )
        data = dict(
            literal, id=self.uid(), authorization={"type": "web_view_authorization"}
        )
        self.assert_response(
            self.client.request("POST", "/charge", data, idem=self.uid()),
            "/charge",
            200,
        )
        for changes in (
            {"description": "I'm a deposit in sandbox."},
            {"description": "Pagamento São Paulo"},
            {"destination_owner": original["destination_owner"]},
            {"payment_data": original["payment_data"]},
        ):
            sample = self.fixture("charge", **changes)
            self.assert_response(
                self.client.request("POST", "/charge", sample, idem=self.uid()),
                "/charge",
                200,
            )
        sample = self.fixture("charge", description="x" * 257)
        self.assertEqual(
            self.client.request("POST", "/charge", sample, idem=self.uid()).status, 400
        )
        with self.configured(STRICT_DESCRIPTION_PATTERN="true"):
            sample = self.fixture("charge", description="Punctuation.")
            self.assertEqual(
                self.client.request("POST", "/charge", sample, idem=self.uid()).status,
                400,
            )

    def test_consolidated_r7_query_return_url_approve_and_deny(self):
        target = "uber://payments/deposit?correlation_id=" + self.uid() + "&source=qr"
        for funding in ("DEMO_WEBVIEW", "DEMO_QR"):
            for decision in ("approve", "deny"):
                _, response = self.charge(funding_method=funding, return_url=target)
                result = self.submit_action(self.action_page(response), decision)
                self.assertEqual(result.status, 302)
                self.assertEqual(result.headers["location"], target)
        for target in (
            "javascript:alert(1)",
            "https://attacker.invalid/",
            "uber://payments.evil/deposit",
        ):
            data = self.fixture("charge", return_url=target)
            self.assertEqual(
                self.client.request("POST", "/charge", data, idem=self.uid()).status,
                400,
            )

    def test_consolidated_d1_cancelled_lookup_flag_and_d2_single_reversal(self):
        data, charge = self.charge(capture_method="MANUAL")
        self.assert_response(
            self.client.request(
                "POST",
                f"/charge/{data['id']}/cancel",
                {"psp_reference": charge["psp_reference"]},
                idem=self.uid(),
            ),
            "/charge/{id}/cancel",
            200,
        )
        self.assertEqual(self.lookup(data["id"])["status"], "REJECTED")
        with self.configured(ENABLE_CANCELLED_LOOKUP="true"):
            self.assertEqual(self.lookup(data["id"])["status"], "CANCELLED")
        data, charge = self.charge(funding_method="DEMO_REFUND_SYNC_REVERSAL")
        refund = self.refund_request(data, charge)
        result = self.assert_response(
            self.client.request("POST", "/refund", refund, idem=self.uid()),
            "/refund",
            201,
        )
        self.assertEqual(result["status"], "CONFIRMED")
        events = self.accepted("updateRefund", refund["id"])
        self.assertEqual([e["payload"]["status"] for e in events], ["REJECTED"])

    def test_consolidated_m1_poison_completion_does_not_stall_client(self):
        data, _ = self.charge(funding_method="DEMO_DEPOSIT")
        with self.db() as db:
            db.execute(
                """UPDATE jobs SET status='PENDING',next_attempt_at=now(),
                payload=jsonb_set(jsonb_set(payload::jsonb,'{state}','"CONFIRMED"'),
                '{amount_e5}',%s::jsonb)::text
                WHERE client_id=%s AND tx_id=%s AND job_type='COMPLETION'""",
                (str(data["amount"]["value"] + 1), self.client_id, data["id"]),
            )
        other, _ = self.charge(funding_method="DEMO_PENDING")
        self.accepted("chargeCallback", other["id"])
        with self.db() as db:
            row = db.execute(
                "SELECT status,attempts,last_error FROM jobs WHERE client_id=%s AND tx_id=%s AND job_type='COMPLETION'",
                (self.client_id, data["id"]),
            ).fetchone()
        self.assertNotEqual(row[0], "DONE")
        self.assertGreater(row[1], 0)
        self.assertTrue(row[2])

    def test_consolidated_m7_bidirectional_payouts_use_consistent_account_order(self):
        with self.http_processor() as processor:
            requests = [self.fixture("payout") for _ in range(16)]
            for index, body in enumerate(requests):
                if index % 2:
                    body["payer"]["id"], body["payee"]["account"]["account_id"] = (
                        body["payee"]["account"]["account_id"],
                        body["payer"]["id"],
                    )
            with ThreadPoolExecutor(8) as pool:
                results = list(
                    pool.map(
                        lambda body: self.client.request(
                            "POST", "/payout", body, idem=self.uid()
                        ),
                        requests,
                    )
                )
            self.assertEqual([r.status for r in results], [200] * len(requests))
            self.assertEqual(len(processor.entries(self.client_id)), len(requests))
            self.assertTrue(
                all(e["effects"] == 1 for e in processor.entries(self.client_id))
            )

    def test_consolidated_tg2_uncertain_statuses_and_generation_fencing(self):
        with self.http_processor() as processor:
            with self.db() as db:
                before = db.execute(
                    "SELECT available_e5,balance_e5 FROM accounts WHERE client_id=%s AND account_id='demo-account' AND currency='BRL'",
                    (self.client_id,),
                ).fetchone()
            data = self.fixture("charge")
            barrier = processor.block_next()
            with ThreadPoolExecutor(1) as pool:
                future = pool.submit(
                    self.client.request, "POST", "/charge", data, idem=self.uid()
                )
                try:
                    self.assertTrue(barrier.entered.wait(4))
                    entry = self.provider_entry(processor, data["id"])
                    with self.db() as db:
                        db.execute(
                            "UPDATE provider_intents SET generation=generation+1,lease_until=now() WHERE client_id=%s AND provider_key=%s",
                            (self.client_id, entry["key"]),
                        )
                    processor.responses.extend([202, 503, 409])
                    eventually(
                        lambda: self.lookup(data["id"]),
                        timeout=8,
                        description="fenced recovery after uncertain HTTP responses",
                    )
                finally:
                    barrier.release.set()
                self.assertIn(future.result().status, (200, 500))
            self.assertEqual(self.provider_entry(processor, data["id"])["effects"], 1)
            self.assertIn((self.client_id, entry["key"]), processor.lookups)
            with self.db() as db:
                after = db.execute(
                    "SELECT available_e5,balance_e5 FROM accounts WHERE client_id=%s AND account_id='demo-account' AND currency='BRL'",
                    (self.client_id,),
                ).fetchone()
                self.assertEqual(
                    after, tuple(value - data["amount"]["value"] for value in before)
                )
                self.assertEqual(
                    db.execute(
                        "SELECT count(*) FROM settlement_entries WHERE client_id=%s AND tx_id=%s",
                        (self.client_id, data["id"]),
                    ).fetchone()[0],
                    1,
                )

    def test_consolidated_m10_read_idempotency_and_strict_json(self):
        one, _ = self.charge()
        two, _ = self.charge()
        key = self.uid()
        for body in (one, two):
            result = self.client.request(
                "GET", "/find-transaction?id=" + body["id"], idem=key
            )
            self.assertEqual(result.body["id"], body["id"])
        data = self.fixture("charge")
        result = self.client.request(
            "POST",
            "/charge",
            raw=json.dumps(data).encode() + b' {"x":1}',
            idem=self.uid(),
        )
        self.assertEqual(result.status, 400)

    def test_consolidated_m8_slow_uploads_leave_health_available(self):
        endpoint = self.client.base
        clients = []
        try:
            for _ in range(220):
                sock = socket.create_connection(
                    (endpoint.hostname, endpoint.port), timeout=3
                )
                sock.sendall(
                    b"POST /uberpay/v1/charge HTTP/1.1\r\nHost: localhost\r\nContent-Type: application/json\r\nContent-Length: 1000000\r\n\r\n{"
                )
                clients.append(sock)
            for _ in range(3):
                self.assertEqual(
                    self.client.request(
                        "GET", "/health", signed=False, outside=True
                    ).status,
                    200,
                )
                time.sleep(0.1)
        finally:
            for sock in clients:
                sock.close()
