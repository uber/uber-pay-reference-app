# PSP integration path

## 1. Learn the correct direction locally

Choose Node.js, Java or Go and run the shared suite. Uber Pay is the caller of the PSP-hosted `/charge`, `/refund`, `/payout` and other partner operations. The gateway is the caller of Uber callback endpoints. `make demo` exercises a synthetic charge and transaction lookup with generated development signing keys.

## 2. Map your processor and wallet

| Adapter responsibility | PSP-supplied behavior |
|---|---|
| Charge | Account/funding mapping, synchronous or asynchronous authorization/capture, user actions |
| Capture and cancel | Atomic/consistent completion of an authorization, including expiry and races |
| Refund | Original-transaction lookup, cumulative refundable amount and rejected/pending outcomes |
| Balance | Account-bound available/total balances without precision loss |
| Payout | Payee mapping and processor reference with explicit final or pending result |
| Operation lookup | Resolve ambiguous timeouts using the original provider idempotency key |
| Wallet identity/consent | Authenticate the resource owner, validate wallet references and manage grant/token lifecycle |

The supplied demo adapters intentionally make no external calls. Their ledger and idempotency effects share a PostgreSQL transaction; they are not a safe wrapper for an arbitrary real HTTP processor. Use the supplied native `ProcessorExecutor` and map the [private processor transport](../contracts/processor-protocol.md) to a separate processor service; it already commits intents/reservations, dispatches outside SQL and reconciles by the original provider key. [Run the independent durable fixture](processor-executor.md) before adapting it. Do not bypass the local-only mode/host gates merely to point the demo at a remote server. The runnable real-processor implementation is PSP-specific and is not bundled.

## 3. Apply the accepted reference profile

All U1–U15 reference-project decisions are accepted and implemented in
`contracts/decisions.json`; they are not a pending reference engineering backlog.
`contracts/errata.json` describes exact differences from the unchanged upstream
pin. U4 now maps cancelled/expired authorizations to REJECTED by default; only
`ENABLE_CANCELLED_LOOKUP=true` enables the reference-only CANCELLED response. U5 permits one partial capture, releases
the unused hold and limits refunds to actual captured funds. A reference choice
does not establish that a particular deployed Uber consumer has accepted an
extension. Preserve the upstream pin and qualify the chosen real integration.

Uber onboarding must provision/confirm the developer client, PSP base URL, supported funding-method codes, source accounts, signing keys, scopes and callback environment. `DEMO_*`, demo accounts and localhost URLs are not onboarding configuration. `FUNDING_METHODS_FILE` loads an explicit per-code country/currency mapping (see `contracts/funding-methods.example.json`); it does not register a funding method with Uber. Seed only synthetic wallet/settlement accounts in those currencies for local tests. Keep alias configuration immutable until outstanding intents drain. Real sandbox connectivity remains disabled until an appropriate adapter/auth provider and review are available.

The local consent UI validates and presents all authorization details and approved
scopes in English or Portuguese. Country/funding/locale consent context is retained
through code, refresh and access grants; scope/account authorization remains
mandatory. A real resource owner, consent presentation policy and identity system
must replace the synthetic demo account. This is not an SCA implementation.

## 4. Test the real boundary without real funds

Extend the common fixture/scenario corpus and the chosen language's unit tests with processor rejection, timeout, restart, idempotency, multiple-refund, capture/cancel and callback failures. Use meaningful request/response captures with exact amount, currency, state, Uber ID and PSP reference. Redact credentials and personal data. A passing local suite is a prerequisite—not certification by Uber.

## 5. Reference settlement and operational readiness

`docs/settlement.md` documents the runnable accepted U15 workflow: immutable
confirmation/reversal journal, closed UTC reporting periods (daily default),
one canonical JSONL file per source and checksummed manifest, sealed replay,
no overlaps, atomic private-directory or pinned-host-key local SFTP publication.
Transport receipt does not mean financial settlement acceptance. Configure the
actual counterpart's format mapping, host/key/credentials and business acceptance
process; do not claim that the reference format is Uber's bilateral file format.
A live deployment needs its own privacy/security/financial review, retention,
reconciliation and incident handling. The reference has no live-data migration.


## 6. Published examples and deprecated surface

New integrations use `chargeCallback`, `payoutCallback` and `updateRefund`.
`confirmDeposit`, `cancelDeposit`, `updateDeposit` are **upstream-deprecated**:
`DEMO_DEPOSIT`/`callback_mode=deposit` is an opt-in legacy compatibility example
for partners already on those callbacks, not a new-integration recommendation.
Also deprecated: `session_id`, charge-response `merchant_reference`/`metadata`,
`updateDeposit.metadata`, and every Account/AccountOwner/Deposit `uri` field.
Informational deprecated URI fields are strings; they cannot choose a wallet or
redirect. Deprecated charge-response fields are not emitted.

The literal Authentication curl also omits required `id` and `authorization`.
The stored golden fixture preserves those omissions; the executable acceptance
case supplies them and replaces BOLETO with a configured code. The gateway does
not invent transaction IDs or authorization. Default descriptions accept Unicode
and punctuation, retaining type/maxLength256. `STRICT_DESCRIPTION_PATTERN=true`
restores the upstream pattern. Merchant/points business rules apply only to their
funding profiles (`DEMO_MEAL_VOUCHER`, `DEMO_POINTS`), never unrelated charges.
The voucher fixture allowlist is demo-merchant / demo-merchant-secret only.

## 7. Redirect, signing, and transport deployment requirements

OAuth `ALLOWED_REDIRECT_URIS` is exact registration (including query), intentionally
stricter than upstream guidance. Register any changed Uber redirect before use.
Payment `RETURN_URL_ALLOWLIST` is separate: scheme, authority/port and path (a
trailing slash permits that subtree), never the query. The original allowed URL,
including its correlation query, is returned verbatim after approve or deny.
Unknown schemes, credentials, fragments and unlisted authorities fail closed.
Keep HTTP entries loopback-only in this local reference. Add reviewed HTTPS/deep
link entries rather than a wildcard. `uber://payments/` is a synthetic example,
not an assertion of every Uber application's registered scheme.

Proxies must preserve the externally signed **Host and complete raw path/query**,
including BASE_PATH. Do not rewrite them or trust arbitrary X-Forwarded-* values.
Buffer/rate-limit incoming uploads and public consent requests at the edge.
Java additionally uses asynchronous bounded raw-body buffering with a total10s
deadline so slow bodies do not occupy all request threads. Local mode remains
non-production. `UBER_SIGNING_KEYS_FILE` optionally names a JSON object mapping
key IDs to PEM paths for overlapping RSA signing-key rotation; the legacy single
key stays configured. Reject unknown key IDs and remove old keys only after the
agreed overlap. Never commit private keys.

## 8. Fields a real PSP must map

| Request data | Required adopter responsibility; synthetic behavior |
|---|---|
| source.id, destination, all owner_id fields | Resolve registered settlement/routing/customer identities. Without access-token authorization the demo debits demo-account regardless of source.id; source.id is reporting metadata, not wallet authority. |
| destination_owner and payee name/local_name/DOB/address/document | Use verified identity/KYC and appropriate consent/prefill. Demo forwards but does not certify these fields. Real RUT/CUIT check digits must be validated; invalid details map to400 INVALID_PAYMENT_DETAILS_ERROR. |
| description, initiated_at | Persist/reference according to PSP policy; do not use client timestamps as ledger authority. Escape text in any presentation. |
| order_payment_data/order_refund_data | Correlate order/refund identifiers and validate their relationship. Demo retains order_id and forwards full input, not a full order/KYC system. |
| wallet_reference_data.display_name | Return supplied label when present, otherwise Local demo wallet. Instrument fingerprint is domain-separated HMAC of synthetic instrument identity under local client secret; real adapters need a stable dedicated secret and canonical underlying-instrument identity across Uber accounts. |
| offline_access | Accepted scope; refresh token is always issued on code/wallet grants because the response schema requires it. It does not bypass consent/account/scope binding. |

`find-transaction` intentionally covers charge/refund, not payouts. Whether Uber
requires payout lookup, and whether two async updateRefund calls are accepted,
remain **external onboarding questions**, not claims answered by this repo.
Use the synchronous-confirmed/single-reversal-callback example by default.
See `docs/operations.md` for elapsed retries, safe re-drive and retention boundaries.
Do not hot-switch Node/Go/Java on one client's live database: their durable
fingerprint/reference encodings are not a cross-language storage migration ABI.
Use isolated client namespaces/database volumes or preserve the originating runtime.
