package com.uber.pay.reference;

import java.nio.charset.StandardCharsets;
import java.nio.file.*;
import java.security.*;
import java.security.spec.X509EncodedKeySpec;
import java.time.*;
import java.time.format.DateTimeFormatter;
import java.util.*;
import java.util.regex.*;

final class SignatureVerifier {
  private static final Pattern PART =
      Pattern.compile("\\s*([A-Za-z][A-Za-z0-9_-]*)=\"([^\"\\r\\n]*)\"\\s*(?:,|$)");
  private final Map<String, PublicKey> keys;
  private final long skew;

  SignatureVerifier(Map<String, PublicKey> keys, long skew) {
    this.keys = Map.copyOf(keys);
    this.skew = skew;
  }

  static PublicKey publicKey(String pem) {
    try {
      String p =
          pem.replace("-----BEGIN PUBLIC KEY-----", "")
              .replace("-----END PUBLIC KEY-----", "")
              .replaceAll("\\s", "");
      return KeyFactory.getInstance("RSA")
          .generatePublic(new X509EncodedKeySpec(Base64.getDecoder().decode(p)));
    } catch (Exception e) {
      throw new IllegalArgumentException("Invalid RSA public key", e);
    }
  }

  static Map<String, String> parse(String value) {
    if (value == null || value.length() > 8192 || value.stripTrailing().endsWith(",")) throw bad();
    Map<String, String> out = new HashMap<>();
    Matcher m = PART.matcher(value);
    int p = 0;
    while (p < value.length()) {
      m.region(p, value.length());
      if (!m.lookingAt() || out.putIfAbsent(m.group(1), m.group(2)) != null) throw bad();
      p = m.end();
    }
    if (!out.keySet().containsAll(Set.of("keyId", "algorithm", "headers", "signature"))
        || !Set.of("keyId", "algorithm", "headers", "signature", "created", "expires")
            .containsAll(out.keySet())) throw bad();
    return out;
  }

  void verify(String method, String target, String host, Map<String, String> headers, byte[] raw) {
    try {
      String digest = headers.get("digest"), date = headers.get("date");
      String expected =
          "SHA-256="
              + Base64.getEncoder()
                  .encodeToString(MessageDigest.getInstance("SHA-256").digest(raw));
      if (digest == null
          || !MessageDigest.isEqual(
              expected.getBytes(StandardCharsets.US_ASCII),
              digest.getBytes(StandardCharsets.US_ASCII))) throw bad();
      Instant when = ZonedDateTime.parse(date, DateTimeFormatter.RFC_1123_DATE_TIME).toInstant();
      if (!DateTimeFormatter.ofPattern("EEE, dd MMM uuuu HH:mm:ss 'GMT'", Locale.US)
          .withZone(ZoneOffset.UTC)
          .format(when)
          .equals(date)) throw bad();
      if (Math.abs(Instant.now().getEpochSecond() - when.getEpochSecond()) > skew) throw bad();
      Map<String, String> s = parse(headers.get("signature"));
      if (!s.get("algorithm").equals("rsa-sha256")
          || !s.get("headers").equals("(request-target) host date digest")
          || !keys.containsKey(s.get("keyId"))) throw bad();
      for (String field : List.of("created", "expires"))
        if (s.containsKey(field)) {
          if (!s.get(field).matches("[0-9]{1,12}")) throw bad();
          long seconds = Long.parseLong(s.get(field)), now = Instant.now().getEpochSecond();
          if (field.equals("created") && Math.abs(now - seconds) > skew
              || field.equals("expires") && seconds < now) throw bad();
        }
      if (s.containsKey("created")
          && s.containsKey("expires")
          && Long.parseLong(s.get("expires")) < Long.parseLong(s.get("created"))) throw bad();
      String canonical =
          "(request-target): "
              + method.toLowerCase(Locale.ROOT)
              + " "
              + target
              + "\nhost: "
              + host
              + "\ndate: "
              + date
              + "\ndigest: "
              + digest;
      Signature v = Signature.getInstance("SHA256withRSA");
      v.initVerify(keys.get(s.get("keyId")));
      v.update(canonical.getBytes(StandardCharsets.UTF_8));
      if (!v.verify(Base64.getDecoder().decode(s.get("signature")))) throw bad();
    } catch (ApiError e) {
      throw e;
    } catch (Exception e) {
      throw bad();
    }
  }

  private static ApiError bad() {
    return new ApiError(401, "invalid_signature", "Invalid request authentication");
  }
}
