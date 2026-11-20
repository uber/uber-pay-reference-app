# Native Node.js reference gateway

This is an independently running TypeScript/Express implementation; it does not
delegate payment logic to Python, Go, or Java. Run it **from the repository root**
so that the shared contract, SQL migration, and development keys resolve correctly.

```sh
npm ci --prefix implementations/node
npm test --prefix implementations/node
npm run build --prefix implementations/node
DATABASE_URL=postgresql://uberpay:uberpay@127.0.0.1:5432/uberpay \
  node implementations/node/dist/main.js
```

Generate local keys and start PostgreSQL/mock Uber using the root README or
Compose instructions first. Configuration and the behavioral choices common to
all languages are in `contracts/implementation-profile.json`.

## Implementation boundaries

- `app.ts`: injectable HTTP app; bounded raw-body capture, request authentication,
  route selection, local consent, and secret-protected test controls.
- `contract.ts`: upstream request/response and callback validation, including
  references and composition; the runtime contract records known upstream errata.
- `codec.ts`: exact large-integer JSON and E5 amount checks.
- `security.ts`: bounded Cavage signature parser, RSA-SHA256, body digest and Date.
- `service.ts`: idempotency, transaction states, synthetic ledger and wallet grants.
- `store.ts`: PostgreSQL migration, seed and per-client advisory transactions.
- `worker.ts`: restart-safe synthetic completion and callback delivery outside locks.
- `processor.ts`: encrypted durable HTTP intents, reservations, lookup recovery and versioned events.
- `features.ts`: shared funding/reason/locale configuration.
- `adapter.ts`: explicit local-only synthetic processor interface.

## Safety boundary

Only `MODE=local` is accepted. Outbound Uber hosts are limited to the local mock
service. The default identity and consent flow is a **fake `demo-account` wallet**,
not a real login provider. No production serving mode or real PSP connector is
bundled.

The local adapter's methods run within the same PostgreSQL transaction as the
synthetic ledger and must remain in-process and network-free. Do **not** replace
one with a real HTTP payment call inside that transaction. Integrating a real PSP
uses the supplied native provider-intent/reconciliation executor outside the database
lock and additionally requires a real wallet identity/consent provider, key/secret provisioning, environment
and contract-errata review, and independent sandbox qualification.

The default refresh grant intentionally obtains an access token without
rotating the refresh token; a production token-rotation/revocation policy is
not claimed by this local reference example.

## Review-hardened local policies

`ACTION_TTL_MS` defaults 600000; `REFRESH_TOKEN_TTL_SECONDS` defaults 2592000. `STRICT_ACCOUNT_NAME=true` is an explicit compatibility opt-in. Unexpected failures remain 500; callback 401 evicts its token and retries. The native adapter is local/in-process only, not a plug-in network processor. The native `ProcessorExecutor` is the separate HTTP boundary: see `docs/processor-executor.md` for encrypted intents, reservations, out-of-transaction dispatch, fenced finalization and provider-key recovery. It is runnable against the independent local fixture; nonlocal modes remain forbidden. Shared feature profiles include reversal/debt, deposit amount ordering, all reason triggers, funding aliases and bilingual consent/errors. See `docs/review-follow-up.md` for accepted reference decisions and real-integration prerequisites.

Accepted U1–U15 choices, rationale and test evidence are in `contracts/decisions.json`. The standalone reference settlement utility and local SFTP workflow are documented in `docs/settlement.md`; they are not a gateway runtime sidecar or a production settlement agreement.
