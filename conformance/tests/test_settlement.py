import copy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import jsonschema
import paramiko

from conformance.sftp_fixture import SftpFixture
from tools.settlement import (
    DirectorySink,
    SettlementWindow,
    group_by_source,
    render,
    sha,
)
from tools.settlement_sftp import SftpSink


class SettlementTest(unittest.TestCase):
    def batch(self):
        now = datetime(2026, 1, 2, tzinfo=timezone.utc)
        window = SettlementWindow(now - timedelta(days=1), now)
        rows = [
            dict(
                source_account_id="source-a",
                tx_kind="charge",
                tx_id="charge-id",
                event_type="CONFIRMED",
                psp_reference="psp",
                currency="BRL",
                amount_e5=-9223372036854775807,
                occurred_at=now - timedelta(hours=2),
            ),
            dict(
                source_account_id="source-b",
                tx_kind="refund",
                tx_id="refund-id",
                event_type="CONFIRMED",
                psp_reference="psp-refund",
                currency="BRL",
                amount_e5=100,
                occurred_at=now - timedelta(hours=1),
            ),
        ]
        return render("client", window, rows), window, rows

    def test_source_grouping_requires_metadata(self):
        self.assertEqual(
            group_by_source([dict(source_account_id="a", id=1)]),
            {"a": [dict(source_account_id="a", id=1)]},
        )
        with self.assertRaises(ValueError):
            group_by_source([dict(id="missing")])

    def test_format_is_exact_deterministic_and_separated_by_source(self):
        batch, window, rows = self.batch()
        self.assertEqual(batch, render("client", window, list(reversed(rows))))
        self.assertEqual(len(batch["files"]), 2)
        first = json.loads(batch["files"][0]["content"])
        self.assertEqual(first["amount_e5"], "-9223372036854775807")
        self.assertEqual(batch["manifest_sha256"], sha(batch["manifest"]))
        self.assertFalse(json.loads(batch["manifest"])["financial_acceptance"])
        schema = json.loads(
            (
                Path(__file__).resolve().parents[2]
                / "contracts/settlement-reference.schema.json"
            ).read_text()
        )
        validator = jsonschema.Draft202012Validator(
            schema, format_checker=jsonschema.FormatChecker()
        )
        validator.validate(json.loads(batch["manifest"]))
        for file in batch["files"]:
            for line in file["content"].splitlines():
                validator.validate(json.loads(line))

        self.assertNotIn("name", first)

    def test_rejects_naive_future_windows_and_inexact_money(self):
        batch, window, rows = self.batch()
        with self.assertRaises(ValueError):
            SettlementWindow(datetime(2026, 1, 1), datetime(2026, 1, 2)).validate()
        with self.assertRaises(ValueError):
            SettlementWindow(
                datetime.now(timezone.utc),
                datetime.now(timezone.utc) + timedelta(days=1),
            ).validate()
        rows[0]["amount_e5"] = 0.1
        with self.assertRaises(ValueError):
            render("client", window, rows)

    def test_directory_delivery_replays_and_refuses_tampering(self):
        batch, _, _ = self.batch()
        with tempfile.TemporaryDirectory() as directory:
            sink = DirectorySink(directory)
            first = sink.publish(batch)
            self.assertEqual(first, sink.publish(batch))
            (Path(directory) / batch["id"] / batch["files"][0]["filename"]).write_text(
                "corrupt"
            )
            with self.assertRaises(ValueError):
                sink.publish(batch)
            bad = copy.deepcopy(batch)
            bad["manifest"] += " "
            with self.assertRaises(ValueError):
                sink.publish(bad)

    def test_real_sftp_transport_is_atomic_replayable_and_host_key_pinned(self):
        batch, _, _ = self.batch()
        with tempfile.TemporaryDirectory() as directory:
            server = SftpFixture(directory).start()
            try:
                config = server.config()
                sink = SftpSink(**config)
                one = sink.publish(batch)
                self.assertEqual(one, sink.publish(batch))
                target = Path(directory) / "reports" / batch["id"]
                self.assertEqual(
                    (target / "manifest.json").read_text(), batch["manifest"]
                )
                self.assertFalse(
                    any(p.name.startswith(".upload-") for p in target.parent.iterdir())
                )
                (target / batch["files"][0]["filename"]).write_text("corrupt")
                with self.assertRaises(ValueError):
                    sink.publish(batch)
                # A different pinned host key must fail, never AutoAddPolicy.
                wrong = paramiko.RSAKey.generate(2048)
                Path(config["known_hosts_file"]).write_text(
                    f"[127.0.0.1]:{server.port} {wrong.get_name()} {wrong.get_base64()}\n"
                )
                with self.assertRaises(paramiko.BadHostKeyException):
                    sink.publish(batch)
            finally:
                server.close()

    def test_sftp_recovers_lost_publish_ack_and_supports_encrypted_keys(self):
        batch, _, _ = self.batch()
        with tempfile.TemporaryDirectory() as directory:
            server = SftpFixture(directory).start()
            try:
                config = server.config()
                server.client_key.write_private_key_file(
                    config["private_key_file"], password="synthetic-test-passphrase"
                )
                config["passphrase_env"] = "REFERENCE_TEST_SFTP_PASSPHRASE"
                with patch.dict(
                    "os.environ",
                    {"REFERENCE_TEST_SFTP_PASSPHRASE": "synthetic-test-passphrase"},
                ):
                    sink = SftpSink(**config)
                server.drop_after_rename = True
                with self.assertRaises((EOFError, OSError, paramiko.SSHException)):
                    sink.publish(batch)
                self.assertTrue(
                    (
                        server.directory / "reports" / batch["id"] / "manifest.json"
                    ).exists()
                )
                receipt = sink.publish(batch)
                self.assertEqual(receipt["manifest_sha256"], batch["manifest_sha256"])
                self.assertEqual(
                    [p.name for p in (server.directory / "reports").iterdir()],
                    [batch["id"]],
                )
            finally:
                server.close()

    def test_sftp_requires_local_explicit_destination_and_private_key_permissions(self):
        with self.assertRaises(ValueError):
            SftpSink(
                host="example.com",
                port=22,
                username="x",
                private_key_file="missing",
                known_hosts_file="missing",
                root="/reports",
            )
        with tempfile.TemporaryDirectory() as directory:
            server = SftpFixture(directory)
            try:
                config = server.config()
                Path(config["private_key_file"]).chmod(0o644)
                with self.assertRaises(ValueError):
                    SftpSink(**config)
            finally:
                server.socket.close()
