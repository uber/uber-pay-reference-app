package com.uber.pay.reference;

import com.fasterxml.jackson.databind.JsonNode;
import java.net.URI;
import java.nio.file.*;
import java.time.OffsetDateTime;
import java.util.*;
import java.util.regex.Pattern;

final class Contracts {
  final JsonNode spec;

  Contracts(Path p) {
    try {
      spec = Json.MAPPER.readTree(Files.readAllBytes(p));
    } catch (Exception e) {
      throw new IllegalArgumentException("Cannot load contract", e);
    }
  }

  JsonNode resolve(JsonNode s) {
    if (s.has("$ref")) {
      String r = s.get("$ref").asText();
      if (!r.startsWith("#/")) throw new IllegalArgumentException("External ref refused");
      return spec.at(r.substring(1));
    }
    return s;
  }

  void validate(JsonNode schema, JsonNode value) {
    String error = error(schema, value, "$");
    if (error != null) throw new ApiError(400, "invalid_request", error);
  }

  String error(JsonNode schema, JsonNode value, String path) {
    JsonNode s = resolve(schema);
    if (s.isMissingNode() || s.isNull()) return null;
    for (String keyword : List.of("allOf", "anyOf", "oneOf"))
      if (s.has(keyword)) {
        int ok = 0;
        for (JsonNode child : s.get(keyword)) if (error(child, value, path) == null) ok++;
        if (keyword.equals("allOf")
            ? ok != s.get(keyword).size()
            : keyword.equals("oneOf") ? ok != 1 : ok == 0) return path + " violates " + keyword;
      }
    if (value == null || value.isNull())
      return s.path("nullable").asBoolean() ? null : path + " must not be null";
    String type = s.path("type").asText();
    boolean valid =
        switch (type) {
          case "object" -> value.isObject();
          case "array" -> value.isArray();
          case "string" -> value.isTextual();
          case "integer" -> value.isIntegralNumber() && value.canConvertToLong();
          case "number" -> value.isNumber();
          case "boolean" -> value.isBoolean();
          default -> true;
        };
    if (!valid) return path + " has wrong type";
    if (s.has("enum")) {
      boolean found = false;
      for (JsonNode e : s.get("enum")) if (e.equals(value)) found = true;
      if (!found) return path + " invalid enum";
    }
    if (value.isObject()) {
      JsonNode required = s.path("required");
      if (required.isTextual()) required = Json.MAPPER.createArrayNode().add(required.asText());
      for (JsonNode k : required)
        if (!value.has(k.asText())) return path + " missing " + k.asText();
      for (var it = value.fields(); it.hasNext(); ) {
        var e = it.next();
        JsonNode prop = s.path("properties").get(e.getKey());
        if (prop != null) {
          String err = error(prop, e.getValue(), path + "." + e.getKey());
          if (err != null) return err;
        } else if (s.has("additionalProperties")) {
          JsonNode ap = s.get("additionalProperties");
          if (ap.isBoolean() && !ap.asBoolean()) return path + " unknown field";
          if (ap.isObject()) {
            String err = error(ap, e.getValue(), path + "." + e.getKey());
            if (err != null) return err;
          }
        }
      }
    }
    if (value.isArray()) {
      for (int i = 0; i < value.size(); i++) {
        String err = error(s.path("items"), value.get(i), path + "[" + i + "]");
        if (err != null) return err;
      }
    }
    if (value.isTextual()) {
      String v = value.asText();
      int length = v.codePointCount(0, v.length());
      if (s.has("minLength") && length < s.get("minLength").asInt()
          || s.has("maxLength") && length > s.get("maxLength").asInt())
        return path + " invalid length";
      if (s.has("pattern") && !Pattern.compile(s.get("pattern").asText()).matcher(v).find())
        return path + " invalid pattern";
      try {
        if (s.path("format").asText().equals("date-time")) OffsetDateTime.parse(v);
        if (s.path("format").asText().equals("uri") && !URI.create(v).isAbsolute())
          return path + " invalid URI";
      } catch (Exception e) {
        return path + " invalid format";
      }
    }
    return null;
  }

  JsonNode op(String path, String method) {
    return spec.path("paths").path(path).path(method);
  }

  void request(String path, String method, JsonNode value) {
    JsonNode s =
        op(path, method)
            .path("requestBody")
            .path("content")
            .path("application/json")
            .path("schema");
    if (!s.isMissingNode()) validate(s, value);
  }

  void response(String path, int status, JsonNode value, String grant) {
    JsonNode s =
        op(path, "post")
            .path("responses")
            .path("" + status)
            .path("content")
            .path("application/json")
            .path("schema");
    if (path.equals("/find-transaction"))
      s =
          op(path, "get")
              .path("responses")
              .path("" + status)
              .path("content")
              .path("application/json")
              .path("schema");
    if (path.equals("/token") && status == 200) {
      String name =
          grant.equals("authorization_code")
              ? "RefreshTokenResponse"
              : grant.equals("refresh_token")
                  ? "AccessTokenResponse"
                  : "WalletGrantRefreshTokenResponse";
      s = spec.path("components").path("schemas").path(name);
    }
    if (!s.isMissingNode() && error(s, value, "$") != null)
      throw new IllegalStateException("Internal response violates " + path + " contract");
  }

  void callback(String name, JsonNode payload) {
    JsonNode cb = spec.path("components").path("callbacks").path(name);
    if (cb.isMissingNode()) throw new ApiError(400, "invalid_request", "Unknown callback");
    JsonNode op = cb.elements().next().path("post");
    validate(
        op.path("requestBody").path("content").path("application/json").path("schema"), payload);
  }
}
