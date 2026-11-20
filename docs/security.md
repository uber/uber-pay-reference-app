# Security boundaries

This repository handles synthetic local requests only. It must never be exposed as an unaudited payment processor.

- RSA public keys are validated and signed headers parsed with bounded, strict rules. Digest/signature authentication uses raw bytes before money processing; bearer authorization precedes idempotency replay.
- The gateway refuses real serving modes, external callback/token hosts and remote database hosts in local mode. Callback/token clients do not follow redirects. Container-facing service ports bind to loopback on the host.
- `.local/`, `.env`, database files, build outputs and conformance artifacts are ignored. Local private keys are generated with restrictive permissions; real secrets must never be put into fixture files or Docker images.
- `/_test/*` endpoints are **disabled by default**. The harness enables them only with a fresh random secret and `X-Test-Control-Secret`. They are not additional Uber Pay endpoints and are not part of a partner deployment.
- `/demo/*` is an explicit local identity/payment-consent experience, not a user authentication implementation. Consent submissions are POST and CSRF-bound, not GET side effects or automatic approvals.
- Error output and logs must not expose private keys, signatures, bearer/refresh/code values or real account/payee details. Reports contain only synthetic test data.
- The optional local HTTP processor uses durable encrypted intents and dispatch/reconciliation outside SQL locks. Before real external execution, supply reviewed PSP and wallet providers, secret management, key rotation, TLS/edge controls, rate limits and data retention/access controls. The fake provider's in-database critical section must not wrap an external network call.

Report a vulnerability privately to the repository maintainer/owning team. Do not post credentials or exploit material containing personal/payment data in public issues.

## Review follow-up controls

All supported gateways bound SQL lock wait to 1500ms and statements to 5000ms. Authentication and persisted-account ownership checks precede replay. Active and idle Node connection failures are handled without unhandled error events. Browser action capabilities are256 random bits, scoped to a WAITING job, CSRF-protected, single-use and expiring; GET never mints authority from an Uber transaction ID. Logs omit raw errors, credentials, merchant secrets and personal request fields. Redirects use exact configured URIs. Optional quoted signature created/expires are additional bounds only and cannot replace/extend signed Date freshness.

Only synthetic fixtures may be used locally. Merchant secrets/KYC/customer data are transient in-process adapter inputs in the default mode and never logged. HTTP mode must recover after a crash, so it retains the operation command as AES-256-GCM ciphertext under an explicitly configured persistent 256-bit key; the command includes a pinned funding-configuration hash and authenticated client/intent context. Do not discard or rotate that key/configuration while intents are outstanding. The independent processor fixture stores hashes and synthetic routing/decisions, not raw commands. The disposable database otherwise contains readable IDs, response bodies and states; it is not production encrypted storage. Source settlement ID, payee routing ID and order ID remain in the disposable local database until the developer deletes the volume; there is no automatic production TTL/erasure service. A PSP must specify purpose, encryption, access control, approved retention, erasure, KYC and audit policy before external use.

## Accepted reference decisions and settlement output

Design acceptance of U1–U15 is not external platform certification. The upstream
pin is unchanged; CANCELLED is explicitly labeled a reference runtime extension.
Settlement exports use synthetic identifiers and decimal-string exact amounts,
not KYC or credentials. Private output directories/files, immutable/checksummed
batches, strict pinned-host-key loopback SFTP and explicit private key credentials
protect the reference transfer. Encrypted keys can use a passphrase environment
variable; agent/password fallback and automatic host-key acceptance are disabled.
An upload receipt is not business acceptance, and file permissions do not replace
production at-rest encryption/retention or other partner deployment controls.
