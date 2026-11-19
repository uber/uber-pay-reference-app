package com.uber.pay.reference;

import com.fasterxml.jackson.databind.JsonNode;
import jakarta.servlet.http.HttpServletRequest;
import java.net.URLDecoder;
import java.nio.charset.StandardCharsets;
import java.sql.*;
import java.util.*;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.*;

@RestController
final class Gateway {
  final Config config;
  final Database db;
  final Contracts contracts;
  final PaymentService payments;
  final WalletService wallets;
  final ProcessorExecutor processor;

  Gateway(
      Config c,
      Database d,
      Contracts ct,
      PaymentService p,
      WalletService w,
      ProcessorExecutor processor) {
    config = c;
    db = d;
    contracts = ct;
    payments = p;
    wallets = w;
    this.processor = processor;
  }

  @RequestMapping("/**")
  ResponseEntity<byte[]> handle(HttpServletRequest req) {
    String template = "";
    try {
      return sendLocal(req, route(req));
    } catch (ApiError e) {
      String path = req.getRequestURI();
      template = template(path);
      if (template.equals("/token") || template.equals("/authorize"))
        return sendLocal(
            req,
            Reply.json(
                400,
                Json.obj(
                    "error",
                    Set.of(
                                "invalid_request",
                                "invalid_client",
                                "invalid_grant",
                                "invalid_scope",
                                "unsupported_grant_type")
                            .contains(e.code)
                        ? e.code
                        : "invalid_request")));
      if (!template.isEmpty()) return sendLocal(req, payments.error(template, e));
      return sendLocal(req, Reply.empty(e.status));
    } catch (Exception e) {
      return sendLocal(req, Reply.empty(500));
    }
  }

  ResponseEntity<byte[]> sendLocal(HttpServletRequest req, Reply r) {
    return send(payments.localize(r, req.getHeader("Accept-Language")));
  }

  ResponseEntity<byte[]> send(Reply r) {
    var b = ResponseEntity.status(r.status());
    r.headers().forEach(b::header);
    if (!r.headers().containsKey("Cache-Control")) b.header("Cache-Control", "no-store");
    return b.body(r.body().getBytes(StandardCharsets.UTF_8));
  }

  static Map<String, String> form(String text) {
    Map<String, String> out = new LinkedHashMap<>();
    if (text == null || text.isEmpty()) return out;
    for (String pair : text.split("&")) {
      String[] p = pair.split("=", 2);
      String k, v;
      try {
        k = URLDecoder.decode(p[0], StandardCharsets.UTF_8);
        v = URLDecoder.decode(p.length > 1 ? p[1] : "", StandardCharsets.UTF_8);
      } catch (IllegalArgumentException e) {
        throw new ApiError(400, "invalid_request", "Malformed percent encoding");
      }
      if (out.putIfAbsent(k, v) != null)
        throw new ApiError(400, "invalid_request", "Duplicate parameter");
    }
    return out;
  }

  String template(String raw) {
    if (!raw.startsWith(config.base + "/")) return "";
    String p = raw.substring(config.base.length());
    if (p.matches("/charge/[^/]+/capture")) return "/charge/{id}/capture";
    if (p.matches("/charge/[^/]+/cancel")) return "/charge/{id}/cancel";
    return Set.of(
                "/charge",
                "/refund",
                "/payout",
                "/balance",
                "/find-transaction",
                "/authorize",
                "/token")
            .contains(p)
        ? p
        : "";
  }

  Reply route(HttpServletRequest req) throws Exception {
    String raw = req.getRequestURI(), method = req.getMethod();
    byte[] bytes = req.getInputStream().readNBytes(1048577);
    if (bytes.length > 1048576) throw new ApiError(400, "invalid_request", "Request too large");
    if (raw.equals("/health") && method.equals("GET")) {
      try (Connection c = db.open()) {
        Database.query(c, "SELECT 1");
      }
      return Reply.json(200, Json.obj("status", "ok", "language", "java"));
    }
    Map<String, String> query = form(req.getQueryString());
    if (raw.equals("/adapter/events")) {
      if (!config.processorMode.equals("http")
          || !method.equals("POST")
          || !WalletService.equal(
              Objects.toString(req.getHeader("Authorization"), ""),
              "Bearer " + config.processorToken)) return Reply.empty(404);
      processor.event(Json.parse(new String(bytes, StandardCharsets.UTF_8)));
      return Reply.empty(204);
    }
    if (raw.startsWith("/_test/")) return control(req, raw, method, bytes);
    if (raw.equals("/demo/consent") && method.equals("POST"))
      return wallets.consent(form(new String(bytes, StandardCharsets.UTF_8)));
    if (raw.equals("/demo/action")) return action(req, query, bytes);
    String t = template(raw);
    if (t.isEmpty()) throw new ApiError(404, "not_found", "Unknown route");
    String expected = Set.of("/authorize", "/find-transaction").contains(t) ? "GET" : "POST";
    if (!method.equals(expected)) throw new ApiError(404, "not_found", "Unknown route");
    if (t.equals("/authorize")) return wallets.authorize(query);
    if (method.equals("GET") && bytes.length != 0)
      throw new ApiError(400, "invalid_request", "GET body must be empty");
    Map<String, String> headers = new HashMap<>();
    for (String h :
        List.of(
            "signature",
            "digest",
            "date",
            "host",
            "content-type",
            "accept-language",
            "x-idempotency-key",
            "authorization")) {
      List<String> values = Collections.list(req.getHeaders(h));
      if (values.size() > 1) throw new ApiError(400, "invalid_request", "Duplicate header");
      if (values.size() == 1) headers.put(h, values.get(0));
    }
    String target = raw + (req.getQueryString() == null ? "" : "?" + req.getQueryString());
    config.signatures.verify(method, target, req.getHeader("Host"), headers, bytes);
    JsonNode op = contracts.op(t, expected.toLowerCase(Locale.ROOT));
    for (JsonNode p : op.path("parameters")) {
      JsonNode param = contracts.resolve(p);
      if (param.path("in").asText().equals("header") && param.path("required").asBoolean()) {
        String name = param.path("name").asText().toLowerCase(Locale.ROOT);
        if (!headers.containsKey(name) || headers.get(name).isBlank())
          throw new ApiError(400, "invalid_request", "Required header missing");
      }
    }
    if (!headers
        .getOrDefault("content-type", "")
        .split(";", 2)[0]
        .trim()
        .equalsIgnoreCase("application/json"))
      throw new ApiError(400, "invalid_request", "Content-Type must be application/json");
    JsonNode body = bytes.length == 0 ? Json.obj() : Json.parse(bytes);
    contracts.request(t, expected.toLowerCase(Locale.ROOT), body);
    if (t.equals("/token")) return wallets.token(body);
    String scope = t.equals("/balance") ? "get_balance" : t.equals("/refund") ? "refund" : "charge";
    String account =
        t.equals("/balance")
            ? Json.text(body, "account_id")
            : body.path("authorization").path("type").asText().equals("access_token_authorization")
                ? body.path("authorization").path("account_id").asText()
                : headers.containsKey("authorization") || config.oauthRequired
                    ? payments.accountForProtectedOperation(
                        t, raw.substring(config.base.length()), body)
                    : null;
    if (t.equals("/balance")
        || account != null
        || config.oauthRequired
            && Set.of("/charge", "/charge/{id}/capture", "/charge/{id}/cancel", "/refund")
                .contains(t)
        || headers.containsKey("authorization"))
      wallets.bearer(headers.get("authorization"), scope, account);
    if (config.processorMode.equals("http")
        && ProcessorExecutor.money(op.path("operationId").asText()))
      return processor.execute(
          op.path("operationId").asText(),
          t,
          raw.substring(config.base.length()),
          headers.get("x-idempotency-key"),
          body,
          ReferenceFeatures.locale(req.getHeader("Accept-Language")));
    return payments.execute(
        op.path("operationId").asText(),
        t,
        raw.substring(config.base.length()),
        headers.get("x-idempotency-key"),
        body,
        query,
        ReferenceFeatures.locale(req.getHeader("Accept-Language")));
  }

  Reply action(HttpServletRequest req, Map<String, String> q, byte[] raw) {
    boolean post = req.getMethod().equals("POST");
    if (!post && !req.getMethod().equals("GET"))
      throw new ApiError(404, "not_found", "Unknown route");
    Map<String, String> f = post ? form(new String(raw, StandardCharsets.UTF_8)) : q;
    String token = f.getOrDefault(post ? "action_token" : "token", "");
    if (token.isEmpty()) throw new ApiError(400, "invalid_request", "Action capability required");
    return db.atomic(
        c -> {
          var job =
              Database.one(
                  c,
                  "SELECT * FROM jobs WHERE client_id=? AND job_type='COMPLETION' AND"
                      + " status='WAITING' AND payload::jsonb->>'payment_token'=?",
                  config.client,
                  token);
          if (job == null) throw new ApiError(400, "invalid_request", "Invalid action");
          var payload =
              (com.fasterxml.jackson.databind.node.ObjectNode)
                  Json.parse(Database.str(job, "payload"));
          if (payload.path("expires_at").asLong() <= System.currentTimeMillis())
            throw new ApiError(400, "invalid_request", "Expired action");
          if (!post) {
            String language = payload.path("locale").asText("en-US");
            var words = payments.features;
            String amount =
                payload.path("profile").path("allow_amount_update").asBoolean()
                    ? "<label>"
                        + words.text(language, "amount")
                        + "<input name='amount_e5' inputmode='numeric'></label>"
                    : "";
            return Ui.html(
                "<!doctype html><html lang='"
                    + language
                    + "'><body><h1>"
                    + words.text(language, "payment_title")
                    + "</h1><p>"
                    + words.text(language, "synthetic")
                    + "</p><form method='post' action='/demo/action'><input type='hidden'"
                    + " name='action_token' value='"
                    + token
                    + "'><input type='hidden' name='csrf' value='"
                    + payload.path("csrf").asText()
                    + "'>"
                    + amount
                    + "<button name='decision' value='approve'>"
                    + words.text(language, "confirm")
                    + "</button><button name='decision' value='deny'>"
                    + words.text(language, "reject")
                    + "</button></form></body></html>",
                language);
          }
          if (!WalletService.equal(payload.path("csrf").asText(), f.get("csrf"))
              || !Set.of("approve", "deny").contains(f.getOrDefault("decision", "")))
            throw new ApiError(400, "invalid_request", "Invalid action");
          payload.put("state", f.get("decision").equals("approve") ? "CONFIRMED" : "REJECTED");
          payload.put("detail", "USER_CANCELLED");
          String amount = f.getOrDefault("amount_e5", "");
          if (!amount.isEmpty()) {
            if (!payload.path("profile").path("allow_amount_update").asBoolean()
                || !amount.matches("[1-9][0-9]*"))
              throw new ApiError(400, "invalid_request", "Invalid local amount");
            long value;
            try {
              value = Long.parseLong(amount);
            } catch (NumberFormatException e) {
              throw new ApiError(400, "invalid_request", "Invalid local amount");
            }
            var tx =
                Database.one(
                    c,
                    "SELECT amount_e5 FROM transactions WHERE client_id=? AND kind='charge' AND"
                        + " id=?",
                    config.client,
                    job.get("tx_id"));
            if (value > Database.number(tx, "amount_e5"))
              throw new ApiError(400, "invalid_request", "Amount exceeds original");
            payload.put("amount_e5", value);
          }

          int n =
              Database.execute(
                  c,
                  "UPDATE jobs SET payload=?,status='PENDING',next_attempt_at=now() WHERE id=? AND"
                      + " status='WAITING'",
                  Json.string(payload),
                  job.get("id"));
          if (n != 1) throw new ApiError(400, "invalid_request", "Action already consumed");
          String target = payload.path("return_url").asText();
          if (config.redirects.contains(target))
            return new Reply(302, "", Map.of("Location", target, "Cache-Control", "no-store"));
          return Reply.html(
              "<!doctype html><html><body>Local payment completion scheduled</body></html>");
        });
  }

  Reply control(HttpServletRequest req, String path, String method, byte[] raw) {
    if (!config.controls
        || !WalletService.equal(config.controlSecret, req.getHeader("X-Test-Control-Secret")))
      throw new ApiError(404, "not_found", "Unknown route");
    if (path.equals("/_test/state") && method.equals("GET"))
      return db.atomic(
          c ->
              Reply.json(
                  200,
                  Json.obj(
                      "accounts",
                      Database.query(c, "SELECT * FROM accounts WHERE client_id=?", config.client),
                      "transactions",
                      Database.query(
                          c, "SELECT * FROM transactions WHERE client_id=?", config.client),
                      "jobs",
                      Database.query(c, "SELECT * FROM jobs WHERE client_id=?", config.client))));
    JsonNode b = Json.parse(raw);
    if (!method.equals("POST")) throw new ApiError(404, "not_found", "Unknown route");
    return db.atomic(
        c -> {
          if (path.equals("/_test/complete")) {
            String kind = Json.text(b, "kind"),
                id = Json.text(b, "id"),
                state = Json.text(b, "state");
            if (!Set.of("CONFIRMED", "REJECTED").contains(state)
                || !Set.of("charge", "refund", "payout").contains(kind))
              throw new ApiError(400, "invalid_request", "Invalid completion");
            int n =
                Database.execute(
                    c,
                    "UPDATE jobs SET"
                        + " payload=?,status='PENDING',next_attempt_at=now(),lease_until=NULL WHERE"
                        + " client_id=? AND tx_kind=? AND tx_id=? AND job_type='COMPLETION' AND"
                        + " status IN ('PENDING','WAITING')",
                    Json.string(Json.obj("state", state)),
                    config.client,
                    kind,
                    id);
            if (n == 0) throw new ApiError(400, "invalid_request", "Completion is not pending");
          } else if (path.equals("/_test/reverse-refund")) {
            payments.reverseRefund(c, Json.text(b, "id"));
          } else if (path.equals("/_test/expire")) {
            String id = Json.text(b, "id");
            Database.execute(
                c,
                "UPDATE transactions SET expires_at=now()-interval '1 second' WHERE client_id=? AND"
                    + " kind='charge' AND id=? AND state='AUTHORIZED'",
                config.client,
                id);
            var t =
                Database.one(
                    c,
                    "SELECT * FROM transactions WHERE client_id=? AND kind='charge' AND id=?",
                    config.client,
                    id);
            if (t == null) throw new ApiError(400, "invalid_request", "Unknown authorization");
            payments.expire(c, t);
          } else if (path.equals("/_test/callback")) {
            String name = Json.text(b, "name"), id = Json.text(b, "id");
            contracts.callback(name, b.path("payload"));
            payments.queue(c, "test", id, "CALLBACK", name, b.path("payload"), "PENDING", 0);
          } else throw new ApiError(404, "not_found", "Unknown control");
          return Reply.json(200, Json.obj("ok", true));
        });
  }
}
