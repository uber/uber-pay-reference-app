"""Final owner-policy, retention, accounting, scale and settlement qualification."""

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from urllib.parse import urlencode, urlsplit, parse_qs
import psycopg
from psycopg import sql
from tools.settlement import SettlementStore, SettlementWindow
from .wire import eventually


class FinalOwnerSuite:
    def owner2_balance(self, account="demo-account"):
        with self.db() as db:
            return db.execute(
                "SELECT available_e5,balance_e5 FROM accounts WHERE client_id=%s AND account_id=%s AND currency='BRL'",
                (self.client_id, account),
            ).fetchone()

    def owner2_authorize(self):
        return "/authorize?" + urlencode(
            dict(
                client_id=self.client_id,
                response_type="code",
                state="state+preserved",
                redirect_uri="http://127.0.0.1:9090/return",
                scope="charge",
                authorization_details=json.dumps(
                    [
                        dict(
                            type="authorization_detail",
                            locale="en-US",
                            funding_method="DEMO_SYNC",
                            country_iso2="BR",
                        )
                    ]
                ),
            )
        )

    def test_owner2_error_parity_and_persisted_expiry(self):
        data = self.fixture("payout")
        data["payee"]["account"]["account_id"] = "unknown-" + self.uid()
        r = self.client.request("POST", "/payout", data, idem=self.uid())
        self.assertEqual(
            (r.status, r.body["detail"]), (400, "INVALID_PAYMENT_DETAILS_ERROR")
        )
        r = self.client.request(
            "POST",
            "/charge",
            self.fixture("charge"),
            idem=self.uid(),
            signed=False,
            headers={"Accept-Language": "pt-BR"},
        )
        self.assertEqual(r.status, 401)
        self.assertIsNone(r.body)
        self.assertEqual(r.headers["content-language"], "pt-BR")
        with self.configured(WORKER_INTERVAL_MS="60000"):
            for op in ("capture", "cancel"):
                account = "expiry-" + self.uid()
                token, _ = self.access("charge")
                with self.db() as db:
                    db.execute(
                        "INSERT INTO accounts(client_id,account_id,currency,available_e5,balance_e5) VALUES(%s,%s,'BRL',100000000,100000000)",
                        (self.client_id, account),
                    )
                    db.execute(
                        "UPDATE tokens SET account_id=%s WHERE client_id=%s AND token_hash=%s",
                        (
                            account,
                            self.client_id,
                            hashlib.sha256(token.encode()).hexdigest(),
                        ),
                    )
                before = self.owner2_balance(account)
                data = self.fixture(
                    "charge",
                    capture_method="MANUAL",
                    authorization={
                        "type": "access_token_authorization",
                        "account_id": account,
                    },
                )
                charge = self.assert_response(
                    self.client.request(
                        "POST", "/charge", data, idem=self.uid(), token=token
                    ),
                    "/charge",
                    200,
                )
                with self.db() as db:
                    db.execute(
                        "UPDATE transactions SET expires_at=now()-interval '1 second' WHERE client_id=%s AND kind='charge' AND id=%s",
                        (self.client_id, data["id"]),
                    )
                body = {"psp_reference": charge["psp_reference"]}
                if op == "capture":
                    body["amount_to_capture"] = data["amount"]
                r = self.client.request(
                    "POST", f"/charge/{data['id']}/{op}", body, idem=self.uid()
                )
                self.assertEqual(r.status, 409)
                self.assertNotEqual(r.body["error_description"], "Invalid request")
                with self.db() as db:
                    self.assertEqual(
                        db.execute(
                            "SELECT state FROM transactions WHERE client_id=%s AND kind='charge' AND id=%s",
                            (self.client_id, data["id"]),
                        ).fetchone()[0],
                        "CANCELLED",
                    )
                self.assertEqual(self.owner2_balance(account), before)

    def test_owner2_authorize_server_error_and_bounded_abuse(self):
        with self.configured(
            PUBLIC_CONSENT_GLOBAL_PER_MINUTE="100", PUBLIC_CONSENT_IP_PER_MINUTE="3"
        ):
            with self.db() as db:
                db.execute(
                    "DELETE FROM public_buckets WHERE client_id=%s", (self.client_id,)
                )
                before = db.execute(
                    "SELECT count(*) FROM tokens WHERE client_id=%s AND kind='CONSENT'",
                    (self.client_id,),
                ).fetchone()[0]
            with ThreadPoolExecutor(16) as pool:
                monetary = [pool.submit(self.charge) for _ in range(4)]
                replies = list(
                    pool.map(
                        lambda _: self.client.request(
                            "GET", self.owner2_authorize(), signed=False
                        ),
                        range(40),
                    )
                )
            for future in monetary:
                future.result()
            self.assertEqual(sum(r.status == 200 for r in replies), 3)
            self.assertTrue(all(r.status in (200, 429) for r in replies))
            self.assertTrue(
                all(
                    r.headers.get("retry-after") == "60"
                    for r in replies
                    if r.status == 429
                )
            )
            with self.db() as db:
                self.assertEqual(
                    db.execute(
                        "SELECT count(*) FROM tokens WHERE client_id=%s AND kind='CONSENT'",
                        (self.client_id,),
                    ).fetchone()[0]
                    - before,
                    3,
                )
                db.execute(
                    "DELETE FROM public_buckets WHERE client_id=%s", (self.client_id,)
                )
            self.charge()
            name = "owner_fault_" + self.uid()
            with self.db() as db:
                db.execute(
                    sql.SQL(
                        "CREATE FUNCTION {}() RETURNS trigger LANGUAGE plpgsql AS $f$ BEGIN IF NEW.client_id={} THEN RAISE EXCEPTION 'synthetic fault';END IF;RETURN NEW;END $f$"
                    ).format(sql.Identifier(name), sql.Literal(self.client_id))
                )
                db.execute(
                    sql.SQL(
                        "CREATE TRIGGER {} BEFORE INSERT ON tokens FOR EACH ROW EXECUTE FUNCTION {}()"
                    ).format(sql.Identifier(name), sql.Identifier(name))
                )
            try:
                r = self.client.request("GET", self.owner2_authorize(), signed=False)
                self.assertEqual(r.status, 302)
                q = parse_qs(urlsplit(r.headers["location"]).query)
                self.assertEqual(q["error"], ["server_error"])
                self.assertEqual(q["state"], ["state+preserved"])
            finally:
                with self.db() as db:
                    db.execute(
                        sql.SQL("DROP TRIGGER {} ON tokens").format(
                            sql.Identifier(name)
                        )
                    )
                    db.execute(
                        sql.SQL("DROP FUNCTION {}()").format(sql.Identifier(name))
                    )

    def test_owner2_archive_preserves_replay_and_unresolved_work(self):
        data, key = self.fixture("charge"), self.uid()
        original = self.client.request("POST", "/charge", data, idem=key)
        self.assert_response(original, "/charge", 200)
        done, pending = self.uid(), self.uid()
        with self.db() as db:
            db.execute(
                "UPDATE operations SET created_at=now()-interval '8 days' WHERE client_id=%s AND idem_key=%s",
                (self.client_id, key),
            )
            for identity, status in ((done, "DONE"), (pending, "FAILED")):
                db.execute(
                    "INSERT INTO jobs(id,client_id,tx_kind,tx_id,job_type,callback_name,status,retry_started_at,last_error) VALUES(%s,%s,'test',%s,'CALLBACK','chargeCallback',%s,now()-interval '8 days','synthetic')",
                    (identity, self.client_id, identity, status),
                )
            expired = hashlib.sha256(self.uid().encode()).hexdigest()
            db.execute(
                "INSERT INTO tokens(token_hash,kind,client_id,account_id,scope,expires_at) VALUES(%s,'CONSENT',%s,'demo-account','charge',now()-interval '1 second')",
                (expired, self.client_id),
            )
            counts = db.execute(
                "SELECT reference_maintain(%s,200)", (self.client_id,)
            ).fetchone()[0]
            for k in ("archived_operations", "archived_jobs", "expired_tokens"):
                self.assertGreaterEqual(counts[k], 1)
            self.assertFalse(
                db.execute(
                    "SELECT 1 FROM operations WHERE client_id=%s AND idem_key=%s",
                    (self.client_id, key),
                ).fetchone()
            )
            self.assertTrue(
                db.execute(
                    "SELECT 1 FROM jobs WHERE id=%s AND status='FAILED'", (pending,)
                ).fetchone()
            )
            db.execute(
                "INSERT INTO jobs(id,client_id,tx_kind,tx_id,job_type,callback_name,status) VALUES(%s,%s,'test',%s,'CALLBACK','chargeCallback','PENDING')",
                (done, self.client_id, done),
            )
            self.assertFalse(
                db.execute("SELECT 1 FROM jobs WHERE id=%s", (done,)).fetchone()
            )
            with self.assertRaises(psycopg.errors.CheckViolation):
                db.execute(
                    "UPDATE operations_archive SET response_body='{}' WHERE client_id=%s AND idem_key=%s",
                    (self.client_id, key),
                )
        replay = self.client.request(
            "POST", "/charge", data, idem=key, headers={"Accept-Language": "pt-BR"}
        )
        self.assertEqual((replay.status, replay.raw), (original.status, original.raw))
        self.assertEqual(replay.headers["content-language"], "en-US")
        self.assertEqual(
            self.client.request(
                "POST",
                "/charge",
                dict(data, description="Changed replay identity"),
                idem=key,
            ).status,
            400,
        )

    def test_owner2_runtime_guard_and_stable_instrument_rotation(self):
        with self.db() as db:
            runtime, key = db.execute(
                "SELECT runtime,fingerprint_key_hash FROM runtime_bindings WHERE client_id=%s",
                (self.client_id,),
            ).fetchone()
            for lang, fingerprint in (
                ("go" if runtime != "go" else "java", key),
                (runtime, "changed-key"),
            ):
                with self.assertRaises(psycopg.errors.CheckViolation):
                    db.execute(
                        "SELECT reference_bind_runtime(%s,%s,%s)",
                        (self.client_id, lang, fingerprint),
                    )

        def grant(secret):
            r = self.client.request(
                "POST",
                "/token",
                dict(
                    client_id=self.client_id,
                    client_secret=secret,
                    grant_type="urn:uberpay:params:oauth:grant-type:wallet-grant",
                    country_iso2="BR",
                    funding_method="DEMO_SYNC",
                    scope="charge",
                    wallet_reference_data={"payment_identifier": "demo-account"},
                ),
            )
            self.assertEqual(r.status, 200)
            return r.body["authorization_details"][0]["fingerprint"]

        before = grant(self.client_secret)
        with self.configured(CLIENT_SECRET="rotated-synthetic-client-secret"):
            self.assertEqual(grant("rotated-synthetic-client-secret"), before)

    def test_owner2_strict_refund_default_and_lookup_scope(self):
        with self.configured(ENABLE_ASYNC_REFUND_REVERSAL="false"):
            data, charge = self.charge(funding_method="DEMO_REFUND_REVERSAL")
            refund = self.refund_request(data, charge)
            self.assertEqual(
                self.client.request("POST", "/refund", refund, idem=self.uid()).status,
                400,
            )
            data, charge = self.charge(funding_method="DEMO_REFUND_SYNC_REVERSAL")
            refund = self.refund_request(data, charge)
            self.assertEqual(
                self.client.request("POST", "/refund", refund, idem=self.uid()).body[
                    "status"
                ],
                "CONFIRMED",
            )
            events = self.accepted("updateRefund", refund["id"])
            self.assertEqual([e["payload"]["status"] for e in events], ["REJECTED"])
        payout = self.fixture("payout")
        self.assertEqual(
            self.client.request("POST", "/payout", payout, idem=self.uid()).status, 200
        )
        self.assertEqual(
            self.client.request("GET", "/find-transaction?id=" + payout["id"]).status,
            404,
        )

    def test_owner2_crashes_have_exactly_one_ledger_effect(self):
        for crash in ("lost-response", "post-commit-kill"):
            with self.subTest(fault=crash), self.http_processor() as processor:
                before = self.owner2_balance()
                data, key = self.fixture("charge"), self.uid()
                if crash == "lost-response":
                    processor.drop_responses = 1
                    self.assertEqual(
                        self.client.request("POST", "/charge", data, idem=key).status,
                        500,
                    )
                else:
                    barrier = processor.block_next()
                    with ThreadPoolExecutor(1) as pool:
                        pending = pool.submit(
                            self.client.request, "POST", "/charge", data, idem=key
                        )
                        try:
                            self.assertTrue(barrier.entered.wait(4))
                            self.process.restart(hard=True)
                        finally:
                            barrier.release.set()
                        try:
                            pending.result()
                        except OSError:
                            pass
                eventually(
                    lambda: (
                        r
                        if (
                            r := self.client.request("POST", "/charge", data, idem=key)
                        ).status
                        == 200
                        else None
                    )
                )
                entry = self.provider_entry(processor, data["id"])
                self.assertEqual(entry["effects"], 1)
                self.assertIn((self.client_id, entry["key"]), processor.lookups)
                self.assertEqual(
                    self.owner2_balance(),
                    tuple(v - data["amount"]["value"] for v in before),
                )
                with self.db() as db:
                    self.assertEqual(
                        db.execute(
                            "SELECT count(*) FROM settlement_entries WHERE client_id=%s AND tx_id=%s",
                            (self.client_id, data["id"]),
                        ).fetchone()[0],
                        1,
                    )

    def test_owner2_large_history_uses_expiry_index(self):
        prefix = "history-" + self.uid()
        try:
            with self.db() as db:
                db.execute(
                    "INSERT INTO transactions(client_id,kind,id,psp_reference,account_id,amount_e5,currency,funding_method,state,request_hash,response_status,response_body) SELECT %s,'charge',%s||g,%s||g,'demo-account',1,'BRL','DEMO_REJECT','REJECTED','synthetic-history',200,'{}' FROM generate_series(1,100000) g",
                    (self.client_id, prefix, prefix),
                )
                db.execute("ANALYZE transactions")
                plan = db.execute(
                    "EXPLAIN (FORMAT JSON) SELECT id FROM transactions WHERE client_id=%s AND state='AUTHORIZED' AND expires_at<=now()",
                    (self.client_id,),
                ).fetchone()[0]
                self.assertIn("transactions_expiring", json.dumps(plan))
            self.charge()
        finally:
            with self.db() as db:
                db.execute(
                    "DELETE FROM transactions WHERE client_id=%s AND id LIKE %s",
                    (self.client_id, prefix + "%"),
                )

    def test_owner2_settlement_lifecycles_and_concurrent_seals(self):
        source = dict(
            type="Account", id="source-" + self.uid(), owner_id="synthetic-owner"
        )
        start = self.db_now()
        pending, response = self.charge(funding_method="DEMO_PENDING", source=source)
        self.accepted("chargeCallback", pending["id"])
        self.assertEqual(
            self.client.request("POST", "/charge", pending, idem=self.uid()).body[
                "psp_reference"
            ],
            response["psp_reference"],
        )
        deposit, response = self.charge(funding_method="DEMO_DEPOSIT", source=source)
        self.submit_action(self.action_page(response))
        self.accepted("confirmDeposit", deposit["id"])
        expired, _ = self.charge(capture_method="MANUAL", source=source)
        self.client.control("POST", "expire", {"id": expired["id"]})
        with self.db() as db:
            usd = self.uid()
            # Independent second-currency journal producer; the supplied demo profiles are BRL-only.
            db.execute(
                "INSERT INTO transactions(client_id,kind,id,psp_reference,source_account_id,account_id,amount_e5,currency,funding_method,state,request_hash,response_status,response_body) VALUES(%s,'charge',%s,%s,%s,'demo-account',100,'USD','DEMO_SYNC','CONFIRMED','synthetic-usd',200,'{}')",
                (self.client_id, usd, "psp-" + usd, source["id"]),
            )
        other_source = dict(source, id="source-" + self.uid())
        self.charge(source=other_source)
        end = self.db_now()
        store = SettlementStore(self.database_url)
        window = SettlementWindow(start, end)
        with ThreadPoolExecutor(2) as pool:
            batches = list(
                pool.map(lambda _: store.seal(self.client_id, window), range(2))
            )
        self.assertEqual(batches[0]["manifest"], batches[1]["manifest"])
        item = next(
            f
            for f in json.loads(batches[0]["manifest"])["files"]
            if f["source_account_id"] == source["id"]
        )
        other = next(
            f
            for f in json.loads(batches[0]["manifest"])["files"]
            if f["source_account_id"] == other_source["id"]
        )
        self.assertEqual(other["entries"], 1)
        self.assertEqual(other["net_e5_by_currency"], {"BRL": "-1000000"})
        self.assertEqual(item["entries"], 3)
        self.assertEqual(item["net_e5_by_currency"], {"BRL": "-2000000", "USD": "-100"})
        self.assertFalse(
            any(expired["id"] in f["content"] for f in batches[0]["files"])
        )
        with self.assertRaisesRegex(ValueError, "overlap"):
            store.seal(
                self.client_id, SettlementWindow(start - timedelta(microseconds=1), end)
            )

    def test_owner2_settlement_closing_barrier_uses_commit_clock(self):
        data = self.fixture(
            "charge",
            source=dict(
                type="Account", id="barrier-" + self.uid(), owner_id="synthetic-owner"
            ),
        )
        start = self.db_now()
        with self.db() as barrier, ThreadPoolExecutor(2) as pool:
            barrier.execute(
                "SELECT pg_advisory_lock(hashtextextended(%s,0))",
                ("settlement|" + self.client_id,),
            )
            future = pool.submit(
                self.client.request, "POST", "/charge", data, idem=self.uid()
            )
            try:

                def waiting():
                    with self.db() as db:
                        return db.execute(
                            "SELECT 1 FROM pg_stat_activity WHERE application_name=%s AND wait_event='advisory' LIMIT 1",
                            (self.client_id,),
                        ).fetchone()

                eventually(
                    waiting, description="producer waiting on settlement barrier"
                )
                boundary = self.db_now()
                sealing = pool.submit(
                    SettlementStore(self.database_url).seal,
                    self.client_id,
                    SettlementWindow(start, boundary),
                )
            finally:
                barrier.execute(
                    "SELECT pg_advisory_unlock(hashtextextended(%s,0))",
                    ("settlement|" + self.client_id,),
                )
            self.assertEqual(future.result().status, 200)
            batch = sealing.result()
        with self.db() as db:
            occurred, confirmed = db.execute(
                "SELECT e.occurred_at,t.updated_at FROM settlement_entries e JOIN transactions t ON t.client_id=e.client_id AND t.kind=e.tx_kind AND t.id=e.tx_id WHERE e.client_id=%s AND e.tx_id=%s",
                (self.client_id, data["id"]),
            ).fetchone()
        self.assertLessEqual(confirmed, boundary)
        self.assertGreaterEqual(occurred, boundary)
        self.assertFalse(any(data["id"] in f["content"] for f in batch["files"]))
