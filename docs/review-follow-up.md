# PR #1 review disposition

This is a **local, synthetic-money reference implementation**, not Uber sandbox
qualification. The source v1.4 pin is unchanged. The user delegated reference-project design authority: **U1–U15 are now accepted
and implemented reference choices** in contracts/decisions.json, with rationale
and named acceptance tests. This does not assert external certification.

## Correctness and parity

| Review | Implemented disposition |
|---|---|
| B1, B2 | Infrastructure failures remain retryable 500 on every route and are never cached. Node recovers from active/idle database connection loss. SQL waits are bounded. |
| B3, H1, H2 | Callback401 evicts the scoped token and retries with the same event identity; rejected callbacks carry the correct reason. Token failures conform to OAuth2Error. |
| B4, B6 | Original-account authorization precedes replay. User actions use creation-time 256-bit capabilities, CSRF, expiry and single use; GET never creates authority from an Uber ID. |
| B5, H3 | Account.name is optional by accepted reference U1 (`STRICT_ACCOUNT_NAME=true` opts in). All languages use the same reusable-refresh profile under accepted U2; code reuse revokes the family. |
| H4–H6 | Capture hard/soft declines return422 without a debit and release the hold. Malformed input is400, not500. Abandoned actions expire with MERCHANT_EXPIRED and release their hold. |
| H7 / T7 | Native HTTP executors commit encrypted intents and resource/fund reservations, dispatch with no SQL connection held, then finalize under generation/version fences. Stable-key lookup recovers crashes/unknown effects; duplicate waits are bounded. Independent durable processor tests cover these paths, including simultaneous same-client operations. |
| P1–P16 | Header/input grammar, status mappings, ownership, token lifetime/family handling, bearer casing, callback token safety margin/jitter, pools and lock hashes align. Saved error bytes and Content-Language replay unchanged if a retry requests a different language. |

## Business flows and optional data

| Review | Implemented disposition |
|---|---|
| G1, G14 / T1–T5, T9, T10 | Every full release run gates all32 declared operation/status pairs per language, plus independent semantic assertions; counts are not presented as complete all-field qualification. |
| G2 | Capture422, payout400/409/422, refund PENDING→REJECTED and PENDING→CONFIRMED→REJECTED are reachable. Reversal has a unique inverse record, ordered event versions and exactly-once local accounting. |
| G3 | Shared profiles trigger all14 charge and10 payout callback reasons; charge sync and async outcomes are asserted. Capture/payout initial error details have documented triggers. `unsupported_token_type` applies only to a token-revocation operation absent from this spec; `unauthorized_client` is not applicable to this local single registered client with all three grants enabled. |
| G4, G5 | Validated payment/order-refund/customer/payee fields reach adapter commands. Demo meal-voucher and 1:1 points rules are explicitly synthetic. Retain source/payee/order IDs for reconciliation, not raw KYC or merchant secrets in transaction rows/logs. HTTP recovery commands are AES-256-GCM encrypted; the processor fixture persists only hashes/routing/decisions. |
| G6, G11, G12 | Exact allowlisted return URLs; payout debits payer settlement funds and credits the payee only on confirmation. Pending rejection releases its hold; payout responses omit charge actions. |
| G7, G9 | en-US/pt-BR pages and error descriptions; validate/present consent funding/country/scopes and persist locale/context through code, refresh and access grants. Registered OAuth redirects return state/errors; unregistered redirects are never followed. Real login/SCA is PSP supplied. |
| G8 / U11 | Explicit synthetic DEMO_DEPOSIT approval, rejection/expiry and reduced-amount update-before-confirm flows. Ordinary charges retain chargeCallback. Assigned funding-mode routing is explicit by accepted U11; no external certification is claimed. |
| G10, G16 | Itemized meal-voucher balance and explicit assigned-code alias configuration with country/currency validation. Alias configuration is not Uber onboarding; the example codes/accounts are synthetic. |
| G13, G15 | U1–U15 are decided for this reference. U15 adds immutable events, closed-period per-source JSONL/manifest sealing and atomic private-directory/pinned-host-key local SFTP delivery; receipts are transport-only. No real bilateral format, FX/fee netting or financial acceptance is invented. |
| T6 | Ten versioned declarative JSON workflows execute HTTP/schema/header/body assertions, typed binding, forms, callbacks, concurrency, scoped SQL faults and process restart. Advanced deterministic processor/multiworker tests remain Python orchestration; the suite is not falsely labeled wholly JSON. |
| T8 | Two native workers compete across lease loss; stale successful delivery cannot acknowledge a newer attempt. SIGKILL after callback receipt/before acknowledgment retries the stable event without another ledger effect. |
| L1–L8 | Bounded signature metadata, own-key lookup, scrubbed worker diagnostics, image configuration, clone-local documentation, refresh policy and readable refund failure text. |

## Accepted reference policies and external qualification

A late refund rejection recovers only available **unreserved** funds. An explicit
debt records the remainder, unrelated authorization/intent holds survive, new
spending uses net availability, and credits/releases repay debt first. Real
collections/accounting policy needs PSP review.

Default in-process fake effects use the per-client atomic demo transaction.
`PROCESSOR_MODE=http` demonstrates the external boundary against local hosts only.
A persistent encryption key and immutable funding configuration are required for
recovery; changed/lost key or config fails closed. Uncertain/RECONCILE reservations
must not be deleted to permit a new debit. See `processor-executor.md`.

No real PSP connector, resource-owner identity, production serving profile,
production retention/erasure or actual counterparty settlement acceptance is bundled.
The reference settlement format and runnable delivery implementation are included. These
are intentional adoption/onboarding gates, not hidden implementation claims.

## ERD alignment

The Main ERD and separate Low Level Design tab now describe the implemented local
and HTTP paths, actual SQL tables, encryption/reservations, reconciliation/debt,
ordered callback versions, consent/localization/configuration and release gates.
Privacy/security answers distinguish synthetic controls from production obligations.
Reference design acceptance closes U1–U15, but no document wording claims external certification or implies that cloning enables real funds.

All local development state is disposable. Reset incompatible pre-release example
data instead of treating idempotent startup DDL as a production financial migration.

## Owner-decision completion

U4 returns200 CANCELLED using an explicit derived-runtime extension. U5 supports
one partial capture and releases unused hold, preserving the original authorized
amount and refund cap. U11 accepts bounded provider-driven deposit reductions.
U15 now has immutable SQL journal/batches/files, deterministic per-source reports,
exact totals, overlap rejection, atomic delivery and real local SFTP fault tests.
See owner-decisions.md and settlement.md. No project decision remains pending.
