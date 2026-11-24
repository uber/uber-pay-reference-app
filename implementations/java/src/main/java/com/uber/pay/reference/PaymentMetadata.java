package com.uber.pay.reference;

import com.fasterxml.jackson.databind.JsonNode;

/** Synthetic fixture policy; not merchant authentication, FX, or KYC. No input logging/storage. */
final class PaymentMetadata {
  static void validate(JsonNode input, long amount) {
    if (!input.has("payment_data")) return;
    JsonNode profile =
        new ReferenceFeatures().method(input.path("funding_method").asText(), "", "");
    for (JsonNode item : input.path("payment_data")) {
      if (profile.path("itemized").asBoolean()
          && item.path("type").asText().equals("meal_voucher_payment_data")) {
        if (!item.path("meal_voucher_merchant_id").asText().equals("demo-merchant")
            || item.has("meal_voucher_merchant_secret")
                && !item.path("meal_voucher_merchant_secret")
                    .asText()
                    .equals("demo-merchant-secret"))
          throw new ApiError(400, "VALIDATION_ERROR", "Invalid synthetic merchant");
      }
      if (profile.path("points").asBoolean()
          && item.path("type").asText().equals("point_conversion_payment_data")) {
        var p = item.path("points_to_charge_e5");
        if (!p.canConvertToLong() || p.asLong() < 0 || amount > 0 && p.asLong() != amount)
          throw new ApiError(400, "VALIDATION_ERROR", "Synthetic 1:1 points mismatch");
      }
    }
  }
}
