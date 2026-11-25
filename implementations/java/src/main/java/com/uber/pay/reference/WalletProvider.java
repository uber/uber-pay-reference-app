package com.uber.pay.reference;

/** Identity/consent integration seam; the default provider identifies a synthetic local account. */
public interface WalletProvider {
  default boolean isLocalOnly() {
    return false;
  }

  record Identity(String accountId, String fingerprint, String displayName) {}

  Identity identityForConsent();

  Identity exchangeWalletReference(
      String paymentIdentifier, String requestedAccountId, String fundingMethod, String country);

  Identity describeAccount(String accountId);
}

final class DemoWalletProvider implements WalletProvider {
  final String fingerprintKey;

  DemoWalletProvider() {
    this("local-synthetic-instrument-key-v1-only");
  }

  DemoWalletProvider(String key) {
    fingerprintKey = key;
  }

  public boolean isLocalOnly() {
    return true;
  }

  public Identity identityForConsent() {
    return describeAccount("demo-account");
  }

  public Identity exchangeWalletReference(
      String identifier, String requested, String funding, String country) {
    new ReferenceFeatures().method(funding, country, "");
    if (!identifier.equals("demo-account") || !requested.isEmpty() && !requested.equals(identifier))
      throw new ApiError(400, "invalid_grant", "Unknown demo wallet");
    return describeAccount(identifier);
  }

  public Identity describeAccount(String accountId) {
    if (!accountId.equals("demo-account"))
      throw new ApiError(400, "invalid_grant", "Unknown demo identity");
    try {
      var mac = javax.crypto.Mac.getInstance("HmacSHA256");
      mac.init(
          new javax.crypto.spec.SecretKeySpec(
              fingerprintKey.getBytes(java.nio.charset.StandardCharsets.UTF_8), "HmacSHA256"));
      String fingerprint =
          java.util.HexFormat.of()
              .formatHex(
                  mac.doFinal(
                      ("uberpay-instrument-v1|" + accountId)
                          .getBytes(java.nio.charset.StandardCharsets.UTF_8)));
      return new Identity(accountId, fingerprint, "Local demo wallet");
    } catch (java.security.GeneralSecurityException e) {
      throw new IllegalStateException("Instrument identity unavailable", e);
    }
  }
}
