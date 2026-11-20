"""U15 reference reconciliation export. Not a production Uber settlement format.

Canonical JSONL (decimal-string E5), one file per source account; manifest commits
all files. Seal a closed UTC period in SQL, publish outside SQL, then record a
transport receipt. A transport receipt is NOT financial settlement acceptance.
"""

import argparse
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
import os
import re
import sys
from pathlib import Path
import tempfile
from typing import Mapping, Protocol, Sequence
from urllib.parse import urlsplit

import psycopg
from psycopg.rows import dict_row

FORMAT = "uberpay-reference-settlement-jsonl-v1"
MAX_ENTRIES = 100000
MAX_FILE_BYTES = 64 * 1024 * 1024


def canonical(value):
    return (
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        + "\n"
    )


def sha(value):
    return hashlib.sha256(value.encode()).hexdigest()


def utc(value):
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError("Settlement boundaries require explicit UTC")
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True)
class SettlementWindow:
    start: datetime
    end: datetime
    cadence: timedelta = timedelta(days=1)

    def validate(self, now=None):
        utc(self.start)
        utc(self.end)
        if self.end <= self.start or self.cadence <= timedelta(0):
            raise ValueError("Invalid settlement window")
        if self.end > (now or datetime.now(timezone.utc)):
            raise ValueError("Cannot seal an open/future period")


class SettlementSink(Protocol):
    def publish(self, batch: Mapping) -> Mapping:
        """Publish immutable files and manifest; return a transport-only receipt."""
        ...


def group_by_source(transactions: Sequence[Mapping]) -> dict[str, list[Mapping]]:
    groups = defaultdict(list)
    for transaction in transactions:
        source = transaction.get("source_account_id")
        if not source:
            raise ValueError("Settlement source account is required")
        groups[source].append(transaction)
    return dict(groups)


def render(client, window, rows):
    """Pure, byte-deterministic format; no float money or spreadsheet formulas."""
    window.validate()
    if len(rows) > MAX_ENTRIES:
        raise ValueError("Reference export limit exceeded; split the reporting period")
    batch_id = sha(canonical([FORMAT, client, utc(window.start), utc(window.end)]))
    files, descriptions = [], []
    for source, entries in sorted(group_by_source(rows).items()):
        encoded, totals = [], defaultdict(int)
        for entry in sorted(
            entries,
            key=lambda r: (r["occurred_at"], r["tx_kind"], r["tx_id"], r["event_type"]),
        ):
            if not window.start <= entry["occurred_at"] < window.end:
                raise ValueError("Entry outside reporting period")
            if (
                entry["tx_kind"] not in {"charge", "refund", "payout"}
                or entry["event_type"] not in {"CONFIRMED", "REFUND_REVERSED"}
                or not re.fullmatch("[A-Z]{3}", entry["currency"])
            ):
                raise ValueError("Invalid entry kind/event/currency")
            if not all(
                isinstance(entry[k], str) and entry[k]
                for k in ("source_account_id", "tx_id", "psp_reference")
            ):
                raise ValueError("Entry identity required")
            amount = entry["amount_e5"]
            if type(amount) is not int or not -(2**63) < amount < 2**63 or amount == 0:
                raise ValueError("Settlement entry must be a nonzero signed int64")
            identity = [client, entry["tx_kind"], entry["tx_id"], entry["event_type"]]
            value = dict(
                event_id=sha(canonical(identity)),
                occurred_at=utc(entry["occurred_at"]),
                source_account_id=source,
                transaction_type=entry["tx_kind"],
                uber_id=entry["tx_id"],
                psp_reference=entry["psp_reference"],
                event_type=entry["event_type"],
                currency=entry["currency"],
                amount_e5=str(amount),
            )
            encoded.append(canonical(value))
            totals[entry["currency"]] += amount
        content = "".join(encoded)
        if len(content.encode()) > MAX_FILE_BYTES:
            raise ValueError("Reference file size limit exceeded")
        name = "source-" + sha(source) + ".jsonl"
        description = dict(
            source_account_id=source,
            filename=name,
            sha256=sha(content),
            entries=len(entries),
            net_e5_by_currency={k: str(v) for k, v in sorted(totals.items())},
        )
        descriptions.append(description)
        files.append(dict(description, content=content))
    manifest = canonical(
        dict(
            format_version=FORMAT,
            batch_id=batch_id,
            client_id=client,
            period_start=utc(window.start),
            period_end=utc(window.end),
            money_encoding="signed base-10 integer strings; debit negative, refund credit positive",
            financial_acceptance=False,
            files=descriptions,
        )
    )
    return dict(
        id=batch_id,
        client_id=client,
        manifest=manifest,
        manifest_sha256=sha(manifest),
        files=files,
    )


class SettlementStore:
    def __init__(self, database_url):
        if urlsplit(database_url).hostname not in {
            "localhost",
            "127.0.0.1",
            "::1",
            "db",
            "postgres",
        }:
            raise ValueError("Reference settlement refuses nonlocal databases")
        self.url = database_url

    def connect(self):
        return psycopg.connect(
            self.url,
            row_factory=dict_row,
            application_name="reference-settlement",
            options="-c timezone=UTC -c lock_timeout=1500 -c statement_timeout=5000",
        )

    @staticmethod
    def read(c, batch_id):
        batch = c.execute(
            "SELECT * FROM settlement_batches WHERE id=%s", (batch_id,)
        ).fetchone()
        if not batch:
            return None
        batch["files"] = c.execute(
            "SELECT * FROM settlement_batch_files WHERE batch_id=%s ORDER BY source_account_id",
            (batch_id,),
        ).fetchall()
        verify_batch(batch)
        return batch

    def seal(self, client, window):
        if not client:
            raise ValueError("Client required")
        window.validate()
        with self.connect() as c:
            c.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
                ("settlement|" + client,),
            )
            window.validate(
                c.execute("SELECT clock_timestamp() AS now").fetchone()["now"]
            )
            old = c.execute(
                "SELECT id FROM settlement_batches WHERE client_id=%s AND period_start=%s AND period_end=%s AND format_version=%s",
                (client, window.start, window.end, FORMAT),
            ).fetchone()
            if old:
                return self.read(c, old["id"])
            if c.execute(
                "SELECT 1 FROM settlement_batches WHERE client_id=%s AND period_start<%s AND period_end>%s",
                (client, window.end, window.start),
            ).fetchone():
                raise ValueError("Reporting periods must not overlap a sealed batch")
            rows = c.execute(
                "SELECT * FROM settlement_entries WHERE client_id=%s AND occurred_at>=%s AND occurred_at<%s ORDER BY occurred_at,tx_kind,tx_id,event_type LIMIT %s",
                (client, window.start, window.end, MAX_ENTRIES + 1),
            ).fetchall()
            batch = render(client, window, rows)
            c.execute(
                "INSERT INTO settlement_batches(id,client_id,period_start,period_end,format_version,manifest,manifest_sha256) VALUES(%s,%s,%s,%s,%s,%s,%s)",
                (
                    batch["id"],
                    client,
                    window.start,
                    window.end,
                    FORMAT,
                    batch["manifest"],
                    batch["manifest_sha256"],
                ),
            )
            for file in batch["files"]:
                c.execute(
                    "INSERT INTO settlement_batch_files(batch_id,source_account_id,filename,content,sha256,entry_count) VALUES(%s,%s,%s,%s,%s,%s)",
                    (
                        batch["id"],
                        file["source_account_id"],
                        file["filename"],
                        file["content"],
                        file["sha256"],
                        file["entries"],
                    ),
                )
            return self.read(c, batch["id"])

    def publish(self, batch_id, sink, destination):
        with self.connect() as c:
            batch = self.read(c, batch_id)
        if not batch:
            raise ValueError("Unknown batch")
        # Deliberately outside every SQL transaction/connection lifetime.
        receipt = dict(sink.publish(batch))
        if (
            receipt.get("manifest_sha256") != batch["manifest_sha256"]
            or receipt.get("batch_id") != batch_id
        ):
            raise ValueError("Delivery receipt does not identify the sealed manifest")
        receipt["financial_acceptance"] = False
        with self.connect() as c:
            c.execute(
                "INSERT INTO settlement_deliveries(batch_id,destination_hash,manifest_sha256,receipt) VALUES(%s,%s,%s,%s::jsonb) ON CONFLICT DO NOTHING",
                (
                    batch_id,
                    sha(destination),
                    batch["manifest_sha256"],
                    canonical(receipt),
                ),
            )
        return receipt


def verify_batch(batch):
    if sha(batch["manifest"]) != batch["manifest_sha256"]:
        raise ValueError("Manifest checksum mismatch")
    manifest = json.loads(batch["manifest"])
    if manifest["batch_id"] != batch["id"] or manifest["format_version"] != FORMAT:
        raise ValueError("Manifest identity mismatch")
    files = {f["filename"]: f for f in batch["files"]}
    if len(files) != len(batch["files"]) or len(files) != len(manifest["files"]):
        raise ValueError("Manifest/file count mismatch")
    for item in manifest["files"]:
        file = files[item["filename"]]
        if (
            item["filename"] != "source-" + sha(item["source_account_id"]) + ".jsonl"
            or file["source_account_id"] != item["source_account_id"]
            or sha(file["content"]) != item["sha256"]
            or file["sha256"] != item["sha256"]
        ):
            raise ValueError("File checksum or identity mismatch")
    return batch


def payloads(batch):
    verify_batch(batch)
    return {f["filename"]: f["content"].encode() for f in batch["files"]} | {
        "manifest.json": batch["manifest"].encode()
    }


class DirectorySink:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.root.stat().st_mode & 0o077:
            raise ValueError("Reference output directory must be private (0700)")

    def publish(self, batch):
        expected = payloads(batch)
        target = self.root / batch["id"]
        if not target.exists():
            temporary = Path(tempfile.mkdtemp(prefix=".upload-", dir=self.root))
            try:
                for name, data in expected.items():
                    path = temporary / name
                    with path.open("xb") as out:
                        os.chmod(path, 0o600)
                        out.write(data)
                        out.flush()
                        os.fsync(out.fileno())
                try:
                    temporary.rename(target)
                except OSError:
                    if not target.exists():
                        raise
            finally:
                # Only our freshly created staging directory, never another batch.
                for item in temporary.iterdir() if temporary.exists() else []:
                    item.unlink()
                if temporary.exists():
                    temporary.rmdir()
        if target.is_symlink() or set(p.name for p in target.iterdir()) != set(
            expected
        ):
            raise ValueError("Destination identity/file list mismatch")
        for name, data in expected.items():
            path = target / name
            if path.is_symlink() or not path.is_file():
                raise ValueError("Destination must contain regular files")
            with path.open("rb") as stream:
                if stream.read(MAX_FILE_BYTES + 1) != data:
                    raise ValueError("Destination conflicts with sealed content")
        return dict(
            batch_id=batch["id"],
            manifest_sha256=batch["manifest_sha256"],
            transport="directory",
            financial_acceptance=False,
        )


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--database-url", default=os.getenv("DATABASE_URL"))
    p.add_argument("--client-id", default=os.getenv("CLIENT_ID", "demo-client"))
    p.add_argument(
        "--date",
        type=date.fromisoformat,
        required=True,
        help="Closed UTC day, YYYY-MM-DD",
    )
    p.add_argument("--output", default=".local/settlement")
    p.add_argument(
        "--sftp-config",
        help="Explicit local SFTP JSON configuration; no credentials on the command line",
    )
    args = p.parse_args()
    if not args.database_url:
        p.error("Local DATABASE_URL required")
    start = datetime.combine(args.date, datetime.min.time(), timezone.utc)
    store = SettlementStore(args.database_url)
    batch = store.seal(
        args.client_id, SettlementWindow(start, start + timedelta(days=1))
    )
    sink, destination = (
        DirectorySink(args.output),
        "directory:" + str(Path(args.output).resolve()),
    )
    if args.sftp_config:
        from tools.settlement_sftp import SftpSink

        sink = SftpSink(**json.loads(Path(args.sftp_config).read_text()))
        destination = sink.destination
    receipt = store.publish(batch["id"], sink, destination)
    print(
        canonical(
            dict(
                batch_id=batch["id"],
                source_files=len(batch["files"]),
                transport_receipt=receipt,
            )
        ),
        end="",
    )


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        # Never print connection strings, credentials or server exception bodies.
        print(
            canonical(
                {
                    "error": "reference_settlement_failed",
                    "category": type(error).__name__,
                }
            ),
            file=sys.stderr,
            end="",
        )
        raise SystemExit(1)
