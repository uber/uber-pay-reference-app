package com.uber.pay.reference;

import java.util.Map;

record Reply(int status, String body, Map<String, String> headers) {
  static Reply replay(Map<String, Object> row) {
    Map<String, String> headers =
        Json.MAPPER.convertValue(
            Json.parse(Database.str(row, "response_headers")),
            new com.fasterxml.jackson.core.type.TypeReference<Map<String, String>>() {});
    return new Reply(
        ((Number) row.get("status")).intValue(), Database.str(row, "response_body"), headers);
  }

  static Reply json(int status, Object value) {
    return new Reply(status, Json.string(value), Map.of("Content-Type", "application/json"));
  }

  static Reply empty(int status) {
    return new Reply(status, "", Map.of());
  }

  static Reply html(String html) {
    return new Reply(
        200,
        html,
        Map.of(
            "Content-Type",
            "text/html; charset=utf-8",
            "Cache-Control",
            "no-store",
            "Referrer-Policy",
            "no-referrer",
            "X-Content-Type-Options",
            "nosniff",
            "Content-Security-Policy",
            "default-src 'none'; form-action 'self'; style-src 'unsafe-inline'; frame-ancestors"
                + " 'none'"));
  }
}
