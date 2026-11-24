# Accepted reference-project decisions — U1–U15

The user delegated reference-project design authority on November 20, 2026.
All fifteen choices are **accepted for this reference release**, implemented and
covered by named acceptance tests in `contracts/decisions.json`. They are no longer
an unresolved engineering backlog. This does not manufacture approval of an Uber
API change, production deployment or a real PSP settlement agreement.

Policy: prefer the declared operation schemas over conflicting diagrams; make
necessary deviations explicit in `contracts/errata.json` and reproducible in
`tools/check_contract.py`. The upstream capture and hash remain unchanged.

| ID | Accepted choice |
|---|---|
| U1 | Account.name optional by default; explicit strict profile remains available. |
| U2 | Reusable refresh tokens, no undeclared rotation field; bounded lifetime, account/scope/client binding and code-family reuse revocation. |
| U3 | Retryable500 on infrastructure faults everywhere, never cached; authentication uses declared per-route401/400 mappings. |
| U4 | Default200 REJECTED for cancelled/expired authorization, no confirmed_at. ENABLE_CANCELLED_LOOKUP=true explicitly enables the reference-only CANCELLED enum extension (D1, November24). |
| U5 | One full or partial capture, positive and no larger than authorization. Release all unused hold on success, preserve authorized_e5, refund only captured funds; another capture conflicts409. |
| U6 | Definitive capture rejection is422 with a schema-valid reason and releases the full original hold, even for a partial attempt. Unknown external effects remain protected/retryable, not business declines. |
| U7 | Successful cancel is empty200. |
| U8 | Lookup by Uber charge/refund ID; supplied merchant reference must match. |
| U9 | Always include PSP reference on rejected charge callbacks. |
| U10 | Code exchange returns refresh token; access token is obtained with the refresh grant. |
| U11 | Charge callback routing is explicit per asynchronous funding profile, never inferred from an ID or funding-code prefix. Only opt-in legacy deposit mode uses upstream-deprecated update/confirm/cancelDeposit; new integrations use chargeCallback. Amount may decrease before confirmation; update precedes confirm. Refund/payout keep their own callback types. |
| U12 | Balance request uses account_id/funding_method and signature plus account-scoped bearer. |
| U13 | Refund schema fields and201 CONFIRMED/PENDING/REJECTED; exactly-once local inverse with explicit debt for an unrecovered late rejection. |
| U14 | Changed payout identity/key payload is409 DUPLICATE_REQUEST_ERROR; identical request replays. |
| U15 | Runnable reference reconciliation export: closed UTC periods (daily default), immutable journal/batches, one canonical JSONL file per source, checksummed manifest, exact decimal-string E5, atomic directory or pinned-host-key local SFTP publication. Upload receipt is **not** financial acceptance. |

## Adoption is configuration and integration, not unresolved reference policy

A PSP must still supply its real processor and resource-owner identity, real
credentials/hosts, supported funding/account mappings and deployment controls.
Communicate the U4 additive value to strict upstream consumers or map it in the
partner boundary. A reference design decision does not prove a deployed Uber
consumer accepts a new enum value. The provided applications remain local and
synthetic until actual onboarding and security/financial qualification.

The U15 format is deliberately named `uberpay-reference-settlement-jsonl-v1`.
Use an explicit mapping to a counterparty's agreed format; do not send it to Uber
and call it a production settlement report. See `settlement.md` for runnable
commands, guarantees, failure behavior and the transport-only receipt.

## Consolidated review decisions (November 24, 2026)

D1 changes U4 to upstream-compatible default lookup; D2 recommends `DEMO_REFUND_SYNC_REVERSAL` (one reversal callback) and gates the async two-call variant behind an explicit default-off test extension; D3 relaxes only the description pattern by default, with `STRICT_DESCRIPTION_PATTERN=true` opt-in. Type/256-character maximum and escaping remain mandatory. See `contracts/decisions.json` and `docs/review-2026-11-24.md`.

## Final delegated-owner completion

Reference completion choices O1–O7 are accepted in contracts/decisions.json, each with rationale and executable evidence. They resolve callback/lookup scope, safe archival, bounded public consent, runtime/identity stability, expanded semantic qualification and immutable supply-chain pins. No reference-owner decision remains waiting on an external answer. Real integration credentials, processor/identity mapping and external qualification remain adoption prerequisites, not unimplemented reference policy.

- **O1 — Refund reversal callback policy:** Use synchronous201 CONFIRMED then one REJECTED updateRefund by default. ENABLE_ASYNC_REFUND_REVERSAL=false refuses the synthetic two-callback funding profile; true is an explicit reference/test interpretation of the reversal exception. Never suppress an actual financial inverse.
- **O2 — Payout lookup scope:** Keep find-transaction limited to charges/refunds and return404 for payout IDs. Payout recovery uses original provider-key reconciliation and callbacks; do not add an undeclared public operation.
- **O3 — Retention and exact replay:** Purge expired token records; archive operations and DONE jobs older than7days in bounded200-row batches once per minute. Keep unfinished work live and preserve immutable replay bytes, fingerprints, headers and event identities indefinitely in local archives.
- **O4 — Public endpoint abuse controls:** Admit consent via atomic PostgreSQL fixed-minute counters: default200/client and60/peer IP, counting actual peer rather than forwarded headers. Reject with429/Retry-After60 before creating a consent row. Expire old buckets; signed money operations have separate admission. Validated OAuth internal failures redirect server_error with original state.
- **O5 — Runtime and identity stability:** Bind every fresh client namespace to native runtime, storage version2 and independent instrument-key hash. Reject legacy/unversioned populated state, cross-language reuse or silent fingerprint-key changes. Rotating OAuth client credentials does not rotate instrument identity.
- **O6 — Semantic release gate:** Run the same independent wire scenarios for every language, including exactly-once ledger witnesses after crash/lost-response,100000-row index-plan regression, bounded public abuse, async/deposit/expiry/duplicate settlement events, multi-source/currency reporting, concurrent seals and commit-clock boundary checks.
- **O7 — Supply-chain and key separation:** Pin all seven base/service images by verified manifest digest and all six official Actions by commit. Run fixture/keygen as non-root; export a public-only gateway key volume and keep the development private key only in the fixture/client volume.
