# Native Go PSP reference

This is an independent `net/http` implementation of the pinned partner API. It does not forward requests to the Node.js or Java gateway. Only **synthetic local mode** is supported; the executable refuses sandbox/production mode and nonlocal callback, token, and database hosts.

## Build and test

From this directory:

```sh
go test -race ./...
go vet ./...
go build -o ../../.artifacts/bin/uberpay-go ./cmd/psp
```

Run the built binary **from the repository root**, with the configuration described in `contracts/implementation-profile.json`. Contracts, public-key files, and SQL migration paths are resolved from that working directory. The main README and Compose profiles configure these automatically.

From the repository root, run the independent black-box tests:

```sh
.venv/bin/python -m conformance.run --languages go --database-url "$DATABASE_URL"
```

The test runner builds this executable, creates an isolated client namespace, generates local signing keys, starts a mock Uber receiver, runs real HTTP requests, and restarts the gateway for durability cases. It does not call Uber's live APIs.

## Module boundaries

| Module | Responsibility |
| --- | --- |
| `security.go` | Bounded signature parsing, raw-target RSA-SHA256 verification and digest/date checks |
| `contract.go` | Duplicate-key-safe exact-integer JSON and pinned-schema validation |
| `adapter.go` | Typed PSP and wallet interfaces plus synthetic providers |
| `app.go` | Route dispatch, auth-before-replay, operation-specific responses and orchestration |
| `store.go` | PostgreSQL migration, identity, fake balances and durable job insertion |
| `wallet.go` | Explicit local consent, single-use codes, three token grants and family revocation |
| `worker.go` | Completion/expiry, six callback contracts, scoped tokens, leases and retries |
| `processor.go` | Native encrypted-intent executor, private HTTP transport, reconciliation and versioned events |
| `features.go` | Shared funding/reason/locale configuration |
| `control.go` | Secret-protected, disabled-by-default conformance controls |

`NewWithDependencies` permits local test providers to be injected without starting an HTTP listener. All public routes are rooted at `BASE_PATH`; `/health`, `/demo/consent`, `/demo/action`, and disabled-by-default `/_test/*` are separate local-only routes.

## Processor boundary and safety

The sample ledger's debit/reserve/refund effects and idempotent response are committed in one PostgreSQL transaction under a per-client advisory lock. The local PSP adapter makes deterministic decisions using that synthetic state. **Do not implement a remote processor by inserting an HTTP call into these locked adapter methods.** A real processor integration needs a durable dispatch intent, provider idempotency key, timeout/unknown-outcome reconciliation, a trusted identity provider, and an independent deployment/security review before a nonlocal serving profile can be enabled. `LocalOnly()` guards make that limitation explicit.

`PspAdapter` exposes charge, capture, cancel, refund, payout, balance, and operation lookup. `WalletProvider` isolates the fake consent identity and reference exchange. Internal pending/authorized states are mapped to the pinned wire contract; partial capture and CANCELLED lookup follow the accepted reference choices in contracts/decisions.json.

The callback worker performs network I/O outside database transactions. Callback 409 is recorded as `RECONCILE`, permanent errors/retry exhaustion as `FAILED`, and only 2xx as `DONE`. A stable callback identity is reused where the callback supports an idempotency header. Refresh tokens are reusable without rotation in the provisional local profile; authorization-code reuse revokes the issued token family.

## Review-hardened local policies

`ACTION_TTL_MS` defaults 600000; `REFRESH_TOKEN_TTL_SECONDS` defaults 2592000. `STRICT_ACCOUNT_NAME=true` is an explicit compatibility opt-in. Unexpected failures remain 500; callback 401 evicts its token and retries. The native adapter is local/in-process only, not a plug-in network processor. The native `ProcessorExecutor` is the separate HTTP boundary: see `docs/processor-executor.md` for encrypted intents, reservations, out-of-transaction dispatch, fenced finalization and provider-key recovery. It is runnable against the independent local fixture; nonlocal modes remain forbidden. Shared feature profiles include reversal/debt, deposit amount ordering, all reason triggers, funding aliases and bilingual consent/errors. See `docs/review-follow-up.md` for accepted reference decisions and real-integration prerequisites.

Accepted U1–U15 choices, rationale and test evidence are in `contracts/decisions.json`. The standalone reference settlement utility and local SFTP workflow are documented in `docs/settlement.md`; they are not a gateway runtime sidecar or a production settlement agreement.
