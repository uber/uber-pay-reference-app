package com.uber.pay.reference;

/** Pure fake-money decisions; PaymentService applies the local ledger effect atomically. */
final class DemoPspAdapter implements PspAdapter {
  public boolean isLocalOnly() {
    return true;
  }

  private static final java.util.Set<String> METHODS =
      java.util.Set.of(
          "DEMO_SYNC",
          "DEMO_CAPTURE_HARD",
          "DEMO_CAPTURE_SOFT",
          "DEMO_REJECT",
          "DEMO_PENDING",
          "DEMO_PENDING_REJECT",
          "DEMO_WEBVIEW",
          "DEMO_QR");

  static void method(String value) {
    if (!METHODS.contains(value))
      throw new ApiError(400, "VALIDATION_ERROR", "Unknown demo funding method");
  }

  public Decision charge(ChargeCommand c, String providerKey) {
    method(c.fundingMethod());
    PaymentMetadata.validate(c.request(), c.amountE5());
    if (c.captureMethod().equals("MANUAL")
        && !(c.fundingMethod().equals("DEMO_SYNC")
            || c.fundingMethod().startsWith("DEMO_CAPTURE_")
            || c.fundingMethod().equals("DEMO_REJECT")))
      throw new ApiError(400, "VALIDATION_ERROR", "Manual authorization must be synchronous");
    if (c.fundingMethod().equals("DEMO_REJECT") || c.amountE5() > c.availableE5())
      return new Decision("REJECTED", "INSUFFICIENT_BALANCE", false);
    return new Decision(
        c.fundingMethod().equals("DEMO_SYNC") || c.fundingMethod().startsWith("DEMO_CAPTURE_")
            ? "CONFIRMED"
            : "PENDING",
        null,
        c.fundingMethod().equals("DEMO_WEBVIEW") || c.fundingMethod().equals("DEMO_QR"));
  }

  public Decision payout(PayoutCommand c, String providerKey) {
    Decision d =
        charge(
            new ChargeCommand(
                c.fundingMethod(), "AUTOMATIC", c.amountE5(), c.availableE5(), c.request()),
            providerKey);
    if (d.state().equals("REJECTED"))
      throw new ApiError(400, "INSUFFICIENT_BALANCE", "Payout rejected");
    return d;
  }

  public Decision capture(CaptureCommand c, String providerKey) {
    if (!c.state().equals("AUTHORIZED"))
      throw new ApiError(409, "INVALID_CHARGE_STATE", "Charge is not authorized");
    if (!c.authorizedCurrency().equals(c.currency()))
      throw new ApiError(400, "VALIDATION_ERROR", "Currency mismatch");
    if (c.amountE5() > c.authorizedE5())
      throw new ApiError(400, "EXCEEDS_CAPTURE_LIMIT", "Capture exceeds authorization");
    if (c.amountE5() != c.authorizedE5())
      throw new ApiError(400, "VALIDATION_ERROR", "Partial capture requires an agreed contract");
    if (c.fundingMethod().startsWith("DEMO_CAPTURE_"))
      return new Decision(
          "REJECTED",
          c.fundingMethod().equals("DEMO_CAPTURE_SOFT") ? "BANK_SOFT_ERROR" : "BANK_HARD_ERROR",
          false);
    return new Decision("CONFIRMED", null, false);
  }

  public Decision cancel(CancelCommand c, String providerKey) {
    if (!c.state().equals("AUTHORIZED"))
      throw new ApiError(409, "INVALID_CHARGE_STATE", "Charge is not authorized");
    return new Decision("CANCELLED", null, false);
  }

  public Decision refund(RefundCommand c, String providerKey) {
    if (c.amountE5() > c.refundableE5())
      throw new ApiError(400, "VALIDATION_ERROR", "Refund exceeds captured amount");
    return new Decision(
        c.fundingMethod().startsWith("DEMO_PENDING") ? "PENDING" : "CONFIRMED", null, false);
  }

  public BalanceResult getBalance(BalanceCommand c) {
    method(c.fundingMethod());
    PaymentMetadata.validate(c.request(), 0);
    if (c.availableE5() < 0 || c.balanceE5() < c.availableE5())
      throw new IllegalStateException("Invalid fake ledger balance");
    return new BalanceResult(c.availableE5(), c.balanceE5(), c.currency());
  }

  public Snapshot lookupOperation(LookupCommand c) {
    return c.localSnapshot();
  }
}
