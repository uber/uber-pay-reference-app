# Uber Pay PSP reference app

A self-contained **PSP-hosted** integration reference for Uber Pay API **v1.4**, implemented natively in **Node.js/TypeScript, Java, and Go**. Choose one implementation; each exposes the same nine partner operations and is tested by the same wire-level suite.

**Synthetic local funds only.** This is not an Uber-operated payment processor or a production PSP SDK. All bundled processor and wallet identities are demonstrations. Non-local serving modes and non-local Uber callback destinations fail closed. Real PSP processing, Uber onboarding, contract clarification and security/financial review are required before a sandbox or live integration; changing an environment label is not sufficient.

## Clone and run a local example

Install Docker Engine with Compose v2, Git and Make. No Uber credentials are required.

```sh
git clone https://github.com/Arka-Uber-Open/uber-pay-reference-app.git
cd uber-pay-reference-app
make up LANGUAGE=node       # alternatives: java or go
make demo LANGUAGE=node
```

Compose builds the selected native gateway, PostgreSQL, a key-generation job and a fake Uber callback/OAuth service. The demo sends a correctly signed synthetic charge and looks it up. The gateway is at `http://127.0.0.1:8080/uberpay/v1`; readiness is `GET http://127.0.0.1:8080/health`. Do not enable two language profiles on port 8080 simultaneously.

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

The harness launches native processes, signs the exact request bytes, validates responses **independently** against the contract, receives all six callback types, injects failures, tests concurrent requests, and restarts gateways. No language proxies its implementation through another. Required cases are not silently skipped. JSON and JUnit reports plus redacted server logs are written under `.artifacts/conformance/` (ignored by Git). Ten versioned [JSON workflows](docs/scenario-format.md) include typed bindings, concurrency, SQL faults, forms and process restart; independent Python probes cover processor barriers and multi-worker lease faults. All run against every language. JSON fixtures live in `conformance/fixtures/`; `--match` is available for debugging but is not a release qualification run.

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

Uber calls these paths on the PSP base URL, **not** on Uber's callback host. Callback clients cover `chargeCallback`, `payoutCallback`, `updateRefund`, `confirmDeposit`, `cancelDeposit` and `updateDeposit`. The latter three are exercised by explicit `DEMO_DEPOSIT` approval/denial and reduced-amount flows, not an automatic routing rule for ordinary charges. Accepted U11 selects routing explicitly per asynchronous funding profile; it is not inferred from funding-code strings.

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

The clone is self-contained: the API pin and documented local compatibility profile are in `contracts/`. `make contract` verifies the source hash and reproducible normalization; `tools/check_contract.py --check-live` detects website drift without overwriting the pin. See [review disposition and remaining gates](docs/review-follow-up.md) before treating schema acceptance as feature support.

## Adapting this reference to your PSP

Read [the integration guide](docs/psp-integration.md) and [architecture](docs/architecture.md). Use the native [durable HTTP processor executor](docs/processor-executor.md) to exercise the network boundary against an independent local fixture, then map your actual processor/wallet systems to that boundary. Follow the accepted reference profile and map it explicitly to the actual counterparty contract before external effects. There is intentionally no runnable real-money adapter, fabricated user authentication, production deployment or a real-counterparty settlement agreement.

U1–U15 are accepted reference-project choices in [the decision register](contracts/decisions.json), with rationale and executable evidence. The derived runtime explicitly adds CANCELLED lookup and supports one partial capture with remainder release; the upstream pin is unchanged. Project design acceptance is not Uber sandbox/production certification. See [owner decisions](docs/owner-decisions.md).

The [reference settlement workflow](docs/settlement.md) seals immutable per-source JSONL reports and a checksummed manifest, then publishes atomically to a private directory or strict-host-key loopback SFTP. Upload receipts are not financial acceptance or a claim that Uber uses this reference format.

This is an intentional replacement of the old deposit-only UI. Its source is recoverable at Git commit `100c5f8`; it is not served or supported by version 2. No live data migration is involved.

## Verification scope

Native build/unit and full shared conformance results are recorded in [implementation status](docs/IMPLEMENTATION_STATUS.md). Docker builds and Uber sandbox certification are separate checks; do not infer either from passing native tests. No real transactions, credentials or customer data belong in CI.

License: [Apache-2.0](LICENSE). See [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md).
