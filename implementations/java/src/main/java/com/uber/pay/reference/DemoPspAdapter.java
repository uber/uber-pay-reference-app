package com.uber.pay.reference;

/** Pure fake-money decisions; PaymentService applies the local ledger effect atomically. */
final class DemoPspAdapter implements PspAdapter {
  public boolean isLocalOnly() {
    return true;
  }

  static final ReferenceFeatures FEATURES = new ReferenceFeatures();

  static void method(String value) {
    FEATURES.method(value, "", "");
  }

  private Decision decide(ChargeCommand c, boolean payout) {
    var p =
        FEATURES.method(
            c.fundingMethod(),
            c.request().path("country_iso2").asText(),
            c.request().path("amount").path("currency").asText());
    PaymentMetadata.validate(c.request(), c.amountE5());
    String behavior = p.path("behavior").asText(), detail = p.path("detail").asText("");
    if (!detail.isEmpty()) {
      boolean valid = false;
      for (var x : FEATURES.data.path(payout ? "payout_reasons" : "charge_reasons"))
        if (x.asText().equals(detail)) valid = true;
      if (!valid) throw new ApiError(400, "VALIDATION_ERROR", "Reason not valid for operation");
    }
    if (c.captureMethod().equals("MANUAL")
        && java.util.Set.of("pending", "pending_reject", "webview", "qr").contains(behavior))
      throw new ApiError(400, "VALIDATION_ERROR", "Manual authorization must be synchronous");
    if (c.amountE5() > c.availableE5())
      return new Decision("REJECTED", "INSUFFICIENT_BALANCE", false);
    if (behavior.equals("reject")) return new Decision("REJECTED", detail, false);
    if (java.util.Set.of("pending", "pending_reject", "webview", "qr").contains(behavior))
      return new Decision("PENDING", null, behavior.equals("webview") || behavior.equals("qr"));
    return new Decision("CONFIRMED", null, false);
  }

  public Decision charge(ChargeCommand c, String key) {
    return decide(c, false);
  }

  public Decision payout(PayoutCommand c, String key) {
    Decision d =
        decide(
            new ChargeCommand(
                c.fundingMethod(), "AUTOMATIC", c.amountE5(), c.availableE5(), c.request()),
            true);
    if (d.state().equals("REJECTED"))
      throw new ApiError(
          java.util.Set.of(
                      "VALIDATION_ERROR", "INVALID_PAYMENT_DETAILS_ERROR", "INSUFFICIENT_BALANCE")
                  .contains(d.detail())
              ? 400
              : 422,
          d.detail(),
          "Synthetic payout decline");
    return d;
  }

  public Decision capture(CaptureCommand c, String providerKey) {
    if (!c.state().equals("AUTHORIZED"))
      throw new ApiError(409, "INVALID_CHARGE_STATE", "Charge is not authorized");
    if (!c.authorizedCurrency().equals(c.currency()))
      throw new ApiError(400, "VALIDATION_ERROR", "Currency mismatch");
    if (c.amountE5() > c.authorizedE5())
      throw new ApiError(400, "EXCEEDS_CAPTURE_LIMIT", "Capture exceeds authorization");
    if (java.util.Set.of("capture_hard", "capture_soft")
        .contains(FEATURES.method(c.fundingMethod(), "", "").path("behavior").asText()))
      return new Decision(
          "REJECTED",
          FEATURES
                  .method(c.fundingMethod(), "", "")
                  .path("behavior")
                  .asText()
                  .equals("capture_soft")
              ? "BANK_SOFT_ERROR"
              : "BANK_HARD_ERROR",
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
    if (FEATURES
        .method(c.fundingMethod(), "", "")
        .path("behavior")
        .asText()
        .equals("refund_sync_reject")) return new Decision("REJECTED", null, false);
    return new Decision(
        java.util.Set.of("pending", "pending_reject", "refund_reversal", "refund_reject")
                .contains(FEATURES.method(c.fundingMethod(), "", "").path("behavior").asText())
            ? "PENDING"
            : "CONFIRMED",
        null,
        false);
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
