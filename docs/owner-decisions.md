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
| U4 | Successful cancelled/expired authorization lookup is200 CANCELLED. This is a documented additive **reference runtime extension**, not a silent edit to upstream. |
| U5 | One full or partial capture, positive and no larger than authorization. Release all unused hold on success, preserve authorized_e5, refund only captured funds; another capture conflicts409. |
| U6 | Definitive capture rejection is422 with a schema-valid reason and releases the full original hold, even for a partial attempt. Unknown external effects remain protected/retryable, not business declines. |
| U7 | Successful cancel is empty200. |
| U8 | Lookup by Uber charge/refund ID; supplied merchant reference must match. |
| U9 | Always include PSP reference on rejected charge callbacks. |
| U10 | Code exchange returns refresh token; access token is obtained with the refresh grant. |
| U11 | Charge callback routing is explicit per asynchronous funding profile, never inferred from an ID or funding-code prefix. Only deposit mode uses update/confirm/cancelDeposit. Amount may decrease before confirmation; update precedes confirm. Refund/payout keep their own callback types. |
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
