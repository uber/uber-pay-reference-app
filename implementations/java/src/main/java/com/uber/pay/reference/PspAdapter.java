package com.uber.pay.reference;

/**
 * Native processor boundary. The shipped adapter is local and performs no network I/O. A network
 * implementation requires durable intent/reconciliation outside database locks; installing an
 * implementation does not enable a nonlocal serving mode.
 */
public interface PspAdapter {
  /** False by default: installing a network processor must not run it under the demo DB lock. */
  default boolean isLocalOnly() {
    return false;
  }

  record ChargeCommand(
      String fundingMethod,
      String captureMethod,
      long amountE5,
      long availableE5,
      com.fasterxml.jackson.databind.JsonNode request) {
    public ChargeCommand(String f, String c, long a, long v) {
      this(f, c, a, v, Json.obj());
    }
  }

  record PayoutCommand(
      String fundingMethod,
      long amountE5,
      long availableE5,
      com.fasterxml.jackson.databind.JsonNode request) {
    public PayoutCommand(String f, long a, long v) {
      this(f, a, v, Json.obj());
    }
  }

  record CaptureCommand(
      String state,
      long authorizedE5,
      String authorizedCurrency,
      long amountE5,
      String currency,
      String fundingMethod) {
    public CaptureCommand(
        String state,
        long authorizedE5,
        String authorizedCurrency,
        long amountE5,
        String currency) {
      this(state, authorizedE5, authorizedCurrency, amountE5, currency, "DEMO_SYNC");
    }
  }

  record CancelCommand(String state) {}

  record RefundCommand(
      String fundingMethod,
      long amountE5,
      long refundableE5,
      com.fasterxml.jackson.databind.JsonNode request) {
    public RefundCommand(String f, long a, long v) {
      this(f, a, v, Json.obj());
    }
  }

  record BalanceCommand(
      String fundingMethod,
      String currency,
      long availableE5,
      long balanceE5,
      com.fasterxml.jackson.databind.JsonNode request) {
    public BalanceCommand(String f, String c, long a, long v) {
      this(f, c, a, v, Json.obj());
    }
  }

  record Snapshot(
      String id,
      String reference,
      String state,
      long amountE5,
      String currency,
      String updatedAt) {}

  /** localSnapshot is null when the reference store does not know this operation. */
  record LookupCommand(String uberId, String providerKey, Snapshot localSnapshot) {}

  record Decision(String state, String detail, boolean action) {}

  record BalanceResult(long availableE5, long balanceE5, String currency) {}

  Decision charge(ChargeCommand command, String providerKey);

  Decision payout(PayoutCommand command, String providerKey);

  Decision capture(CaptureCommand command, String providerKey);

  Decision cancel(CancelCommand command, String providerKey);

  Decision refund(RefundCommand command, String providerKey);

  BalanceResult getBalance(BalanceCommand command);

  /** A null result is an explicitly unknown outcome, not an instruction to repeat an effect. */
  Snapshot lookupOperation(LookupCommand command);
}
