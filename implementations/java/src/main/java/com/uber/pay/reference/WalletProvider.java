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
    return new Identity(accountId, Json.hash(accountId), "Local demo wallet");
  }
}
