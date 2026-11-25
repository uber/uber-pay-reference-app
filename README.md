# Uber Pay PSP reference app

A self-contained **PSP-hosted** integration reference for Uber Pay API **v1.4**, implemented natively in **Node.js/TypeScript, Java, and Go**. Choose one implementation; each exposes the same nine partner operations and is tested by the same wire-level suite.

**Synthetic local funds only.** This is not an Uber-operated payment processor or a production PSP SDK. All bundled processor and wallet identities are demonstrations. Non-local serving modes and non-local Uber callback destinations fail closed. Real PSP processing, Uber onboarding, contract clarification and security/financial review are required before a sandbox or live integration; changing an environment label is not sufficient.

## Clone and run a local example

Install Docker Engine with Compose v2, Git and Make. No Uber credentials are required.

```sh
git clone https://github.com/uber/uber-pay-reference-app.git
cd uber-pay-reference-app
make up LANGUAGE=node       # alternatives: java or go
make demo LANGUAGE=node
```

Compose builds the selected native gateway, PostgreSQL, a key-generation job and a fake Uber callback/OAuth service. The demo sends a correctly signed synthetic charge and looks it up. The gateway is at `http://127.0.0.1:8080/uberpay/v1`; readiness is `GET http://127.0.0.1:8080/health`. Do not enable two language profiles on port 8080 simultaneously. Each client namespace is bound to its originating language; use a fresh Compose project/database namespace when trying another implementation.

```sh
make down                  # stop containers; keep the disposable DB/key volumes
```

The default Compose credentials and account identifiers are explicitly synthetic, not secrets for a real integration. Keys are generated locally into an ignored Docker volume; private keys are not committed or built into an image. Ports bind to loopback. See [security boundaries](docs/security.md).

## Run the same conformance suite against all three languages

Native development prerequisites: **Node 24+ and npm, JDK 21, Go 1.25+, Python 3.13+, PostgreSQL 17**, and Make. The Java Maven wrapper downloads and checksum-verifies Maven if it is not already available. Set `JAVA_HOME` when the shell's Java is not JDK 21. Dependency files pin the versions used by each implementation.

```sh
make setup                         # Python venv, public test dependencies, dev keys
# Start only the local database if using Docker for PostgreSQL:
docker compose up -d db
make conformance LANGUAGE=all       # builds and tests Node, Java, Go separately
# or one language:
make conformance LANGUAGE=java
make test                          # Python and all native unit/security tests
make contract                      # source hash, schema syntax and reference checks
```

Use a different native database with `make conformance LANGUAGE=all DATABASE_URL=postgresql://uberpay:uberpay@127.0.0.1:5432/uberpay`. Local DB hosts are restricted; each test run uses a new synthetic client namespace. A PostgreSQL installation can also be managed with `tools/local_db.py` on port 55432; it does not register a background OS service.

The harness launches native processes, signs the exact request bytes, validates responses **independently** against the contract, receives all six callback types, injects failures, tests concurrent requests, and restarts gateways. No language proxies its implementation through another. Required cases are not silently skipped.

Sixteen versioned [JSON workflows](docs/scenario-format.md) cover all nine public operations and all six callback types, including balance and all three token grants. Shared schema-derived cases test missing/null/wrong-type fields, enums, exact money boundaries and required headers. Per-callback tests exercise every declared HTTP response plus lost responses, retry exhaustion and both forms of `Retry-After`. Independent Python probes retain processor barriers and multi-worker lease faults.

The [coverage matrix](conformance/coverage-matrix.json) maps required workflows, validation profiles and lifecycle behavior to executable tests. A full run fails if any required test, public endpoint, declared inbound/callback status, JSON request field or validation assertion receipt is missing. JSON/JUnit reports, per-language `.coverage.json` assessments and redacted server logs are written under `.artifacts/conformance/` (ignored by Git). A filtered `--match` run is for debugging and always reports `qualified: false`. These gates describe the pinned local reference scope, not every possible data combination or real PSP certification.

## API and behavior

| Partner-hosted operations | Coverage |
|---|---|
| `POST /charge` | Sync confirmed/rejected, pending, WebView/QR action, manual authorization |
| `POST /charge/{id}/capture`, `POST /charge/{id}/cancel` | Single full/partial capture with unused-hold release; cancel/expiry and concurrent-state protection |
| `GET /find-transaction` | Charge/refund lookup with original Uber ID |
| `POST /refund` | Full/partial refund, pending/rejected completion and ordered late reversal |
| `POST /balance` | Exact E5 available/total and optional itemized balance; signed request **and** scoped bearer |
| `POST /payout` | Sync or pending payout and callback |
| `GET /authorize`, `POST /token` | Explicit demo consent; code, refresh and wallet grants |

Uber calls these paths on the PSP base URL, **not** on Uber's callback host. New integrations use `chargeCallback`, `payoutCallback` and `updateRefund`. The reference also demonstrates `confirmDeposit`, `cancelDeposit` and `updateDeposit`: these are **upstream-deprecated**, opt-in legacy callbacks, exercised by explicit `DEMO_DEPOSIT` approval/denial and reduced-amount flows. Callback routing is configured per funding profile, never inferred from funding-code strings.

Demo profiles include sync/pending/reject, WebView/QR, capture hard/soft decline, refund pending rejection/late reversal, itemized meal-voucher balance, and a separate deposit flow. `contracts/reference-features.json` declares every profile plus triggers for all 14 charge and 10 payout callback reasons. These are local controls, **not** Uber-assigned funding codes. Demo accounts use BRL and begin with synthetic E5 balances. The HTTP contract uses integer E5 amounts, never floating-point money.

## Repository map

```text
contracts/                  Unmodified upstream pin, provenance, derived runtime and errata
implementations/node/       Native TypeScript / Express gateway and tests
implementations/java/       Native Java / Spring Boot gateway and tests
implementations/go/         Native Go / net/http gateway and tests
storage/migrations/         Shared PostgreSQL schema (local reference ledger)
conformance/                Independent Python HTTP harness, mock Uber and JSON fixtures
tools/                      Contract integrity, key and local-development utilities
docs/                       PSP adapter guide, architecture, security and known limits
compose.yaml                Credential-free fake-money environment
.github/workflows/          Per-language tests, conformance and container smoke gates
```

Clone the whole repository and choose one language. The language directories are not standalone packages: each uses the shared `contracts/` and `storage/migrations/` assets, PostgreSQL, and configured keys. The Dockerfiles package the selected gateway with its shared assets; the other two gateways are not runtime dependencies.

The API pin and documented local compatibility profile are in `contracts/`. `make contract` verifies the source hash and reproducible normalization; `tools/check_contract.py --check-live` detects website drift without overwriting the pin.

## PSP documentation

| Guide | What you will learn |
|---|---|
| [PSP integration](docs/psp-integration.md) | Map your processor, wallet identity, funding methods and Uber onboarding configuration |
| [Architecture](docs/architecture.md) | Understand request handling, ledger state, idempotency and callback delivery |
| [Processor executor](docs/processor-executor.md) | Exercise the durable HTTP boundary, retries and unknown-outcome reconciliation |
| [Security](docs/security.md) | Configure signing keys and understand synthetic-data and deployment boundaries |
| [Operations](docs/operations.md) | Inspect blocked work, safely re-drive it and manage local state |
| [Settlement](docs/settlement.md) | Generate and deliver reference reconciliation reports |
| [Testing and scenarios](docs/scenario-format.md) | Run the suite, interpret coverage and add shared HTTP/failure workflows |

## Adapting this reference to your PSP

Start with the integration guide and architecture above. Use the native processor executor to exercise the network boundary against an independent local fixture, then map your actual processor/wallet systems to that boundary. Supply real resource-owner authentication, provision credentials through Uber onboarding, and qualify the chosen contract behavior before external effects. The repository does not include a runnable real-money adapter or production deployment.

Important compatibility defaults:

- Cancelled/expired authorizations are returned as `REJECTED`. `ENABLE_CANCELLED_LOOKUP=true` enables the reference-only `CANCELLED` extension.
- Descriptions accept Unicode and punctuation with the declared type/256-character limit. `STRICT_DESCRIPTION_PATTERN=true` restores the upstream pattern.
- Manual authorization permits one full or partial capture, releases the unused hold and limits refunds to the captured amount.
- Use `DEMO_REFUND_SYNC_REVERSAL` for the single-reversal callback example. The synthetic two-update variant requires `ENABLE_ASYNC_REFUND_REVERSAL=true` and is disabled by default.

The [integration guide](docs/psp-integration.md#3-configure-api-compatibility-and-onboarding) explains compatibility behavior; `contracts/errata.json` records differences from the unchanged upstream pin. Passing local tests does not establish Uber sandbox or production certification.

The [reference settlement workflow](docs/settlement.md) seals immutable per-source JSONL reports and a checksummed manifest, then publishes atomically to a private directory or strict-host-key loopback SFTP. Upload receipts are not financial acceptance or a claim that Uber uses this reference format.

## Verification scope

Conformance runs write their results to `.artifacts/conformance/`; the per-language CI jobs also run unit tests and a container clone-and-run smoke test. The harness builds and controls these reference gateways, including database faults and process restarts; it is not a generic external-PSP URL certification tool. Docker builds and Uber sandbox certification are separate checks; do not infer either from passing native tests. No real transactions, credentials or customer data belong in CI.

License: [Apache-2.0](LICENSE). See [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md).
