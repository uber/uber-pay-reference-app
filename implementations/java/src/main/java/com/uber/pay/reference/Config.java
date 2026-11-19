package com.uber.pay.reference;

import java.net.URI;
import java.nio.file.*;
import java.security.interfaces.RSAPublicKey;
import java.util.*;

final class Config {
  final String processorMode, processorUrl, processorToken, processorKey;
  final int processorTimeout, processorLease, processorWait, processorPoll, processorMaxFailures;
  final String host,
      base,
      db,
      client,
      secret,
      keyId,
      callbackBase,
      tokenUrl,
      uberClient,
      uberSecret,
      publicBase,
      controlSecret;
  final int port, skew, delay, interval, maxAttempts, retryMs, actionTtlMs, refreshTtlSeconds;
  final boolean controls, oauthRequired, strictAccountName, processorPauseAfterPrepare;
  final Set<String> redirects;
  final Path contract, migration;
  final SignatureVerifier signatures;

  Config(Map<String, String> env) {
    if (!get(env, "MODE", "local").equals("local"))
      throw new IllegalArgumentException(
          "Only isolated local mode is supported; supply reviewed real adapters before sandbox"
              + " use");
    processorMode = get(env, "PROCESSOR_MODE", "local");
    if (!Set.of("local", "http").contains(processorMode))
      throw new IllegalArgumentException("Invalid PROCESSOR_MODE");
    processorUrl = get(env, "PROCESSOR_URL", "http://127.0.0.1:9080").replaceAll("/+$", "");
    localUrl(processorUrl);
    processorToken = get(env, "PROCESSOR_AUTH_TOKEN", "");
    processorKey = get(env, "PROCESSOR_STATE_KEY", "");
    if (processorMode.equals("http")
        && (processorToken.length() < 32 || Base64.getDecoder().decode(processorKey).length != 32))
      throw new IllegalArgumentException(
          "HTTP processor requires explicit credentials and persistent 256-bit state key");
    processorTimeout = num(env, "PROCESSOR_TIMEOUT_MS", 2000, 1, 60000);
    processorLease = num(env, "PROCESSOR_LEASE_MS", 15000, 1, 60000);
    processorWait = num(env, "PROCESSOR_WAIT_MS", 250, 1, 60000);
    processorPoll = num(env, "PROCESSOR_POLL_MS", 200, 1, 60000);
    processorMaxFailures = num(env, "PROCESSOR_MAX_FAILURES", 8, 1, 1000);
    host = get(env, "HOST", "127.0.0.1");
    port = num(env, "PORT", 8080, 1, 65535);
    base = get(env, "BASE_PATH", "/uberpay/v1");
    if (!base.matches("(/[A-Za-z0-9_-]+)+"))
      throw new IllegalArgumentException("Invalid BASE_PATH");
    db = get(env, "DATABASE_URL", "postgresql://uberpay:uberpay@127.0.0.1:5432/uberpay");
    client = get(env, "CLIENT_ID", "demo-client");
    secret = get(env, "CLIENT_SECRET", "demo-secret");
    keyId = get(env, "UBER_KEY_ID", "demo-uber-key");
    callbackBase = get(env, "UBER_CALLBACK_BASE_URL", "http://127.0.0.1:9090");
    tokenUrl = get(env, "UBER_TOKEN_URL", "http://127.0.0.1:9090/oauth/token");
    publicBase = get(env, "PUBLIC_BASE_URL", "http://127.0.0.1:" + port + base);
    localUrl(callbackBase);
    localUrl(tokenUrl);
    localUrl(publicBase);
    uberClient = get(env, "UBER_CLIENT_ID", "demo-uber-client");
    uberSecret = get(env, "UBER_CLIENT_SECRET", "demo-uber-secret");
    redirects =
        Set.copyOf(
            Arrays.asList(
                get(
                        env,
                        "ALLOWED_REDIRECT_URIS",
                        "http://127.0.0.1:9090/return,uber://ubercash_topup")
                    .split(",")));
    skew = num(env, "SIGNATURE_MAX_SKEW_SECONDS", 300, 1, 3600);
    delay = num(env, "ASYNC_DELAY_MS", 500, 0, 3600000);
    interval = num(env, "WORKER_INTERVAL_MS", 100, 10, 60000);
    maxAttempts = num(env, "CALLBACK_MAX_ATTEMPTS", 5, 1, 100);
    retryMs = num(env, "CALLBACK_RETRY_MS", 200, 1, 60000);
    actionTtlMs = num(env, "ACTION_TTL_MS", 600000, 1, 3600000);
    refreshTtlSeconds = num(env, "REFRESH_TOKEN_TTL_SECONDS", 2592000, 1, 31536000);
    strictAccountName = get(env, "STRICT_ACCOUNT_NAME", "false").equals("true");
    processorPauseAfterPrepare =
        get(env, "ENABLE_TEST_CONTROL", "false").equals("true")
            && get(env, "PROCESSOR_TEST_PAUSE_AFTER_PREPARE", "false").equals("true");
    controls = get(env, "ENABLE_TEST_CONTROL", "false").equals("true");
    controlSecret = get(env, "TEST_CONTROL_SECRET", "");
    if (controls && controlSecret.length() < 16)
      throw new IllegalArgumentException(
          "Test controls require a secret of at least 16 characters");
    oauthRequired = get(env, "OAUTH_REQUIRED", "false").equals("true");
    contract = Path.of(get(env, "CONTRACT_FILE", "contracts/runtime.json"));
    migration = Path.of(get(env, "MIGRATION_FILE", "storage/migrations/001_init.sql"));
    try {
      var key =
          SignatureVerifier.publicKey(
              Files.readString(
                  Path.of(get(env, "UBER_PUBLIC_KEY_FILE", ".local/uber-public.pem"))));
      if (((RSAPublicKey) key).getModulus().bitLength() < 2048)
        throw new IllegalArgumentException("RSA key must be at least 2048 bits");
      signatures = new SignatureVerifier(Map.of(keyId, key), skew);
    } catch (java.io.IOException e) {
      throw new IllegalArgumentException("Cannot read Uber public key file", e);
    }
  }

  static String get(Map<String, String> e, String k, String d) {
    return e.getOrDefault(k, d);
  }

  static int num(Map<String, String> e, String k, int d, int lo, int hi) {
    int v = Integer.parseInt(get(e, k, "" + d));
    if (v < lo || v > hi) throw new IllegalArgumentException("Invalid " + k);
    return v;
  }

  static void localUrl(String x) {
    URI u = URI.create(x);
    if (!Set.of("http", "https").contains(u.getScheme())
        || u.getUserInfo() != null
        || u.getFragment() != null
        || u.getQuery() != null
        || u.getHost() == null
        || !Set.of("localhost", "127.0.0.1", "::1", "[::1]", "mock-uber", "processor")
            .contains(u.getHost()))
      throw new IllegalArgumentException("Local mode refuses nonlocal outbound URL");
  }
}
