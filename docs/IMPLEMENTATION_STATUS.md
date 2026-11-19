# Implementation and verification status

Baseline: Uber Pay API v1.4 / OpenAPI 3.0.3, rechecked against the published source on November 20, 2026. The upgrade is tracked in draft PR #1 on branch `arka/uberpay-psp-reference-v2`. Review-hardened behavior and remaining limitations are recorded in `review-follow-up.md`.

## Implemented

- Native TypeScript/Express, Java/Spring Boot and Go/net/http gateways, not language wrappers around a common sidecar.
- All nine partner operations, six validated callback builders, synthetic payment/payout/refund ledger, explicit local user actions and wallet consent.
- Exact E5/int64 codecs, raw-wire signed ingress, OAuth scopes/account binding and three token grants.
- Shared PostgreSQL migrations, atomic demo effects/idempotency, refund reservations, expiry/capture/cancel protection, durable callback retries and lease/attempt fencing.
- Shared black-box request/response fixtures, scenario manifests, mock Uber token/callback service, failures/races/restart tests and machine-readable reports.
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
| Python fixture/codec/packaging unit tests | 16 passed |
| Node native unit/security tests | 14 passed |
| Java native unit/security tests | 13 passed |
| Go native tests with race detector | Passed; 8 native tests with subcases |
| Independent Node wire conformance | 81 passed, 0 failed, 0 skipped |
| Independent Java wire conformance | 81 passed, 0 failed, 0 skipped |
| Independent Go wire conformance | 81 passed, 0 failed, 0 skipped |
| Python lint/format and Go vet | Passed |
| PostgreSQL migration up/repeated-up/down in rollback-only isolated schema | Passed |

The 243 wire cases exercise all 32 declared operation/status pairs per language. They include exact integers above 2^53, raw signature failures, concurrent duplicates, capture/cancel races, OAuth consent and grant reuse, refund limits, all six callbacks, transient/permanent failures, retry exhaustion and recovery after SIGKILL. They use synthetic funds only. JSON/JUnit reports and native server logs are in ignored `.artifacts/conformance/`; the review aggregate log is `.artifacts/pr1-review-verify.log`.

## Not verified or not bundled

- Docker Engine/Compose are unavailable on the implementation host. Container builds and clone-and-run smoke are exercised in all three CI jobs; consult the PR checks for the current commit rather than inferring container results from local tests.
- No real Uber sandbox call or live transaction was made. No Uber onboarding/compatibility approval is claimed.
- No real PSP processor, real resource-owner login/consent, production serving mode, or settlement-file implementation is bundled. Non-local modes fail closed. Before enabling a network processor, add the durable external-operation intent/dispatch/reconciliation boundary described in `architecture.md`; do not call a real processor inside the demo database transaction.
- The local contract interpretations in `contracts/errata.json` require Uber-owner review before external qualification. Refresh-token rotation policies and real retention/access/financial controls remain a PSP responsibility, not a promise of the examples.

## Review qualification

The original 53-scenario pass did not establish complete behavioral conformance. The follow-up adds an independent 32/32 declared-status release gate and regressions for transient faults, backend loss, real action pages, callback 401/revocation, cross-account operations, capture/payout declines, token families, payout ledger direction and metadata handling. Full enum/business-flow coverage, deposit orchestration, real PSP executor/reconciliation, localization and owner approvals remain out of scope or pending, as itemized in `review-follow-up.md`. Use the current `.artifacts/conformance/summary.json` and CI for exact scenario counts and results; do not infer them from this narrative.
