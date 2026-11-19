# PR #1 review disposition

This is a **local, synthetic-money reference profile**, not full Uber sandbox qualification.
The review is evidence to validate, not authority to approve API deviations. Every U-decision
below remains unapproved for live use. The upstream v1.4 pin is unchanged.

## Correctness fixes in this follow-up

| Review | Disposition |
|---|---|
| B1 | Retryable infrastructure errors stay 500 on every route, never become validation 400, and are not cached. The independent validator permits only the documented empty undeclared 5xx, not undeclared 4xx. |
| B2 | Node listens for both idle-pool and checked-out-client connection errors, discards clients after failed rollback, and recovers after backend termination. |
| B3 | Callback 401 evicts the scope token and schedules a fresh-token retry under the same event identity, bounded by the ordinary attempt budget. |
| B4 | Capture/cancel/refund bearer account is checked against the persisted original charge, before replay. |
| B5 | Default runtime accepts the published Account examples without undefined `name`; `STRICT_ACCOUNT_NAME=true` restores strict behavior. This is provisional U1, not an Uber approval. |
| B6 | WAITING user actions get creation-time random 256-bit capabilities, expiry and CSRF protection. GET never creates authority from a transaction ID; consumed/forged/expired sessions cannot change a payment. |
| H1, H2 | Rejected charge/payout callbacks carry a schema-valid reason. Token errors have the required OAuth2Error body and enum; bad signature is 400 invalid_request in every language. |
| H3 | All languages use the same reusable-refresh profile, no undeclared rotated token, configurable 30-day lifetime. Authorization-code reuse revokes the issued family. U2 remains open. |
| H4 | Capture declines are reachable with DEMO_CAPTURE_HARD/SOFT, produce 422, release the hold, and do not debit balance. |
| H5, H6 | Malformed query input is400; health ignores unrelated query data. Abandoned actions expire, release the reservation and notify with MERCHANT_EXPIRED; a user denial uses USER_CANCELLED. |
| H7 | **Scope narrowed, not a real executor implementation.** Only in-process synthetic adapters run under the per-client lock. Lock wait is 1500ms; statement timeout 5000ms. Real adapters remain fail-closed until intent/dispatch/finalization and reconciliation exist. |
| P1–P8 | Required headers, reference mismatch statuses, action-payment refunds, payout trigger semantics, strict JSON grammar and offset-required timestamps now align. |
| P9–P16 | Code-family revocation, refresh lifetime, token renewal margin, jitter, advisory-lock hash, Java Hikari pool, case-insensitive Bearer and IMF-fixdate profile align. |

## Coverage and data corrections

- G1, G14 / T1–T5, T9, T10: assert all 32 declared operation/status pairs per language, exact auth statuses/bodies, working date/URI formats, cancelled lookup, refund-ID lookup, rejected callbacks and the reviewed failures. The runner fails a full release run on a missing status. It does **not** infer complete semantic coverage from that count.
- G2: capture422 and payout400/409/422 are reproducible. `contracts/demo-outcomes.json` documents synthetic payout payee triggers; identical payout identity replays, changed payload conflicts. **Refund CONFIRMED→REJECTED reversal remains unimplemented**, pending a reviewed accounting/ordering design.
- G3: capture hard/soft, payout decline details, user cancellation, expiry and asynchronous merchant rejection are demonstrated. **Not every processor reason code has a production mapping or synthetic trigger.** Real processor mappings require partner input; do not advertise all enum-value coverage.
- G4, G5: adapter commands receive validated payment data, order-refund data, customer/account and payee fields in memory. Synthetic meal-voucher validation uses `demo-merchant` / optional `demo-merchant-secret`; the points fixture uses a documented 1:1 E5 conversion, not a real conversion rule. Retain source settlement ID (also inherited by refunds), payee routing ID and order ID for reconciliation, not merchant secrets, raw KYC or whole request bodies. Real KYC/routing/points policy is PSP-supplied.
- G6, G11, G12: return only to exactly configured redirect URIs; payouts debit payer settlement funds and credit the payee only upon confirmation, including restart-safe pending completion; payout responses never contain charge-only actions.
- G9: valid registered OAuth redirects receive errors and original state; unregistered redirects are never followed. Code exchange remains the schema-defined refresh-only result (U10).
- G13: all U1–U15 decisions are recorded in `contracts/errata.json`; none is presented as owner approval.
- G15: source-account IDs persist and `tools/settlement.py` defines an integration-only grouping/sink port. There is no fabricated file format, netting algorithm, SFTP delivery or report schedule.
- L1–L8: validate optional quoted created/expires metadata in addition to the signed Date, use own-key lookup, log scrubbed worker failures, correct Java image environment settings, and align status/security/lifetime docs. Refund failure text is human-readable. No credentials or personal request data are logged.

## Explicit remaining work and owner gates

| Review | What is NOT complete |
|---|---|
| H7 / T7 | External operation intents, dispatch outside locks, provider-key lookup/reconciliation, independent-charge throughput and ambiguous-processor crash tests. Current concurrency tests protect the **serialized local** ledger; no parallel-processing claim. |
| G2 | Refund reversal from CONFIRMED to REJECTED, ordered status-versioned events, idempotent inverse ledger entry and reconciliation if refunded funds have been spent. Do not enable by removing a unique constraint alone. |
| G3 | Exhaustive processor reason mappings and enum-value outcome coverage. |
| G7, G9 | Localized pages/error descriptions and consent presentation/selection for authorization_details locale, country and funding method. Current UI is English; schema acceptance is not localized-consent support. |
| G8 / U11 | Real deposit cash-in/top-up orchestration, updateDeposit amount adjustments, callback selection and agreed retry semantics. All six builders are tested, but three deposit builders are not end-to-end deposit flows. |
| G10 | Itemized balances are not applicable to the shipped single-bucket synthetic wallet; optional field is omitted. |
| G16 | Uber-assigned funding aliases with per-method country/currency onboarding configuration. Only documented DEMO methods are runnable; changing a string is not sandbox integration. |
| T6 | The shared suite is Python behavioral tests **plus JSON scenarios**, not wholly declarative JSON. Migrating fault/orchestration cases to a versioned scenario DSL is future work. |
| T8 | Completion restart and callback retry/retention are covered; additional independent multi-worker lease-loss/crash-between-ack-and-record probes remain required before external processing. |
| U1–U15 | Uber owner decisions and bilateral settlement requirements remain unresolved; local defaults are provisional and real modes remain disabled. |

## ERD alignment

Keep local implementation guarantees distinct from the external-adapter target design.
Update the Main ERD integration boundary and release criteria; correct Low Level Design
claims about sandbox mode, short intent transactions, PSP I/O outside locks, complete
JSON scenarios and fake clocks. Add the explicit local action/token/error/payout policies,
source/payee/order metadata, and remaining scope table above. A local 32/32 status pass
is not all-field/all-enum conformance or qualification by Uber.

Local v2 development state is disposable. Older synthetic payout rows may lack the new routing metadata; reset the local development ledger when switching incompatible pre-release examples rather than treating startup DDL as a real financial-data migration.
