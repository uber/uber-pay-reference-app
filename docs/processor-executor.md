# Durable processor execution

This is a runnable **synthetic network boundary**, not a bank implementation or a
production switch. All three gateways implement it natively. The private adapter
transport is defined in `contracts/processor-protocol.md`; it is not an Uber API.
`MODE=local` and local-host gates remain in force.

## Run independently of the gateway

From the repository root, generate and keep local development credentials in your
shell (do not commit them):

```sh
export PROCESSOR_MODE=http
export PROCESSOR_URL=http://127.0.0.1:9080
export PROCESSOR_AUTH_TOKEN="$(openssl rand -hex 24)"
export PROCESSOR_STATE_KEY="$(openssl rand -base64 32)"
.venv/bin/python -m conformance.processor_fixture --state .local/processor.sqlite
```

Start the chosen **native** gateway from another shell with those same values,
its normal local database/key/callback configuration, and `MODE=local`. See its
implementation README for the launch command. The fixture's SQLite file survives
both gateway and fixture restarts. Losing `PROCESSOR_STATE_KEY` makes unresolved
commands unrecoverable: retain it alongside the database and restore it, never
invent a replacement key and resubmit payments.

## Three phases

1. **Prepare:** authenticate before replay. In a short transaction, serialize only
   the logical operation identity; validate the native operation under a rolled-back
   savepoint; commit an AES-256-GCM encrypted command, stable provider key, resource
   claim, debit reservation and any required credit headroom. A known local rejection
   is committed/replayed without contacting the processor.
2. **Dispatch:** release the SQL connection before HTTP. Submit with the stable
   provider key, bounded timeout/body, bearer authentication and no redirects.
   Concurrent independent charges can reach the processor simultaneously, including
   charges for the same client/account. Locks still serialize short ledger updates.
3. **Finalize:** lock the intent, check its generation, validate the decision and
   immutable reference, and apply the native transition/replay response atomically.
   A PENDING result converts the debit reservation into the local hold; credit
   headroom and the resource claim remain until the final result. No local demo
   timer may finalize an HTTP-processor payment.

If a confirmed provider effect cannot be applied because local preconditions changed,
finalization rolls back and retains its intent/reservations for repair/reconciliation;
it never converts that known effect into a cached validation4xx.

Transport loss returns retryable 500, not a cached result or invented rejection.
The durable worker looks up the original key before a safe, same-key submission
only after a definite 404. HTTP 202 means unresolved, **not** permission to retry
with a new key. Lease generations fence stale finalizers. A repeated in-flight
request waits `PROCESSOR_WAIT_MS` then returns retryable 500 if still unresolved.
After `PROCESSOR_MAX_FAILURES`, the intent becomes inspectable `RECONCILE`; its
reservations remain. An explicit same-key retry can resume lookup. Never delete an
uncertain intent or clear its reservation to make another debit proceed.

## Final events and recovery

The worker polls PENDING operations. A local processor may alternatively POST to
`/adapter/events` with its bearer credential, provider_key and versioned decision.
It is **not** an Uber endpoint and is disabled in the default processor mode.
Invalid, duplicate and stale events cannot take over an active lease. Accepted
versions only advance state, except the explicit confirmed-refund reversal. A
reference change or impossible transition requires reconciliation.

The initial HTTP response is immutable on replay even when lookup later shows the
final state. Finalization commits the transaction and ordered callback outbox
record together. Per-transaction sequence numbers prevent a later refund-rejection
or deposit-confirmation callback from overtaking its predecessor; a failed earlier
callback blocks later ones for inspection/reconciliation.

Funding configuration is immutable for the lifetime of outstanding intents. The
command includes its configuration hash; restoring with changed aliases fails
closed. Drain outstanding work or restore the original configuration before a
change. There is no automatic key rotation or hot configuration migration.

## Accounting and policy choices

- Exact E5 debit and credit headroom protect both spendable funds and int64 bounds.
- A late rejection reverses a confirmed refund exactly once. Recover only currently
  unreserved funds, preserve unrelated authorization/intent holds, and record the
  unrecovered amount in `account_debts`. Future credits or released holds repay debt
  before becoming spendable. This is an explicit **synthetic reference policy**, not
  an Uber-approved production collections policy.
- `DEMO_DEPOSIT` is a distinct synthetic flow: confirmation/cancellation callbacks,
  with an optional **reduced** amount update delivered before confirmation. Modern
  ordinary charges still use chargeCallback. Assigned funding profiles explicitly select callback_mode under accepted U11.
- Balance uses the reference wallet ledger, not a remote production balance source.
  Resource-owner login, KYC, real routing and conversion rules remain PSP supplied.
- Commands may include sensitive operation input, so they are encrypted at rest in
  the intent. Routing IDs, hashes, decisions and replay responses remain readable
  in the disposable local store. Loopback HTTP/PostgreSQL are not production TLS or
  database-at-rest encryption. No real personal/payment data is allowed here.

## Acceptance tests

The common suite uses a SQLite-backed processor outside the gateway process and
SQL namespace-scoped faults. It proves simultaneous entry before a barrier release,
no idle SQL transaction while HTTP is blocked, bounded duplicates, crash before
submit, crash after independent processor commit, lost response recovery, all money
operations, pending credit reservations, and duplicate/stale reversal events. It
also exercises two competing callback workers and a crash after callback receipt
but before acknowledgment recording. `PROCESSOR_TEST_PAUSE_AFTER_PREPARE=true` is
a deterministic test fault honored only with test controls enabled; never use it
outside disposable conformance runs.

These tests establish the reference boundary, not exactly-once effects in an
arbitrary real processor. An adopter must supply durable downstream idempotency
and authoritative lookup, or keep uncertain outcomes quarantined.

### Accepted lifecycle additions

A manual charge permits one positive full/partial capture up to the original
authorization; successful capture releases the unused hold and closes the
authorization. The original is authorized_e5 and amount_e5 becomes captured money.
A subsequent capture conflicts409; refunds cannot exceed the captured amount.
Known declined partial capture releases the entire original hold, not just the
requested captured amount. Unknown provider outcomes remain protected.

For an existing pending charge whose frozen funding profile explicitly selects
deposit callbacks, a final CONFIRMED processor decision/event may include
`amount:{value,currency}`. Currency must match, and the positive amount may only
decrease. Finalization releases unused hold, records actual amount, and enqueues
updateDeposit before confirmDeposit. Increases or ordinary-charge adjustments
are quarantined without changing the ledger or inventing a successful callback.
