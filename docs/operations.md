# Local reference operations

These are synthetic-reference diagnostics, not a production pager/monitoring
deployment. No live-transaction rollout or migration is required.

## Retry budgets and blocked chains

| Setting | Default | Meaning |
|---|---|---|
| CALLBACK_RETRY_BUDGET_MS | 86400000 (24h) | Elapsed delivery budget from job creation/re-drive |
| CALLBACK_RETRY_MS | 1000 | Exponential base, capped at60s plus jitter |
| CALLBACK_MAX_ATTEMPTS | 0 | Disabled; positive value is an explicit additional cap, primarily for tests |
| PROCESSOR_RETRY_BUDGET_MS | 86400000 (24h) | Elapsed unresolved failure streak; successful polls reset it |
| PROCESSOR_POLL_MS | 200 | Poll/recovery base, exponential transport-failure backoff capped at60s |
| PROCESSOR_MAX_FAILURES | 0 | Disabled; explicit positive test/operator override |

Callback429/503 honors Retry-After seconds or HTTP-date, capped at1h; elapsed
budget still applies. A callback409 enters RECONCILE; other permanent4xx enter
FAILED. Exhaustion is retained, never acknowledged or deleted.

Earlier unfinished callbacks **must continue blocking** later versions.
FAILED/RECONCILE emits `callback_chain_blocked` with safe job ID/status.
`worker_item_failed` identifies a poison completion without logging raw payloads,
credentials, KYC or SQL exceptions. Per-row savepoint/backoff prevents one poison
completion from stalling unrelated work. The inspection command exposes blocked
event counts; a real adopter should connect these to its own alerts.

## Inspect and re-drive without test controls

Run from repository root with local database credentials in DATABASE_URL:

```sh
.venv/bin/python -m tools.operations --client demo-client
.venv/bin/python -m tools.operations --client demo-client \
  --redrive callback --id JOB_ID --reason 'Dependency recovered; payload verified'
.venv/bin/python -m tools.operations --client demo-client \
  --redrive intent --id INTENT_ID --reason 'Provider lookup restored'
```

Database credentials are operator authority. These commands do not depend on
ENABLE_TEST_CONTROL and expose no public admin route. They refuse foreign,
completed and actively leased work. Re-drive resets the elapsed retry budget,
preserves cumulative attempts/response bytes/event identity/reservations, and
records actor/reason in operator_actions. An intent's generation advances so a
stale finalizer cannot commit. Re-drive never means “force success” or “clear a
hold”: the native worker must look up the original provider key.

For callback RECONCILE, first reconcile the receiver's actual outcome. Re-drive
only when retry is appropriate. updateRefund has no declared idempotency header;
do not promise exactly-once remote delivery. The synchronous-confirmed/single-
reversal example avoids two distinct callback transitions; transport retries can
still repeat the same callback.

## Storage and deployment boundaries

Every native worker performs bounded maintenance once per minute. Expired tokens
are deleted only after their existing validity ends. Operations and DONE jobs
older than7days move into immutable operations_archive/jobs_archive in batches of
200 per table; reference_operations reads both hot and archive rows for exact
replay. Failed/reconciling/pending/leased work is never purged. A database guard
prevents an archived job identity from being requeued. Archive data is retained
until deliberate disposal of the local namespace: no legal real-data erasure or
production retention certification is implied. PostgreSQL vacuum and disk/backup
policy remain deployment responsibilities.

Manual bounded maintenance, with no test-control dependency:

```sh
.venv/bin/python -m tools.operations --client demo-client --maintain --batch-size 200
```

Public consent creation does not take the monetary per-client advisory lock,
and is bounded by shared PostgreSQL counters:200/client/minute and60/actual peer
IP/minute by default. Excess gets429 with Retry-After60 before consent storage.
Old bucket rows expire. Signed money requests are not charged against this
public budget. Real deployments still need edge buffering, quotas and abuse controls. The
Java upload deadline and bounded payloads are defense in depth, not production
availability certification.

Keep processor encryption keys/configuration and the originating language
runtime with unresolved local state. A persisted runtime_bindings row enforces the originating language, storage
version and instrument-key hash. Startup rejects populated unversioned state,
wrong-language reuse and silent instrument-key changes. Use a fresh CLIENT_ID
and/or a fresh Compose project for a new runtime; no live migration is required.
INSTRUMENT_FINGERPRINT_KEY is separate from CLIENT_SECRET and must remain stable.
The included default is a disclosed synthetic-only value, never a production secret.
