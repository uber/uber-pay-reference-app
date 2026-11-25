"""Acceptance evidence for decided U1-U15 reference policies, not Uber approval."""

from datetime import timedelta, timezone
import json
from pathlib import Path
import tempfile

import psycopg

from .sftp_fixture import SftpFixture
from tools.settlement import DirectorySink, SettlementStore, SettlementWindow
from tools.settlement_sftp import SftpSink


class OwnerSuite:
    def test_owner_partial_capture_http_releases_remainder_and_limits_refunds(self):
        with self.http_processor() as processor:
            invalid = self.fixture("charge")
            invalid["source"]["id"] = ""
            key = self.uid()
            for _ in range(2):
                self.assert_response(
                    self.client.request("POST", "/charge", invalid, idem=key),
                    "/charge",
                    400,
                )
            self.assertFalse(
                any(
                    e["resource"] == invalid["id"]
                    for e in processor.entries(self.client_id)
                ),
                "known missing routing metadata must fail before a provider effect",
            )
            token, _ = self.access()
            balance = {"account_id": "demo-account", "funding_method": "DEMO_SYNC"}
            before = self.assert_response(
                self.client.request("POST", "/balance", balance, token=token),
                "/balance",
                200,
            )
            original, response = self.charge(capture_method="MANUAL")
            capture = {
                "amount_to_capture": {"value": 400000, "currency": "BRL"},
                "psp_reference": response["psp_reference"],
            }
            key = self.uid()
            self.assert_response(
                self.client.request(
                    "POST", f"/charge/{original['id']}/capture", capture, idem=key
                ),
                "/charge/{id}/capture",
                200,
            )
            # Same key is replay, another key cannot capture the released remainder.
            self.assert_response(
                self.client.request(
                    "POST", f"/charge/{original['id']}/capture", capture, idem=key
                ),
                "/charge/{id}/capture",
                200,
            )
            self.assert_response(
                self.client.request(
                    "POST",
                    f"/charge/{original['id']}/capture",
                    capture,
                    idem=self.uid(),
                ),
                "/charge/{id}/capture",
                409,
            )
            found = self.lookup(original["id"])
            self.assertEqual(found["amount"], capture["amount_to_capture"])
            after = self.assert_response(
                self.client.request("POST", "/balance", balance, token=token),
                "/balance",
                200,
            )
            self.assertEqual(
                before["available_balance"]["value"]
                - after["available_balance"]["value"],
                400000,
            )
            self.assertEqual(
                before["balance"]["value"] - after["balance"]["value"], 400000
            )
            with self.db() as db:
                self.assertEqual(
                    db.execute(
                        "SELECT authorized_e5,amount_e5 FROM transactions WHERE client_id=%s AND kind='charge' AND id=%s",
                        (self.client_id, original["id"]),
                    ).fetchone(),
                    (1000000, 400000),
                )
            refund = self.refund_request(
                original, response, amount=capture["amount_to_capture"]
            )
            self.assert_response(
                self.client.request("POST", "/refund", refund, idem=self.uid()),
                "/refund",
                201,
            )
            excess = self.refund_request(
                original, response, amount={"value": 1, "currency": "BRL"}
            )
            self.assert_response(
                self.client.request("POST", "/refund", excess, idem=self.uid()),
                "/refund",
                400,
            )

            rejected, ref = self.charge(
                funding_method="DEMO_CAPTURE_HARD", capture_method="MANUAL"
            )
            self.assert_response(
                self.client.request(
                    "POST",
                    f"/charge/{rejected['id']}/capture",
                    {
                        "amount_to_capture": {"value": 400000, "currency": "BRL"},
                        "psp_reference": ref["psp_reference"],
                    },
                    idem=self.uid(),
                ),
                "/charge/{id}/capture",
                422,
            )
            after_reject = self.assert_response(
                self.client.request("POST", "/balance", balance, token=token),
                "/balance",
                200,
            )
            self.assertEqual(
                after_reject,
                before,
                "declining a partial capture releases the full original hold",
            )

    def test_owner_provider_deposit_amount_update_is_ordered_and_bounded(self):
        with self.http_processor() as processor:
            data, response = self.charge(funding_method="DEMO_DEPOSIT")
            self.assertEqual(response["state"]["value"], "PENDING")
            entry = self.provider_entry(processor, data["id"])
            processor.set_result(
                self.client_id,
                entry["key"],
                "CONFIRMED",
                version=2,
                amount={"value": 500000, "currency": "BRL"},
            )
            self.accepted("confirmDeposit", data["id"])
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
            self.assertFalse(
                self.mock.matching("chargeCallback", data["id"], accepted=False)
            )
            # An oversized provider update cannot over-debit or release protection.
            bad, _ = self.charge(funding_method="DEMO_DEPOSIT")
            item = self.provider_entry(processor, bad["id"])
            value = processor.set_result(
                self.client_id,
                item["key"],
                "CONFIRMED",
                version=2,
                amount={"value": 2000000, "currency": "BRL"},
            )
            self.client.request(
                "POST",
                "/adapter/events",
                value,
                signed=False,
                outside=True,
                headers={"Authorization": "Bearer " + processor.token},
            )
            with self.db() as db:
                self.assertEqual(
                    db.execute(
                        "SELECT state,amount_e5 FROM transactions WHERE client_id=%s AND kind='charge' AND id=%s",
                        (self.client_id, bad["id"]),
                    ).fetchone(),
                    ("PENDING", 1000000),
                )
            self.assertFalse(self.mock.matching("confirmDeposit", bad["id"]))
            processor.set_result(
                self.client_id,
                item["key"],
                "REJECTED",
                version=3,
                detail="USER_CANCELLED",
            )
            # Explicit retry also resumes any quarantined pending intent without a new effect.
            with self.db() as db:
                key = db.execute(
                    "SELECT idem_key FROM provider_keys WHERE intent_id=%s",
                    (item["key"],),
                ).fetchone()[0]
            self.client.request("POST", "/charge", bad, idem=key)
            self.accepted("cancelDeposit", bad["id"])

    def test_owner_settlement_journal_seal_and_real_sftp_delivery(self):
        with self.db() as db:
            start = (
                db.execute("SELECT clock_timestamp()")
                .fetchone()[0]
                .astimezone(timezone.utc)
            )
        source = "source-" + self.uid()
        with self.db() as db:
            db.execute(
                "INSERT INTO accounts(client_id,account_id,currency,available_e5,balance_e5) VALUES(%s,%s,'BRL',1000000,1000000)",
                (self.client_id, source),
            )
        account = {"type": "Account", "id": source, "owner_id": "synthetic-owner"}
        charge, response = self.charge(source=account)
        manual, _ = self.charge(source=account, capture_method="MANUAL")
        self.assert_response(
            self.client.request(
                "POST",
                f"/charge/{manual['id']}/capture",
                {"amount_to_capture": {"value": 400000, "currency": "BRL"}},
                idem=self.uid(),
            ),
            "/charge/{id}/capture",
            200,
        )
        refund = self.refund_request(
            charge, response, amount={"value": 200000, "currency": "BRL"}
        )
        self.assert_response(
            self.client.request("POST", "/refund", refund, idem=self.uid()),
            "/refund",
            201,
        )
        self.assertEqual(
            self.client.control("POST", "reverse-refund", {"id": refund["id"]}).status,
            200,
        )
        payout = self.fixture("payout", payer=account)
        self.assert_response(
            self.client.request("POST", "/payout", payout, idem=self.uid()),
            "/payout",
            200,
        )
        with self.db() as db:
            end = (
                db.execute("SELECT clock_timestamp()")
                .fetchone()[0]
                .astimezone(timezone.utc)
            )
            rows = db.execute(
                "SELECT event_type,amount_e5 FROM settlement_entries WHERE client_id=%s AND source_account_id=%s ORDER BY occurred_at",
                (self.client_id, source),
            ).fetchall()
            self.assertEqual(
                [row[1] for row in rows], [-1000000, -400000, 200000, -200000, -100000]
            )
            with self.assertRaises(psycopg.errors.CheckViolation):
                db.execute(
                    "UPDATE settlement_entries SET amount_e5=1 WHERE client_id=%s AND source_account_id=%s",
                    (self.client_id, source),
                )
        store, window = SettlementStore(self.database_url), SettlementWindow(start, end)
        batch = store.seal(self.client_id, window)
        repeated = store.seal(self.client_id, window)
        self.assertEqual(batch["manifest"], repeated["manifest"])
        item = next(
            f
            for f in json.loads(batch["manifest"])["files"]
            if f["source_account_id"] == source
        )
        self.assertEqual(item["entries"], 5)
        self.assertEqual(item["net_e5_by_currency"], {"BRL": "-1500000"})
        with self.assertRaises(ValueError):
            store.seal(
                self.client_id, SettlementWindow(start, end + timedelta(microseconds=1))
            )
        with tempfile.TemporaryDirectory() as directory:
            receipt = store.publish(
                batch["id"], DirectorySink(directory), "directory:" + directory
            )
            self.assertFalse(receipt["financial_acceptance"])
            server = SftpFixture(Path(directory) / "sftp").start()
            try:
                sink = SftpSink(**server.config())
                first = store.publish(batch["id"], sink, sink.destination)
                self.assertEqual(
                    first, store.publish(batch["id"], sink, sink.destination)
                )
                self.assertEqual(
                    (
                        server.directory / "reports" / batch["id"] / "manifest.json"
                    ).read_text(),
                    batch["manifest"],
                )
            finally:
                server.close()
        with self.db() as db:
            self.assertEqual(
                db.execute(
                    "SELECT count(*) FROM settlement_deliveries WHERE batch_id=%s",
                    (batch["id"],),
                ).fetchone()[0],
                2,
            )
            with self.assertRaises(psycopg.errors.CheckViolation):
                db.execute(
                    "UPDATE settlement_batches SET manifest='tampered' WHERE id=%s",
                    (batch["id"],),
                )
