# Architecture and implementation boundaries

## Contract first, native implementations

`contracts/uberpay-openapi.json` is an unchanged capture of the v1.4 site, with its hash in `source.json`. `runtime.json` repairs a string-valued `required` and mislocated path-parameter `required` values, plus the provisional U1 choice to accept Account examples without undefined `name`. `STRICT_ACCOUNT_NAME=true` restores that requirement. Token responses are validated using the request grant because the upstream `oneOf` response variants overlap. The independent Python validator checks every declared response body, int64 range and all callback request bodies; it never imports a gateway's validator.

The three gateways implement their own HTTP/authentication/codec/services in native language code. They share SQL migrations and public fixtures, not a sidecar business-logic server. Each example uses an injectable PSP adapter seam and demo wallet provider. The complete local profile, including deliberately synthetic funding modes, is in `contracts/implementation-profile.json`.

## Durable fake-money processing

The local fake processor and gateway data commit in **one PostgreSQL transaction**, serialized per client with a transaction advisory lock. `accounts` contains exact int64 balances; `transactions` contains Uber IDs and distinct PSP references; `operations` stores the original response for an idempotency key. This makes local duplicate/concurrency examples observable and atomic. A captured charge consumes both total and available balance; manual authorization reserves available balance, capture consumes total, cancel/expiry releases the hold. Pending refunds reserve the refundable amount against the original charge.

Pending completion and its eventual callback are durable rows in `jobs`. A worker commits the final state and callback job atomically. Callback network calls take place outside ledger transactions. Leased jobs resume after restart; retries use a stable delivery key only when that callback declares one. Callback 401 refreshes the scope token and retries with the same identity and bounded budget. Any other permanent error or exhausted retry is inspectable `FAILED`; a callback409 becomes `RECONCILE`, never an assumption that the payment itself failed. Callback delivery state is distinct from PSP transaction state.

## External PSP boundary is deliberately closed

A local atomic fake effect is **not** exactly-once external processing. Do not make a network request from the demo adapter while holding its SQL transaction. Before adding an external adapter, introduce durable operation intents and stable provider keys, dispatch outside database locks, then reconcile uncertain outcomes by provider lookup or safe provider-key replay before committing the response. Tests must cover a process crash after the processor succeeded but before the gateway recorded the result. Without that executor/reconciliation boundary, non-local modes must remain rejected.

Similarly, local consent is a synthetic identity exercise, not an authentication product. Real resource-owner login, consent, client registration, token revocation/rotation policies and redirect handling need the PSP's provider and security review. No environment setting turns demo identities into real identities.

## Authentication direction

Inbound Uber signatures cover raw request bytes, original path/query, Host, Date and Digest using the published draft-Cavage RSA-SHA256 scheme. Inbound PSP-issued bearer tokens are scoped and account-bound where needed; balance always requires them. Outbound callbacks instead obtain **Uber-issued** client-credentials tokens per payment/payout scope. These credential systems must never be interchanged.

## Scope and known limits

- Supported runtime is local-only; demo adapters, loopback/container-network endpoints, no production serving.
- E5 int64 is preserved across decoding, persistence, response and callback serialization; overflow is rejected.
- Full capture only until partial-capture semantics are approved.
- Some declared errors/state vocabularies contradict upstream prose/examples. The local error choices are not formal Uber compatibility approval.
- The three deposit callback builders are tested explicitly; funding-method-specific automatic use needs an Uber decision.
- No settlement schema/SFTP exporter is fabricated. Production retention, erasure, fraud and financial controls belong to each actual deployment.

## Review-hardened local profile

Transient DB/infrastructure errors are 500 even on routes lacking a declared 5xx; this is an explicitly provisional erratum, never a fake400 or stored retry result. Every pool uses a 1500ms lock timeout and 5000ms statement timeout. Java uses HikariCP. The per-client local lock is intentional and makes no independent-charge throughput promise.

Payouts reserve/debit the payer settlement account and credit the payee only on confirmation. Reconciliation metadata includes source_account_id, payee_account_id and order_id; raw customer/KYC/merchant-secret input is passed to the local adapter in memory, not persisted.

WAITING actions have creation-time random capabilities, CSRF, configurable lifetime and single-use completion. User denial and expiry release holds and emit distinct USER_CANCELLED / MERCHANT_EXPIRED reasons. OAuth codes are single-use and reuse revokes their family; refresh tokens deliberately do not rotate pending U2. Return URLs must exactly match the configured allowlist to receive a redirect.

See [review disposition](review-follow-up.md) for the full distinction between implemented regressions, optional omissions, unimplemented business flows and owner decisions.
