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
pin. U4 adds CANCELLED to the derived runtime enum; communicate/map that additive
value when integrating a strict consumer. U5 permits one partial capture, releases
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
