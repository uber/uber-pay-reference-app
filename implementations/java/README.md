# Java PSP reference gateway

This is a native Java 21 / Spring Boot implementation of the pinned Uber Pay protocol. It does not forward to the Node, Go, or Python implementations. The only supported serving mode is `local`, with synthetic accounts and fake-money effects in PostgreSQL.

## Build and run

From the repository root:

```sh
implementations/java/mvnw test package
java -jar implementations/java/target/psp-reference-java-2.0.0.jar
```

The Maven launcher verifies the pinned Maven distribution's SHA-512 before extracting it. Project-local Maven settings use public Maven Central rather than requiring Uber's internal artifact mirror. If Maven is already installed, set `MAVEN_HOME` to its directory.

Use the root quickstart to generate development signing keys and start PostgreSQL / mock Uber. The gateway reads `contracts/implementation-profile.json`'s environment convention. `GET /health` checks actual database connectivity. No tokens, signatures, credentials, or request bodies are logged. Local diagnostic/test controls are disabled by default and require an explicit random secret when enabled.

## Components

- `Gateway`: bounded raw HTTP input, route binding, authentication and response mapping.
- `SignatureVerifier`: bounded Cavage RSA-SHA256 parsing and verification over the original path/query/body; no alternate signature algorithms or undeclared headers.
- `Contracts`: shared pinned schema validation, including composition, required fields, enum/format constraints, and exact signed int64 E5 amounts. Token responses use the grant-selected schema because upstream `oneOf` branches overlap.
- `PaymentService`: native PostgreSQL transaction orchestration, response replay, scoped ledger state and callback work creation.
- `PspAdapter`: public typed ports for charge, capture, cancel, refund, payout, balance and operation lookup. `DemoPspAdapter` is an injected fake-money decision provider.
- `WalletProvider`: public identity/consent and wallet-reference integration seam, injected into `WalletService`; `DemoWalletProvider` represents the explicit synthetic local account.
- `ProcessorExecutor`: encrypted intents, resource/fund reservations, HTTP outside SQL, reconciliation and versioned events.
- `ReferenceFeatures`: shared funding/reason/locale configuration.
- `CallbackWorker`: durable pending completion and callback delivery, scoped token caching, exponential retry, and inspectable failure/reconciliation states.

## Integration boundary and safety

The default `PspAdapter` and `WalletProvider` are Spring beans guarded by `@ConditionalOnMissingBean`, so another local implementation can be injected through application configuration. Both interfaces default to `isLocalOnly() == false`; the local executor refuses such adapters. This guard is not permission to label a network implementation as local.

The fake-money adapter has no external side effect: its decisions and fake ledger writes execute under the same short per-client PostgreSQL transaction/advisory lock. For a network boundary use the supplied native ProcessorExecutor **outside** that database lock; before supporting a real PSP, review/map its private processor transport, provide real identity/consent, replace secret and storage integration, and resolve the documented contract errata. Changing `MODE` or installing a network adapter does not enable sandbox or production serving.

The reference supports one full/partial manual capture, releases unused hold and caps refunds at captured funds. CANCELLED lookup is a documented additive runtime extension. Refunds reserve against the original captured amount. Required idempotency requests replay persisted response bytes/headers and must authenticate before replay. A callback retry cannot run the fake ledger effect again. Permanent callback failures remain `FAILED`; HTTP 409 remains `RECONCILE` rather than being silently treated as successful.

## Verification

```sh
implementations/java/mvnw test package
.venv/bin/python -m conformance.run --languages java --database-url "$DATABASE_URL" --skip-build
```

Unit tests cover exact int64 values, schema composition, bounded malformed signatures, stale/future signatures, raw-target/body tampering, all callback builders, all public adapter ports, and nonlocal-mode guards. The root black-box suite tests the same server-visible behaviors as Node and Go, including concurrent duplication, capture/cancel races, explicit consent, all token grants, callbacks/retries, and process restart.

## Review-hardened local policies

`ACTION_TTL_MS` defaults 600000; `REFRESH_TOKEN_TTL_SECONDS` defaults 2592000. `STRICT_ACCOUNT_NAME=true` is an explicit compatibility opt-in. Unexpected failures remain 500; callback 401 evicts its token and retries. The native adapter is local/in-process only, not a plug-in network processor. The native `ProcessorExecutor` is the separate HTTP boundary: see `docs/processor-executor.md` for encrypted intents, reservations, out-of-transaction dispatch, fenced finalization and provider-key recovery. It is runnable against the independent local fixture; nonlocal modes remain forbidden. Shared feature profiles include reversal/debt, deposit amount ordering, all reason triggers, funding aliases and bilingual consent/errors. See `docs/review-follow-up.md` for accepted reference decisions and real-integration prerequisites.

Accepted U1–U15 choices, rationale and test evidence are in `contracts/decisions.json`. The standalone reference settlement utility and local SFTP workflow are documented in `docs/settlement.md`; they are not a gateway runtime sidecar or a production settlement agreement.
