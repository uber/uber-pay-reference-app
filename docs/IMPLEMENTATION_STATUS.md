# Implementation and verification status

Baseline: Uber Pay API v1.4 / OpenAPI 3.0.3, rechecked against the published source on November 24, 2026. The upgrade is tracked in draft PR #1 on branch `arka/uberpay-psp-reference-v2`. Review-hardened behavior and remaining limitations are recorded in `review-follow-up.md` and `review-2026-11-24.md`.

## Implemented

- Native TypeScript/Express, Java/Spring Boot and Go/net/http gateways, not language wrappers around a common sidecar.
- All nine partner operations, six validated callback clients and end-to-end synthetic flows, synthetic payment/payout/refund ledger, explicit local user actions and wallet consent.
- Exact E5/int64 codecs, raw-wire signed ingress, OAuth scopes/account binding and three token grants.
- Shared PostgreSQL migrations, atomic demo effects/idempotency, refund reservations, expiry/capture/cancel protection, durable callback retries and lease/attempt fencing.
- Native durable HTTP executors with encrypted intents, fund/resource reservations, stable-key reconciliation, bounded duplicate waits and authenticated versioned events.
- Ordered callback versions, idempotent refund inverse/debt, explicit deposit approval/amount updates, en-US/pt-BR consent/errors, itemized balances and country/currency funding aliases.
- Shared black-box request/response fixtures, ten versioned JSON workflows, mock Uber token/callback service, failures/races/restart tests and machine-readable reports.
- Local-only configuration, generated development keys, disabled-by-default secret-protected controls, container recipes and per-language CI.
- The retired deposit-only process/UI is removed from serving code; Git history retains the original.

## Verified locally

The exact aggregate command, run from the repository root with Node 24, JDK 21, Go 1.25, Python 3.13 and a disposable PostgreSQL 17 instance on loopback port 55432:

```sh
make verify DATABASE_URL=postgresql://uberpay@127.0.0.1:55432/uberpay
```

| Check | Result |
|---|---|
| Pinned source hash, documented runtime normalization, 53 schemas, 142 references | Passed |
| Python fixture/codec/packaging unit tests | 32 passed |
| Node native unit/security tests | 14 passed |
| Java native unit/security tests | 13 passed |
| Go native tests with race detector | Passed; 8 native tests with subcases |
| Independent Node wire conformance | 120 passed, 0 failed, 0 skipped |
| Independent Java wire conformance | 120 passed, 0 failed, 0 skipped |
| Independent Go wire conformance | 120 passed, 0 failed, 0 skipped |
| Python lint/format and Go vet | Passed |
| PostgreSQL migration up/repeated-up/down in rollback-only isolated schema | Passed |

The 360 wire cases exercise all 32 declared operation/status pairs per language. They include exact integers above 2^53, raw signature failures, concurrent duplicates, capture/cancel races, OAuth consent and grant reuse, refund limits, all six callbacks, transient/permanent failures, retry exhaustion and recovery after SIGKILL. They use synthetic funds only. JSON/JUnit reports and native server logs are in ignored `.artifacts/conformance/`; the current aggregate log is `.artifacts/review2-final-verify.log`.

## Not verified or not bundled

- Docker Engine/Compose are unavailable on the implementation host. Container builds and clone-and-run smoke are exercised in all three CI jobs; consult the PR checks for the current commit rather than inferring container results from local tests.
- No real Uber sandbox call or live transaction was made. No Uber onboarding/compatibility approval is claimed.
- No real PSP processor, real resource-owner login/consent, production serving mode, or actual counterparty settlement acceptance is bundled. Non-local modes fail closed. The durable native HTTP executor and independent synthetic processor are bundled and tested, but intentionally local-host restricted. A PSP must map the private transport to its actual processor and supply real identity, secrets, financial controls and onboarding; never put a network call inside the in-process demo transaction.
- U1–U15 reference-project choices are accepted in contracts/decisions.json, with implemented behavior and named tests. U4 now defaults to upstream REJECTED for cancelled/expired authorizations; ENABLE_CANCELLED_LOOKUP explicitly opts into the derived-runtime extension. The upstream pin is unchanged. External qualification and real retention/access/financial controls remain deployment obligations, not claims established by a project decision.

## Review qualification

The original 53-scenario pass did not establish complete behavioral conformance. The follow-up adds an independent 32/32 declared-status release gate and regressions for transient faults, backend loss, real action pages, callback 401/revocation, cross-account operations, capture/payout declines, token families, payout ledger direction and metadata handling. The completion adds all14 charge and10 payout callback reasons, native HTTP intent/reconciliation and crash tests, refund reversal/debt, deposit amount/callback ordering, itemized balances, localization, assigned-code configuration, ten versioned JSON workflows and multi-worker fencing. The owner-decision completion adds CANCELLED lookup, one partial capture with remainder release/refund cap, provider-driven deposit reductions, and immutable per-source settlement export plus real local SFTP delivery/fault tests. All U1–U15 reference decisions are closed; real processor/identity integration, production controls and actual counterparty acceptance remain deployment prerequisites. Use the current `.artifacts/conformance/summary.json` and CI for exact scenario counts and results; do not infer them from this narrative.


## November 24 consolidated review

All seven blocking finding regressions are covered by the shared suite. D1–D3
are recorded, including default upstream-compatible cancellation lookup, the
synchronous-confirmed/single-refund-reversal-callback example, and opt-in strict
description regex. The literal published curl also lacks required id/authorization:
it remains400 unchanged; the successful golden case supplies those required fields
and the configured funding code, rather than inventing unsafe defaults.

The update includes elapsed24h retry budgets, Retry-After, auditable operator
re-drive, fair provider polling, idempotency-key serialization, sorted payout
account locking, Java ownership/strict parsing/nonblocking uploads, signing-key
overlap, instrument-label/fingerprint parity and short-barrier settlement export.
See review-2026-11-24.md for exact dispositions and **remaining nonblocking work**,
notably retention, deeper settlement/scale coverage, cosmetic parity and supply
chain pinning, plus real external callback-count/payout-lookup clarifications.
No external approval was invented. The live Google Doc has not been edited in
this review pass; a separate tab-by-tab update checklist was prepared.

`aifx verify` exits0 with “No monorepo config and no custom config found”; it adds
no repository-specific checks here. The explicit make verify command above and
current-head CI are the executed validation evidence.
