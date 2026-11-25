package com.uber.pay.reference;

import static org.junit.jupiter.api.Assertions.*;

import com.fasterxml.jackson.databind.JsonNode;
import java.nio.charset.StandardCharsets;
import java.nio.file.*;
import java.security.*;
import java.time.*;
import java.time.format.DateTimeFormatter;
import java.util.*;
import org.junit.jupiter.api.*;

class CoreTest {
  static KeyPair keys;
  static Contracts contracts;

  @BeforeAll
  static void setup() throws Exception {
    KeyPairGenerator generator = KeyPairGenerator.getInstance("RSA");
    generator.initialize(2048);
    keys = generator.generateKeyPair();
    Path p = Path.of("").toAbsolutePath();
    while (p != null && !Files.exists(p.resolve("contracts/runtime.json"))) p = p.getParent();
    assertNotNull(p);
    contracts = new Contracts(p.resolve("contracts/runtime.json"));
  }

  Map<String, String> headers(byte[] raw, String target, Instant time) throws Exception {
    String date =
        DateTimeFormatter.ofPattern("EEE, dd MMM uuuu HH:mm:ss 'GMT'", Locale.US)
            .format(time.atZone(ZoneOffset.UTC));
    String digest =
        "SHA-256="
            + Base64.getEncoder().encodeToString(MessageDigest.getInstance("SHA-256").digest(raw));
    String canonical =
        "(request-target): post "
            + target
            + "\nhost: localhost:8080\ndate: "
            + date
            + "\ndigest: "
            + digest;
    Signature s = Signature.getInstance("SHA256withRSA");
    s.initSign(keys.getPrivate());
    s.update(canonical.getBytes(StandardCharsets.UTF_8));
    return new HashMap<>(
        Map.of(
            "date",
            date,
            "digest",
            digest,
            "signature",
            "keyId=\"test\",algorithm=\"rsa-sha256\",headers=\"(request-target) host date"
                + " digest\",signature=\""
                + Base64.getEncoder().encodeToString(s.sign())
                + "\""));
  }

  @Test
  void validSignatureUsesExactRawTargetAndBody() throws Exception {
    var v = new SignatureVerifier(Map.of("test", keys.getPublic()), 300);
    byte[] raw = "{ \"n\": 1 }".getBytes(StandardCharsets.UTF_8);
    var h = headers(raw, "/x?a=%20&b=1", Instant.now());
    assertDoesNotThrow(() -> v.verify("POST", "/x?a=%20&b=1", "localhost:8080", h, raw));
    assertThrows(ApiError.class, () -> v.verify("POST", "/x?b=1&a=%20", "localhost:8080", h, raw));
    assertThrows(
        ApiError.class,
        () -> v.verify("POST", "/x?a=%20&b=1", "localhost:8080", h, "{\"n\":1}".getBytes()));
  }

  @Test
  void staleFutureUnknownAndWrongAlgorithmRejected() throws Exception {
    var v = new SignatureVerifier(Map.of("test", keys.getPublic()), 300);
    for (Instant time : List.of(Instant.now().minusSeconds(601), Instant.now().plusSeconds(601)))
      assertThrows(
          ApiError.class,
          () ->
              v.verify(
                  "POST", "/x", "localhost:8080", headers(new byte[0], "/x", time), new byte[0]));
    for (String from : List.of("test", "rsa-sha256", "(request-target) host date digest")) {
      var h = headers(new byte[0], "/x", Instant.now());
      h.put("signature", h.get("signature").replace(from, "invalid"));
      assertThrows(ApiError.class, () -> v.verify("POST", "/x", "localhost:8080", h, new byte[0]));
    }
  }

  @Test
  void malformedHeadersAreBounded() {
    for (String s :
        List.of(
            "",
            "x",
            "keyId=",
            "keyId=\"unterminated",
            "keyId=\"x\",",
            "x=\"a\",x=\"b\"",
            "x".repeat(9000)))
      assertTimeoutPreemptively(
          Duration.ofSeconds(1),
          () -> assertThrows(ApiError.class, () -> SignatureVerifier.parse(s)));
  }

  @Test
  void duplicateJsonKeysRejected() {
    assertThrows(ApiError.class, () -> Json.parse("{\"id\":1,\"id\":2}"));
  }

  @Test
  void signedInt64IsLosslessAndOverflowRejected() {
    JsonNode schema = contracts.spec.path("components").path("schemas").path("CurrencyAmount");
    JsonNode exact = Json.parse("{\"value\":9223372036854775807,\"currency\":\"BRL\"}");
    assertDoesNotThrow(() -> contracts.validate(schema, exact));
    assertTrue(Json.string(exact).contains("9223372036854775807"));
    assertThrows(
        ApiError.class,
        () ->
            contracts.validate(
                schema, Json.parse("{\"value\":9223372036854775808,\"currency\":\"BRL\"}")));
    assertThrows(
        ApiError.class,
        () -> contracts.validate(schema, Json.parse("{\"value\":1.5,\"currency\":\"BRL\"}")));
  }

  @Test
  void schemaHonorsCompositionEnumRequiredAndUnknownFields() {
    JsonNode schema = contracts.spec.path("components").path("schemas").path("Authorization");
    assertDoesNotThrow(
        () ->
            contracts.validate(
                schema, Json.obj("type", "web_view_authorization", "future", "accepted")));
    assertThrows(
        ApiError.class,
        () -> contracts.validate(schema, Json.obj("type", "access_token_authorization")));
    assertThrows(ApiError.class, () -> contracts.validate(schema, Json.obj("type", "unknown")));
  }

  @Test
  void fakeAdapterDoesNotAllowPendingManualOrInventPayoutRejection() {
    PspAdapter a = new DemoPspAdapter();
    assertEquals(
        "CONFIRMED",
        a.charge(new PspAdapter.ChargeCommand("DEMO_SYNC", "MANUAL", 1, 1), "k").state());
    assertEquals(
        "REJECTED",
        a.charge(new PspAdapter.ChargeCommand("DEMO_SYNC", "AUTOMATIC", 2, 1), "k").state());
    assertTrue(
        a.charge(new PspAdapter.ChargeCommand("DEMO_WEBVIEW", "AUTOMATIC", 1, 2), "k").action());
    assertThrows(
        ApiError.class,
        () -> a.charge(new PspAdapter.ChargeCommand("DEMO_PENDING", "MANUAL", 1, 2), "k"));
    assertThrows(
        ApiError.class, () -> a.payout(new PspAdapter.PayoutCommand("DEMO_REJECT", 1, 2), "k"));
  }

  @Test
  void noRealModeOrNonlocalOutboundHosts() {
    assertThrows(IllegalArgumentException.class, () -> new Config(Map.of("MODE", "production")));
    for (String u :
        List.of(
            "http://evil.example",
            "http://user@localhost",
            "http://localhost/#secret",
            "file:///tmp/x"))
      assertThrows(IllegalArgumentException.class, () -> Config.localUrl(u));
    assertDoesNotThrow(() -> Config.localUrl("http://mock-uber:9090"));
  }

  @Test
  void typedAdapterPortsCoverManualRefundBalanceAndLookup() {
    PspAdapter a = new DemoPspAdapter();
    assertEquals(
        "CONFIRMED",
        a.capture(new PspAdapter.CaptureCommand("AUTHORIZED", 10, "BRL", 10, "BRL"), "stable")
            .state());
    assertThrows(
        ApiError.class,
        () ->
            a.capture(new PspAdapter.CaptureCommand("AUTHORIZED", 10, "BRL", 11, "BRL"), "stable"));
    assertEquals(
        "CONFIRMED",
        a.capture(new PspAdapter.CaptureCommand("AUTHORIZED", 10, "BRL", 1, "BRL"), "stable")
            .state());
    assertEquals(
        "CANCELLED", a.cancel(new PspAdapter.CancelCommand("AUTHORIZED"), "stable").state());
    assertThrows(
        ApiError.class, () -> a.cancel(new PspAdapter.CancelCommand("CONFIRMED"), "stable"));
    assertEquals(
        "PENDING", a.refund(new PspAdapter.RefundCommand("DEMO_PENDING", 1, 2), "stable").state());
    assertThrows(
        ApiError.class, () -> a.refund(new PspAdapter.RefundCommand("DEMO_SYNC", 3, 2), "stable"));
    assertEquals(
        2, a.getBalance(new PspAdapter.BalanceCommand("DEMO_SYNC", "BRL", 1, 2)).balanceE5());
    var snapshot =
        new PspAdapter.Snapshot("id", "reference", "CONFIRMED", 2, "BRL", "2026-01-01T00:00:00Z");
    assertSame(snapshot, a.lookupOperation(new PspAdapter.LookupCommand("id", "stable", snapshot)));
    assertNull(a.lookupOperation(new PspAdapter.LookupCommand("missing", "stable", null)));
  }

  @Test
  void walletIdentityIsExplicitlyLocalAndUnknownReferencesFail() {
    WalletProvider p = new DemoWalletProvider();
    assertTrue(p.isLocalOnly());
    assertEquals("demo-account", p.identityForConsent().accountId());
    assertEquals(
        p.identityForConsent(), p.exchangeWalletReference("demo-account", "", "DEMO_SYNC", "BR"));
    assertThrows(ApiError.class, () -> p.exchangeWalletReference("other", "", "DEMO_SYNC", "BR"));
  }

  @Test
  void networkAdaptersCannotBeInstalledUnderDemoTransactionLock() {
    PspAdapter remote =
        (PspAdapter)
            java.lang.reflect.Proxy.newProxyInstance(
                getClass().getClassLoader(),
                new Class<?>[] {PspAdapter.class},
                (proxy, method, args) -> method.getName().equals("isLocalOnly") ? false : null);
    assertThrows(
        IllegalArgumentException.class, () -> new PaymentService(null, null, contracts, remote));
    WalletProvider remoteWallet =
        (WalletProvider)
            java.lang.reflect.Proxy.newProxyInstance(
                getClass().getClassLoader(),
                new Class<?>[] {WalletProvider.class},
                (proxy, method, args) -> method.getName().equals("isLocalOnly") ? false : null);
    assertThrows(
        IllegalArgumentException.class,
        () -> new WalletService(null, null, contracts, remoteWallet));
  }

  @Test
  void callbackContractsAreTyped() {
    contracts.callback(
        "chargeCallback", Json.obj("state", Json.obj("value", "CONFIRMED"), "psp_reference", "p"));
    contracts.callback(
        "payoutCallback", Json.obj("state", Json.obj("value", "REJECTED"), "psp_reference", "p"));
    contracts.callback("updateRefund", Json.obj("status", "CONFIRMED"));
    contracts.callback("confirmDeposit", Json.obj("merchant_reference", "p"));
    contracts.callback("cancelDeposit", Json.obj("cancel_reason", "USER_CANCELLED"));
    contracts.callback("updateDeposit", Json.obj("merchant_reference", "p"));
    assertThrows(
        ApiError.class,
        () ->
            contracts.callback("payoutCallback", Json.obj("state", Json.obj("value", "PENDING"))));
    assertThrows(ApiError.class, () -> contracts.callback("unknown", Json.obj()));
  }

  @Test
  void canonicalJsonIgnoresObjectOrderingNotIntegers() {
    assertEquals(
        Json.string(Json.canonical(Json.parse("{\"b\":2,\"a\":9223372036854775807}"))),
        Json.string(Json.canonical(Json.parse("{\"a\":9223372036854775807,\"b\":2}"))));
  }
}
