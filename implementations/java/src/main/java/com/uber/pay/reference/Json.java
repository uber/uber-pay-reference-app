package com.uber.pay.reference;

import com.fasterxml.jackson.core.JsonParser;
import com.fasterxml.jackson.databind.*;
import com.fasterxml.jackson.databind.node.*;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.util.*;

final class Json {
  static final ObjectMapper MAPPER =
      new ObjectMapper()
          .enable(DeserializationFeature.USE_BIG_INTEGER_FOR_INTS)
          .enable(JsonParser.Feature.STRICT_DUPLICATE_DETECTION);

  private Json() {}

  static JsonNode parse(byte[] b) {
    try {
      return MAPPER.readTree(b);
    } catch (Exception e) {
      throw new ApiError(400, "invalid_request", "Invalid JSON");
    }
  }

  static JsonNode parse(String s) {
    return parse(s.getBytes(StandardCharsets.UTF_8));
  }

  static String string(Object x) {
    try {
      return MAPPER.writeValueAsString(x);
    } catch (Exception e) {
      throw new IllegalStateException(e);
    }
  }

  static ObjectNode obj(Object... pairs) {
    ObjectNode n = MAPPER.createObjectNode();
    for (int i = 0; i < pairs.length; i += 2)
      n.set((String) pairs[i], MAPPER.valueToTree(pairs[i + 1]));
    return n;
  }

  static String text(JsonNode x, String k) {
    return x.path(k).asText("");
  }

  static JsonNode canonical(JsonNode x) {
    if (x.isObject()) {
      ObjectNode o = MAPPER.createObjectNode();
      TreeSet<String> keys = new TreeSet<>();
      x.fieldNames().forEachRemaining(keys::add);
      keys.forEach(k -> o.set(k, canonical(x.get(k))));
      return o;
    }
    if (x.isArray()) {
      ArrayNode a = MAPPER.createArrayNode();
      x.forEach(v -> a.add(canonical(v)));
      return a;
    }
    return x;
  }

  static String hash(String s) {
    try {
      return HexFormat.of()
          .formatHex(
              MessageDigest.getInstance("SHA-256").digest(s.getBytes(StandardCharsets.UTF_8)));
    } catch (Exception e) {
      throw new IllegalStateException(e);
    }
  }
}
