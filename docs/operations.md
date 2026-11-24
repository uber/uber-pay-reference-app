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

Expired tokens and historical operations/DONE jobs are **not automatically
purged**. Replay, code-family revocation, callback ordering and financial
reconciliation impose different retention needs; a blind TTL delete is unsafe.
Use disposable local database/client namespaces. Do not claim bounded production
retention or an erasure policy. A PSP must define reviewed per-record retention,
archiving/purge and audit access before deployment (remaining M11 work).

Public consent creation no longer takes the monetary per-client advisory lock,
but it still persists short-lived consent rows. A real deployment needs edge
rate limits, buffering, resource quotas and the retention policy above. The
Java upload deadline and bounded payloads are defense in depth, not production
availability certification.

Keep processor encryption keys/configuration and the originating language
runtime with unresolved local state. Durable cross-language storage migration
and old ciphertext-format migration are not implemented. Do not switch profiles
over an active client's database and assume hash/reference compatibility.
