# Architecture and implementation boundaries

## Contract first, native implementations

`contracts/uberpay-openapi.json` is an unchanged capture of the v1.4 site, with its hash in `source.json`. `runtime.json` repairs a string-valued `required` and mislocated path-parameter `required` values, plus the accepted reference U1 choice to accept Account examples without undefined `name` and U4 additive CANCELLED lookup enum. `STRICT_ACCOUNT_NAME=true` restores that requirement. Token responses are validated using the request grant because the upstream `oneOf` response variants overlap. The independent Python validator checks every declared response body, int64 range and all callback request bodies; it never imports a gateway's validator.

The three gateways implement their own HTTP/authentication/codec/services in native language code. They share SQL migrations and public fixtures, not a sidecar business-logic server. Each example uses an injectable PSP adapter seam and demo wallet provider. The complete local profile, including deliberately synthetic funding modes, is in `contracts/implementation-profile.json`.

## Durable fake-money processing

The local fake processor and gateway data commit in **one PostgreSQL transaction**, serialized per client with a transaction advisory lock. `accounts` contains exact int64 balances; `transactions` contains Uber IDs and distinct PSP references; `operations` stores the original response for an idempotency key. This makes local duplicate/concurrency examples observable and atomic. A captured charge consumes both total and available balance; manual authorization reserves available balance, capture consumes total, cancel/expiry releases the hold. Pending refunds reserve the refundable amount against the original charge.

Pending completion and its eventual callback are durable rows in `jobs`. A worker commits the final state and callback job atomically. Callback network calls take place outside ledger transactions. Leased jobs resume after restart; retries use a stable delivery key only when that callback declares one. Callback 401 refreshes the scope token and retries with the same identity and bounded budget. Any other permanent error or exhausted retry is inspectable `FAILED`; a callback409 becomes `RECONCILE`, never an assumption that the payment itself failed. Callback delivery state is distinct from PSP transaction state.

## Durable HTTP processor boundary

`PROCESSOR_MODE=http` selects a native prepare/dispatch/finalize executor, not an
injected network call under the demo adapter's lock. It commits an encrypted
`provider_intents` command, stable provider key, `resource_claims` and
`intent_reservations`; releases the SQL connection; then calls the private local
processor transport. A second short transaction checks a fencing generation,
validates the immutable processor reference and commits the native result.
Retries look up the original provider key before replaying an absent operation.
Uncertain effects retain reservations; exhausted recovery becomes inspectable
RECONCILE. Pending credit headroom survives until final confirmation.

This boundary is runnable in all three languages against an independent durable
SQLite-backed processor. Its common tests kill the gateway before dispatch and
after processor commit, drop responses, prove concurrent independent HTTP calls,
and verify bounded duplicate waits and stale-event fencing. It is still a local,
synthetic reference protocol, not a real PSP implementation or production mode.
See [executor protocol and operation](processor-executor.md).

Local consent remains a synthetic identity exercise. Real resource-owner login,
client registration and operational/security policies are PSP supplied. No
environment setting turns demo identities into real identities.

## Authentication direction

Inbound Uber signatures cover raw request bytes, original path/query, Host, Date and Digest using the published draft-Cavage RSA-SHA256 scheme. Inbound PSP-issued bearer tokens are scoped and account-bound where needed; balance always requires them. Outbound callbacks instead obtain **Uber-issued** client-credentials tokens per payment/payout scope. These credential systems must never be interchanged.

## Scope and known limits

- Supported runtime is local-only; demo adapters, loopback/container-network endpoints, no production serving.
- E5 int64 is preserved across decoding, persistence, response and callback serialization; overflow is rejected.
- Single full/partial capture; release unused hold and preserve original authorized_e5. No multi-capture.
- The conflicts have accepted reference choices in decisions.json. The unchanged upstream pin is evidence, and reference design acceptance is not external certification.
- `DEMO_DEPOSIT` exercises confirm/cancel and reduced-amount update callbacks end to end. Ordinary charges still use chargeCallback; real funding-method routing is explicit configuration under accepted U11.
- U15 supplies immutable journals, sealed per-source JSONL/manifest reports and atomic directory/strict local SFTP delivery outside SQL. The format is reference-specific, not a fabricated Uber production agreement. Retention, erasure, fraud and financial controls remain actual-deployment concerns.

## Review-hardened local profile

Transient DB/infrastructure errors are 500 even on routes lacking a declared 5xx; this is an explicitly provisional erratum, never a fake400 or stored retry result. Every pool uses a 1500ms lock timeout and 5000ms statement timeout. Java uses HikariCP. The default in-process demo uses that local lock. HTTP processing uses operation/resource/account locks only for short prepare/finalize transactions, never across HTTP; no production throughput SLO is claimed.

Payouts reserve/debit the payer settlement account and credit the payee only on confirmation. Reconciliation metadata includes source_account_id, payee_account_id and order_id; raw customer/KYC/merchant-secret input is passed in memory in demo mode. The HTTP executor encrypts its recoverable command with AES-256-GCM; logs, normal transaction rows and the independent processor fixture do not store the raw command.

WAITING actions have creation-time random capabilities, CSRF, configurable lifetime and single-use completion. User denial and expiry release holds and emit distinct USER_CANCELLED / MERCHANT_EXPIRED reasons. OAuth codes are single-use and reuse revokes their family; refresh tokens deliberately do not rotate under accepted U2. Return URLs must exactly match the configured allowlist to receive a redirect.

Versioned callback sequences preserve per-transaction delivery order. A confirmed
refund can be reversed once: recover unreserved funds, record remaining debt,
protect unrelated holds, and repay debt from future credits/releases. This is the accepted reference accounting policy, not a claim of Uber-approved production collections behavior.
English/Portuguese pages and error descriptions, consent funding/country/locale
context, configurable funding aliases and optional itemized balances are shared
reference features in `contracts/reference-features.json`.

See [review disposition](review-follow-up.md) for the full distinction between implemented regressions, implemented local policies and remaining real-integration/owner gates.

## Reference settlement boundary

Commit-time deferred SQL records one immutable confirmation event per money
transaction and a separate inverse for late refund rejection. A shared producer /
exclusive period-closer lock orders commit evidence against sealed windows. The
standalone Python export utility (not a gateway sidecar) seals a closed UTC period
and exact per-source JSONL plus checksummed manifest in SQL, releases its
connection before atomic directory/SFTP transfer, then records a transport-only
receipt separately. Overlapping periods and conflicting immutable destinations
fail closed. See `settlement.md` and `contracts/settlement-reference.schema.json`.
