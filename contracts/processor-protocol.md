# Processor adapter transport (reference protocol, NOT an Uber API)

This optional HTTP seam makes the durable executor runnable against a separate processor.
An adopter implements/maps this seam to its processor, wallet and identity systems. It
is not a claim that a particular bank or PSP uses these paths. `MODE=local` still forbids
real hosts; `PROCESSOR_MODE=http` demonstrates the complete network boundary on a local
fixture. Uber onboarding, credentials and approval of the recorded errata remain external.

## Configuration and authentication

`PROCESSOR_MODE=local|http` (default local), `PROCESSOR_URL`, `PROCESSOR_AUTH_TOKEN`
(at least 32 characters), and `PROCESSOR_STATE_KEY` (base64 of 32 random bytes) configure
HTTP mode. Requests use bearer authentication, JSON, a bounded timeout and no redirects.
Durable commands are AES-256-GCM encrypted (12-byte nonce, ciphertext, 16-byte tag,
concatenated and base64 encoded); AAD is `client_id + "|" + intent_id`. No raw command,
merchant secret, KYC or bearer value is logged. The encryption key must survive restarts.

## Submit and reconcile

- `POST /operations/{providerKey}` with `{operation, request, resource}` submits a
  command. `operation` is charge/capture/cancel/initRefund/payout, `request` is the
  validated Uber request, `resource` identifies the original charge for capture/cancel.
- `GET /operations/{providerKey}` returns the SAME decision for a known operation,
  HTTP404 only for a definitely absent key, or HTTP202 for an unresolved operation.
- Repeating POST with the same provider key and command must not repeat an effect.
  A changed command is HTTP409. The executor never invents a new key after a timeout.
- A decision has `{state, psp_reference, detail?, actions?, version?, terminal?}`.
  State is CONFIRMED/REJECTED/PENDING for charge/payout/refund; CONFIRMED/REJECTED
  for capture; CANCELLED for successful cancellation. Manual CONFIRMED is an
  authorization acknowledgment, not a debit. Reference is immutable per operation.
  The detail must be a valid operation-specific Uber reason; the gateway validates it.
- An initial PENDING decision is acknowledged to Uber only after tracking state is
  durable. Polling/reconciliation then obtains the final state and commits the ledger
  transition plus callback atomically. Processor-hosted action URLs remain processor
  owned; the gateway does not mint consent for arbitrary transaction IDs.

The executor commits an intent and short-lived resource/fund reservations BEFORE HTTP,
performs HTTP with NO SQL connection or lock held, and finalizes in a second transaction.
Leases use monotonically increasing fencing generations. A duplicate waits within a
bounded request budget and then returns retryable500 if unresolved. Recovery looks up
providerKey before safely replaying an absent operation. Unknown outcomes retain their
reservations for reconciliation; no new debit is guessed.

## Local fixture versus real processing

The fixture moves synthetic funds only and supports deterministic decline, delay,
response-loss and finalization scenarios. Provider effects persist independently of the
gateway so killing the gateway cannot erase the evidence needed for reconciliation.
A real adapter must give the same durable idempotency and lookup guarantees. Local
conformance is not sandbox certification, and the reference never enables production
serving merely because a processor URL is configured.

## Decided capture and deposit policies

One capture may be smaller than or equal to its original authorization; a success
closes the authorization and releases the remainder. It is not multi-capture.
For confirmation of an existing pending deposit-mode charge only, a versioned
decision may contain `amount:{value,currency}`; it must be positive, no larger than
the held amount, and the same currency. The gateway atomically releases unused
hold and queues updateDeposit before confirmDeposit. The initial PENDING response
is still replayed unchanged. Other amount adjustments fail closed for reconciliation.
