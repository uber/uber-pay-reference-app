package com.uber.pay.reference;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.node.ObjectNode;
import java.nio.file.*;
import java.util.*;

final class ReferenceFeatures {
  final JsonNode data, aliases;

  ReferenceFeatures() {
    try {
      String configured = System.getenv().getOrDefault("REFERENCE_FEATURES_FILE", "");
      Path filePath;
      if (configured.isEmpty()) {
        Path root = Path.of("").toAbsolutePath();
        while (root != null && !Files.exists(root.resolve("contracts/reference-features.json")))
          root = root.getParent();
        if (root == null) throw new IllegalArgumentException("Missing reference features");
        filePath = root.resolve("contracts/reference-features.json");
      } else filePath = Path.of(configured);
      data = Json.parse(Files.readAllBytes(filePath));
      String file = System.getenv().getOrDefault("FUNDING_METHODS_FILE", "");
      aliases = file.isEmpty() ? Json.obj() : Json.parse(Files.readAllBytes(Path.of(file)));
      for (var it = aliases.fields(); it.hasNext(); ) {
        var e = it.next();
        var a = e.getValue();
        a.fieldNames()
            .forEachRemaining(
                k -> {
                  if (!Set.of("method", "countries", "currencies", "callback_mode").contains(k))
                    throw new IllegalArgumentException("Unsupported funding policy field");
                });
        if (a.has("callback_mode")) {
          String mode = a.path("callback_mode").asText();
          if (!Set.of("charge", "deposit").contains(mode))
            throw new IllegalArgumentException("Invalid callback policy");
          if (mode.equals("deposit")
              && !Set.of("webview", "qr", "pending", "pending_reject")
                  .contains(
                      data.path("methods")
                          .path(a.path("method").asText())
                          .path("behavior")
                          .asText()))
            throw new IllegalArgumentException("Deposit callbacks require asynchronous profile");
        }

        if (e.getKey().isEmpty() || !data.path("methods").has(a.path("method").asText()))
          throw new IllegalArgumentException("Invalid funding alias");
        for (String k : List.of("countries", "currencies")) {
          if (!a.path(k).isArray() || a.path(k).isEmpty())
            throw new IllegalArgumentException("Invalid funding profile");
          for (var v : a.path(k))
            if (!v.asText().matches(k.equals("countries") ? "[A-Z]{2}" : "[A-Z]{3}"))
              throw new IllegalArgumentException("Invalid funding profile");
        }
      }
    } catch (java.io.IOException e) {
      throw new IllegalArgumentException("Cannot load reference features", e);
    }
  }

  JsonNode method(String code, String country, String currency) {
    var a = aliases.path(code);
    String key = a.path("method").asText(code);
    if (!data.path("methods").has(key))
      throw new ApiError(400, "VALIDATION_ERROR", "Unknown funding method");
    ObjectNode p = data.path("methods").path(key).deepCopy();
    if (a.isObject()) p.setAll((ObjectNode) a);
    p.put("code", code);
    p.put("method", key);
    for (var e :
        Map.of(
                "countries",
                country == null ? "" : country,
                "currencies",
                currency == null ? "" : currency)
            .entrySet())
      if (!e.getValue().isEmpty()) {
        boolean found = false;
        for (var v : p.path(e.getKey())) if (v.asText().equals(e.getValue())) found = true;
        if (!found)
          throw new ApiError(400, "VALIDATION_ERROR", "Funding country/currency mismatch");
      }
    return p;
  }

  static String locale(String value) {
    return value != null && value.trim().toLowerCase(Locale.ROOT).startsWith("pt")
        ? "pt-BR"
        : "en-US";
  }

  String text(String language, String key) {
    return data.path("translations").path(locale(language)).path(key).asText(key);
  }

  static String html(String v) {
    return v.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace("\"", "&quot;")
        .replace("'", "&#39;");
  }
}
