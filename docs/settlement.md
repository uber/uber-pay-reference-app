# Reference settlement/reconciliation workflow (U15)

This is a complete **local synthetic reconciliation workflow**, not a claim that
Uber's production settlement files use this format. Its design is accepted for
the reference app. Counterparty schema/host/credential mapping is deployment input,
not a missing implementation or an invented commercial agreement.

## Journal and accounting direction

All three native gateways use the shared append-only `settlement_entries` journal.
A deferred SQL trigger runs only at commit, after PSP reference and routing metadata
are final. It records confirmed charges/captures/payouts as negative E5 and confirmed
refunds as positive E5. A later refund rejection adds a separate negative inverse,
never edits or removes the confirmed refund. Pending/authorized/rejected-without-
confirmation transactions and unused authorization releases do not enter the journal.
The original manual authorization is retained in `transactions.authorized_e5`;
partial capture records only the captured amount as a monetary event.

Every entry is unique by client/kind/Uber ID/event type. Missing source-account
metadata is rejected by native validation before processor dispatch and is also
enforced at monetary commit, rather than silently losing a report row.
Journal rows, batch manifests and sealed files reject UPDATE/DELETE. This captures
new transitions after enabling the schema; it does not invent historical events
for old disposable demo state. There is no live financial migration in this project.

## Format and closing periods

- Default period: one completed UTC day, `[00:00, next 00:00)`. The library can take
  an explicitly bounded closed UTC window; windows may not overlap sealed batches.
- One `source-<SHA256(source_account_id)>.jsonl` per source with monetary entries, including independent
  currency totals. No cross-currency netting, fees, FX or commercial settlement
  obligation is inferred from this synthetic ledger.
- Each JSON line has deterministic event ID, UTC timestamp, source, kind/Uber ID,
  immutable PSP reference, event type, currency and **decimal-string** signed E5.
  No floating point, raw KYC, names, merchant secrets or credentials are exported.
- `manifest.json` identifies format, client, period, source files, entry counts,
  SHA256 checksums and per-currency exact totals. Aggregate totals use arbitrary-
  precision integers, then strings. JSONL avoids spreadsheet-formula interpretation
  of arbitrary identifiers that a CSV example could accidentally encourage.
- Sealing commits immutable `settlement_batches`/`settlement_batch_files` content.
  The same period replays the same bytes; overlapping periods fail. A shared
  producer/exclusive closer advisory-lock protocol prevents an in-flight earlier
  producer from slipping behind a sealed period. Event time is assigned by the
  database after taking the producer lock, not backdated by user input.

## Run a report

Use generated local data only. From the repository root, with Python dependencies
installed by `make setup` and a native gateway initialized against local PostgreSQL:

```sh
# Choose a completed UTC day containing your generated synthetic journal entries.
.venv/bin/python -m tools.settlement \
  --database-url "$DATABASE_URL" --client-id "$CLIENT_ID" \
  --date YYYY-MM-DD --output .local/settlement
```

The output root must be private (0700); files are0600. The utility stages a full
batch and atomically renames its directory. Re-running verifies the existing
manifest and every byte; it never overwrites conflicting evidence. Empty periods
produce an explicit empty manifest. Reference limits are100000 entries and64MiB
per file; split a too-large library reporting window instead of truncating it.
No recurring OS or cloud schedule is installed: invoke it for each closed daily
period from an explicitly approved operator/scheduler setup.

## SFTP publication

Pass `--sftp-config .local/settlement-sftp.json` with these explicit fields:

```json
{
  "host": "127.0.0.1",
  "port": 2222,
  "username": "reference",
  "private_key_file": ".local/settlement-client-key",
  "known_hosts_file": ".local/settlement-known-hosts",
  "root": "/reports",
  "passphrase_env": "REFERENCE_SFTP_KEY_PASSPHRASE"
}
```

Omit passphrase_env for an unencrypted generated development key. Never commit
keys, passphrases or actual counterparty configuration. The key must be0600 and
the destination directory must already exist. Only loopback SFTP hosts are
permitted in this reference, with explicit host-key pinning and RejectPolicy;
agent/key discovery, automatic host-key acceptance and password fallback are off.
Connect/authentication/channel I/O have bounded timeouts.

Upload all files into a unique staging directory, read them back to verify exact
checksums, then atomically rename to the immutable batch ID. Only a complete final
batch is consumable. A lost acknowledgment is reconciled by checking the existing
final manifest/files; a retry cannot overwrite a conflicting batch. An interrupted
connection can leave an uncommitted `.upload-*` directory, which is not a report;
operator cleanup must only remove confirmed-abandoned staging directories.

Network/file delivery happens **after the sealing connection is closed**. A short
separate SQL transaction records the idempotent `settlement_deliveries` receipt by
batch/destination. A checksum mismatch or failed/unknown transfer creates no
successful receipt. A crash after upload/before receipt simply re-verifies/replays.
`financial_acceptance=false` is deliberate: transfer completion is not bank
settlement or Uber business acknowledgment.

## Evidence and production boundary

The shared wire suite generates confirmed charge, partial capture, refund,
reversal and payout events from each native gateway; checks signs/exact totals,
immutability, replay and overlap protection; and delivers through a real loopback
SSH/SFTP fixture. Unit tests cover encrypted key use, changed host key rejection,
corrupt content and a lost SFTP acknowledgment after atomic rename.

A real deployment additionally needs purpose/retention/erasure, storage/file
cryptography, access control, clock/cutoff/reconciliation operations, credentials,
its actual counterparty format and commercial financial acceptance workflow.
These are not granted by an engineering decision to ship this reference format.
