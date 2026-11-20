package com.uber.pay.reference;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.node.ObjectNode;
import java.net.URI;
import java.net.http.*;
import java.nio.charset.StandardCharsets;
import java.security.SecureRandom;
import java.sql.*;
import java.time.*;
import java.util.*;
import java.util.concurrent.*;
import javax.crypto.Cipher;
import javax.crypto.spec.*;

/** Commits intents before HTTP, then finalizes under a fenced short transaction. */
final class ProcessorExecutor implements AutoCloseable {
  final PaymentService service;
  final Config config;
  final Database db;
  final HttpClient http =
      HttpClient.newBuilder()
          .connectTimeout(Duration.ofSeconds(2))
          .followRedirects(HttpClient.Redirect.NEVER)
          .build();
  final ScheduledExecutorService worker =
      Executors.newSingleThreadScheduledExecutor(
          r -> {
            Thread t = new Thread(r, "processor-reconciliation");
            t.setDaemon(true);
            return t;
          });

  ProcessorExecutor(PaymentService p) {
    service = p;
    config = p.config;
    db = p.db;
  }

  static boolean money(String op) {
    return Set.of("charge", "capture", "cancel", "initRefund", "payout").contains(op);
  }

  static class Pure implements PspAdapter {
    final PspAdapter demo = new DemoPspAdapter();
    final JsonNode result;
    final boolean validation;

    Pure(JsonNode r) {
      result = r;
      validation = r == null;
    }

    public boolean isLocalOnly() {
      return true;
    }

    Decision frozen() {
      return new Decision(
          result.path("state").asText(),
          result.path("detail").isTextual() ? result.path("detail").asText() : null,
          false);
    }

    public Decision charge(ChargeCommand c, String k) {
      if (!validation) return frozen();
      demo.charge(c, k);
      if (c.amountE5() > c.availableE5())
        return new Decision("REJECTED", "INSUFFICIENT_BALANCE", false);
      return new Decision("CONFIRMED", null, false);
    }

    public Decision payout(PayoutCommand c, String k) {
      if (validation) {
        PaymentMetadata.validate(c.request(), c.amountE5());
        DemoPspAdapter.method(c.fundingMethod());
        if (c.amountE5() > c.availableE5())
          throw new ApiError(400, "INSUFFICIENT_BALANCE", "Insufficient funds");
        return new Decision("CONFIRMED", null, false);
      }
      Decision d = frozen();
      if (d.state().equals("REJECTED"))
        throw new ApiError(
            Set.of("VALIDATION_ERROR", "INVALID_PAYMENT_DETAILS_ERROR", "INSUFFICIENT_BALANCE")
                    .contains(d.detail())
                ? 400
                : 422,
            d.detail(),
            "Processor payout decline");
      return d;
    }

    public Decision capture(CaptureCommand c, String k) {
      if (validation) {
        demo.capture(c, k);
        return new Decision("CONFIRMED", null, false);
      }
      return frozen();
    }

    public Decision cancel(CancelCommand c, String k) {
      if (validation) return demo.cancel(c, k);
      return frozen();
    }

    public Decision refund(RefundCommand c, String k) {
      if (validation) {
        demo.refund(c, k);
        return new Decision("CONFIRMED", null, false);
      }
      return frozen();
    }

    public BalanceResult getBalance(BalanceCommand c) {
      return demo.getBalance(c);
    }

    public Snapshot lookupOperation(LookupCommand c) {
      return demo.lookupOperation(c);
    }
  }

  String configHash() {
    return Json.hash(
        Json.string(
            Json.canonical(
                Json.obj("data", service.features.data, "aliases", service.features.aliases))));
  }

  String encrypt(String id, JsonNode value) throws Exception {
    byte[] nonce = new byte[12];
    new SecureRandom().nextBytes(nonce);
    Cipher cipher = Cipher.getInstance("AES/GCM/NoPadding");
    cipher.init(
        Cipher.ENCRYPT_MODE,
        new SecretKeySpec(Base64.getDecoder().decode(config.processorKey), "AES"),
        new GCMParameterSpec(128, nonce));
    cipher.updateAAD((config.client + "|" + id).getBytes(StandardCharsets.UTF_8));
    byte[] crypt = cipher.doFinal(Json.string(value).getBytes(StandardCharsets.UTF_8));
    byte[] out = new byte[nonce.length + crypt.length];
    System.arraycopy(nonce, 0, out, 0, 12);
    System.arraycopy(crypt, 0, out, 12, crypt.length);
    return Base64.getEncoder().encodeToString(out);
  }

  JsonNode context(Map<String, Object> row) throws Exception {
    byte[] raw = Base64.getDecoder().decode(Database.str(row, "request_encrypted"));
    if (raw.length < 28) throw new IllegalStateException("Invalid encrypted command");
    Cipher c = Cipher.getInstance("AES/GCM/NoPadding");
    c.init(
        Cipher.DECRYPT_MODE,
        new SecretKeySpec(Base64.getDecoder().decode(config.processorKey), "AES"),
        new GCMParameterSpec(128, raw, 0, 12));
    c.updateAAD((config.client + "|" + Database.str(row, "id")).getBytes(StandardCharsets.UTF_8));
    JsonNode value =
        Json.parse(new String(c.doFinal(raw, 12, raw.length - 12), StandardCharsets.UTF_8));
    if (!value.path("config_hash").asText().equals(configHash()))
      throw new IllegalStateException("Restore pinned funding configuration before reconciliation");
    return value;
  }

  JsonNode remote(String key, JsonNode body) throws Exception {
    var b =
        HttpRequest.newBuilder(URI.create(config.processorUrl + "/operations/" + key))
            .timeout(Duration.ofMillis(config.processorTimeout))
            .header("Authorization", "Bearer " + config.processorToken)
            .header("Content-Type", "application/json")
            .header("X-Reference-Client", config.client);
    if (body == null) b.GET();
    else b.POST(HttpRequest.BodyPublishers.ofString(Json.string(body)));
    var res = http.send(b.build(), HttpResponse.BodyHandlers.ofInputStream());
    try (var stream = res.body()) {
      if (body == null && res.statusCode() == 404) return null;
      if (res.statusCode() != 200) throw new IllegalStateException("Processor outcome unresolved");
      byte[] raw = stream.readNBytes(1048577);
      if (raw.length > 1048576) throw new IllegalStateException("Processor response exceeds limit");
      return Json.parse(new String(raw, StandardCharsets.UTF_8));
    }
  }

  JsonNode decision(String op, JsonNode value) {
    String state = value.path("state").asText(), ref = value.path("psp_reference").asText();
    Set<String> allowed =
        op.equals("cancel")
            ? Set.of("CANCELLED")
            : op.equals("capture")
                ? Set.of("CONFIRMED", "REJECTED")
                : Set.of("CONFIRMED", "PENDING", "REJECTED");
    if (!value.isObject() || !allowed.contains(state) || ref.isEmpty() || ref.length() > 256)
      throw new ApiError(400, "invalid_request", "Invalid processor decision");
    if (value.has("terminal") && !value.path("terminal").isBoolean())
      throw new ApiError(400, "invalid_request", "Provider terminal must be boolean");
    JsonNode version = value.path("version");
    if (!version.isMissingNode()
        && (!version.isIntegralNumber() || !version.canConvertToInt() || version.asInt() < 1))
      throw new ApiError(400, "invalid_request", "Invalid provider version");
    ((ObjectNode) value).put("version", version.asInt(1));
    if (state.equals("REJECTED") && !op.equals("initRefund")) {
      if (op.equals("capture")) {
        if (!Set.of("BANK_HARD_ERROR", "BANK_SOFT_ERROR").contains(value.path("detail").asText()))
          throw new ApiError(400, "invalid_request", "Invalid capture decline");
      } else
        service.contracts.validate(
            service
                .contracts
                .spec
                .path("components")
                .path("schemas")
                .path(op.equals("payout") ? "PayoutCallbackStateDetail" : "StateReasonCode"),
            value.path("detail"));
    }
    if (value.has("actions")) {
      if (!op.equals("charge") || !state.equals("PENDING"))
        throw new ApiError(400, "invalid_request", "Actions require pending charge");
      service.contracts.validate(
          service.contracts.spec.path("components").path("schemas").path("Actions"),
          value.path("actions"));
      for (var action : value.path("actions"))
        if (action.has("url")) {
          URI u = URI.create(action.path("url").asText());
          if (!Set.of("http", "https").contains(u.getScheme())
              || u.getUserInfo() != null
              || u.getHost() == null)
            throw new ApiError(400, "invalid_request", "Invalid processor action URL");
        }
    }
    if (value.has("amount"))
      service.contracts.validate(
          service.contracts.spec.path("components").path("schemas").path("CurrencyAmount"),
          value.path("amount"));
    return value;
  }

  Reply replay(Map<String, Object> row) {
    return new Reply(
        ((Number) row.get("response_status")).intValue(),
        Database.str(row, "response_body"),
        Json.MAPPER.convertValue(
            Json.parse(Database.str(row, "response_headers")),
            new com.fasterxml.jackson.core.type.TypeReference<Map<String, String>>() {}));
  }

  Reply execute(
      String op, String template, String resource, String idem, JsonNode body, String language) {
    if (idem == null || idem.isBlank() || idem.length() > 256)
      return service.error(
          template, new ApiError(400, "VALIDATION_ERROR", "Invalid idempotency key"));
    String logical =
        Set.of("charge", "payout", "initRefund").contains(op) ? Json.text(body, "id") : idem;
    String key = Json.hash(config.client + "|" + op + "|" + logical),
        fp = Json.hash(resource + "\n" + Json.string(Json.canonical(body)));
    boolean[] fresh = {false};
    Map<String, Object> row =
        db.raw(
            c -> {
              Database.execute(c, "SELECT pg_advisory_xact_lock(hashtextextended(?,0))", key);
              var mapping =
                  Database.one(
                      c,
                      "SELECT * FROM provider_keys WHERE client_id=? AND operation_name=? AND"
                          + " idem_key=?",
                      config.client,
                      op,
                      idem);
              if (mapping != null && !Database.str(mapping, "fingerprint").equals(fp))
                throw conflict(op);
              String intent = mapping == null ? key : Database.str(mapping, "intent_id");
              var current = Database.one(c, "SELECT * FROM provider_intents WHERE id=?", intent);
              if (current != null && !Database.str(current, "fingerprint").equals(fp))
                throw conflict(op);
              if (current == null) {
                Savepoint sp = c.setSavepoint();
                var validation = new PaymentService(config, db, service.contracts, new Pure(null));
                Reply result =
                    validation.executeTx(c, op, template, resource, idem, body, Map.of(), language);
                c.rollback(sp);
                boolean local =
                    result.status() >= 400
                        || !result.body().isEmpty()
                            && Json.parse(result.body())
                                .path("state")
                                .path("value")
                                .asText()
                                .equals("REJECTED");
                if (local)
                  validation.executeTx(c, op, template, resource, idem, body, Map.of(), language);
                JsonNode ctx =
                    Json.obj(
                        "operation",
                        op,
                        "template",
                        template,
                        "resource",
                        resource,
                        "idem",
                        idem,
                        "body",
                        body,
                        "language",
                        language,
                        "config_hash",
                        configHash());
                String claim = PaymentService.resourceKey(op, resource, body);
                current =
                    Database.one(
                        c,
                        "INSERT INTO"
                            + " provider_intents(id,client_id,operation_name,provider_key,resource_key,fingerprint,request_encrypted,status,attempts,lease_until,response_status,response_body,response_headers)"
                            + " VALUES(?,?,?,?,?,?,?,?,1,now()+(?*interval '1"
                            + " millisecond'),?,?,?::jsonb) RETURNING *",
                        intent,
                        config.client,
                        op,
                        intent,
                        claim,
                        fp,
                        encrypt(intent, ctx),
                        local ? "DONE" : "IN_FLIGHT",
                        config.processorLease,
                        local ? result.status() : null,
                        local ? result.body() : null,
                        Json.string(result.headers()));
                if (!local) {
                  ObjectNode plan =
                      Json.obj(
                          "operation",
                          op,
                          "amount",
                          body.path("amount").path("value"),
                          "currency",
                          body.path("amount").path("currency"),
                          "account",
                          op.equals("payout")
                              ? body.path("payer").path("id").asText()
                              : body.path("authorization")
                                  .path("account_id")
                                  .asText("demo-account"),
                          "payee",
                          body.path("payee").path("account").path("account_id"));
                  if (Set.of("capture", "cancel", "initRefund").contains(op)) {
                    var original =
                        Database.one(
                            c,
                            "SELECT * FROM transactions WHERE client_id=? AND kind='charge' AND"
                                + " id=?",
                            config.client,
                            claim.substring(7));
                    plan.put("account", Database.str(original, "account_id"));
                    plan.put("original_id", Database.str(original, "id"));
                    plan.put("currency", Database.str(original, "currency"));
                    if (op.equals("capture"))
                      plan.set("amount", body.path("amount_to_capture").path("value"));
                    if (op.equals("cancel"))
                      plan.put("amount", Database.number(original, "amount_e5"));
                  }
                  Database.execute(
                      c,
                      "SELECT reference_reserve_intent(?,?,?,?::jsonb)",
                      intent,
                      config.client,
                      claim,
                      Json.string(plan));
                  fresh[0] = true;
                }
              }
              Database.execute(
                  c,
                  "INSERT INTO"
                      + " provider_keys(client_id,operation_name,idem_key,fingerprint,intent_id)"
                      + " VALUES(?,?,?,?,?) ON CONFLICT DO NOTHING",
                  config.client,
                  op,
                  idem,
                  fp,
                  intent);
              return current;
            });
    if (row.get("response_status") != null) {
      if (Database.str(row, "status").equals("RECONCILE"))
        db.raw(
            c -> {
              Database.execute(
                  c,
                  "UPDATE provider_intents SET status='PENDING',failures=0,next_attempt_at=now()"
                      + " WHERE id=? AND status='RECONCILE'",
                  row.get("id"));
              return null;
            });
      return replay(row);
    }
    if (fresh[0])
      return config.processorPauseAfterPrepare ? Reply.empty(500) : dispatch(row, false);
    long end = System.nanoTime() + TimeUnit.MILLISECONDS.toNanos(config.processorWait);
    do {
      var current =
          db.raw(c -> Database.one(c, "SELECT * FROM provider_intents WHERE id=?", row.get("id")));
      if (current.get("response_status") != null) return replay(current);
      var claimed = claim(Database.str(row, "id"), true);
      if (claimed != null) return dispatch(claimed, true);
      try {
        Thread.sleep(25);
      } catch (InterruptedException e) {
        Thread.currentThread().interrupt();
        break;
      }
    } while (System.nanoTime() < end);
    return Reply.empty(500);
  }

  ApiError conflict(String op) {
    return new ApiError(
        op.equals("payout") ? 409 : 400,
        op.equals("payout") ? "DUPLICATE_REQUEST_ERROR" : "VALIDATION_ERROR",
        "Operation identity conflict");
  }

  Map<String, Object> claim(String id, boolean explicit) {
    return db.raw(
        c ->
            Database.one(
                c,
                "UPDATE provider_intents SET"
                    + " generation=generation+1,attempts=attempts+1,lease_until=now()+(?*interval"
                    + " '1 millisecond') WHERE id=? AND client_id=? AND status<>'DONE' AND (? OR"
                    + " status<>'RECONCILE') AND (lease_until IS NULL OR lease_until<now())"
                    + " RETURNING *",
                config.processorLease,
                id,
                config.client,
                explicit));
  }

  Reply dispatch(Map<String, Object> row, boolean lookup) {
    try {
      JsonNode ctx = context(row);
      JsonNode result = lookup ? remote(Database.str(row, "provider_key"), null) : null;
      if (result == null)
        result =
            remote(
                Database.str(row, "provider_key"),
                Json.obj(
                    "operation",
                    ctx.path("operation"),
                    "request",
                    ctx.path("body"),
                    "resource",
                    Database.str(row, "resource_key").split(":", 2)[1]));
      result = decision(ctx.path("operation").asText(), result);
      if (ctx.path("body").path("capture_method").asText().equals("MANUAL")
          && result.path("state").asText().equals("PENDING"))
        throw new IllegalStateException("Manual authorization cannot be pending");
      return finalizeResult(row, result);
    } catch (Exception e) {
      db.raw(
          c -> {
            Database.execute(
                c,
                "UPDATE provider_intents SET failures=failures+1,status=CASE WHEN failures+1>=?"
                    + " THEN 'RECONCILE' ELSE 'UNCERTAIN'"
                    + " END,lease_until=NULL,next_attempt_at=now()+(?*interval '1"
                    + " millisecond'),last_error='processor_unresolved' WHERE id=? AND"
                    + " generation=?",
                config.processorMaxFailures,
                config.processorPoll,
                row.get("id"),
                row.get("generation"));
            return null;
          });
      return Reply.empty(500);
    }
  }

  Reply finalizeResult(Map<String, Object> row, JsonNode result) throws Exception {
    JsonNode ctx = context(row);
    return db.raw(
        c -> {
          var current =
              Database.one(
                  c, "SELECT * FROM provider_intents WHERE id=? FOR UPDATE", row.get("id"));
          if (Database.number(current, "generation") != Database.number(row, "generation"))
            return current.get("response_status") == null ? Reply.empty(500) : replay(current);
          String ref = result.path("psp_reference").asText();
          if (!Database.str(current, "provider_reference").isEmpty()
              && !Database.str(current, "provider_reference").equals(ref))
            throw new IllegalStateException("Provider reference changed");
          int version = result.path("version").asInt();
          if (current.get("response_status") != null
              && version <= Database.number(current, "last_provider_version")) {
            Database.execute(
                c, "UPDATE provider_intents SET lease_until=NULL WHERE id=?", row.get("id"));
            return replay(current);
          }
          Database.execute(c, "SELECT set_config('reference.intent_id',?,true)", row.get("id"));
          String op = ctx.path("operation").asText(),
              kind = op.equals("payout") ? "payout" : op.equals("initRefund") ? "refund" : "charge";
          JsonNode body = ctx.path("body");
          String id = body.path("id").asText(Database.str(row, "resource_key").substring(7));
          Reply reply;
          if (current.get("response_status") != null) {
            reply = replay(current);
            var tx =
                Database.one(
                    c,
                    "SELECT * FROM transactions WHERE client_id=? AND kind=? AND id=? FOR UPDATE",
                    config.client,
                    kind,
                    id);
            String state = Database.str(tx, "state"), next = result.path("state").asText();
            JsonNode updated = null;
            if (result.has("amount")) {
              var profile = Json.parse(Database.str(tx, "reference_profile"));
              if (!kind.equals("charge")
                  || !state.equals("PENDING")
                  || !next.equals("CONFIRMED")
                  || !profile.path("callback_mode").asText().equals("deposit")
                  || !result
                      .path("amount")
                      .path("currency")
                      .asText()
                      .equals(Database.str(tx, "currency")))
                throw new IllegalStateException(
                    "Amount adjustment requires pending deposit confirmation");
              updated = result.path("amount").path("value");
            }
            if (state.equals("PENDING") && !next.equals("PENDING"))
              service.complete(
                  c, kind, id, next, result.path("detail").asText("MERCHANT_REJECTED"), updated);
            else if (kind.equals("refund") && state.equals("CONFIRMED") && next.equals("REJECTED"))
              service.reverseRefund(c, id);
            else if (!state.equals(next)
                && !(state.equals("AUTHORIZED") && next.equals("CONFIRMED")))
              throw new IllegalStateException("Invalid provider transition");
          } else {
            if (result.has("amount"))
              throw new IllegalStateException("Amount update requires existing pending deposit");
            var nativeService = new PaymentService(config, db, service.contracts, new Pure(result));
            reply =
                nativeService.executeTx(
                    c,
                    op,
                    ctx.path("template").asText(),
                    ctx.path("resource").asText(),
                    ctx.path("idem").asText(),
                    body,
                    Map.of(),
                    ctx.path("language").asText());
            int expected =
                op.equals("initRefund")
                    ? 201
                    : result.path("state").asText().equals("REJECTED") && op.equals("capture")
                        ? 422
                        : result.path("state").asText().equals("REJECTED") && op.equals("payout")
                            ? (Set.of(
                                        "VALIDATION_ERROR",
                                        "INVALID_PAYMENT_DETAILS_ERROR",
                                        "INSUFFICIENT_BALANCE")
                                    .contains(result.path("detail").asText())
                                ? 400
                                : 422)
                            : 200;
            if (reply.status() != expected)
              throw new IllegalStateException(
                  "Provider outcome must reconcile; native preconditions changed");
            if (reply.status() < 300
                && !reply.body().isEmpty()
                && Set.of("charge", "payout", "initRefund").contains(op)) {
              ObjectNode out = (ObjectNode) Json.parse(reply.body());
              out.put(kind.equals("refund") ? "merchant_reference" : "psp_reference", ref);
              if (result.has("actions")) out.set("actions", result.path("actions"));
              service.contracts.response(ctx.path("template").asText(), reply.status(), out, "");
              reply = new Reply(reply.status(), Json.string(out), reply.headers());
              Database.execute(
                  c,
                  "UPDATE transactions SET psp_reference=?,response_body=? WHERE client_id=? AND"
                      + " kind=? AND id=?",
                  ref,
                  reply.body(),
                  config.client,
                  kind,
                  id);
              Database.execute(
                  c,
                  "UPDATE operations SET response_body=? WHERE client_id=? AND operation_name=? AND"
                      + " idem_key=?",
                  reply.body(),
                  config.client,
                  op,
                  ctx.path("idem").asText());
            }
          }
          String next = result.path("state").asText();
          Database.execute(
              c,
              "UPDATE jobs SET status=? WHERE client_id=? AND tx_kind=? AND tx_id=? AND"
                  + " job_type='COMPLETION' AND status IN ('PENDING','WAITING','PROVIDER_WAIT')",
              next.equals("PENDING") ? "PROVIDER_WAIT" : "DONE",
              config.client,
              kind,
              id);
          boolean pending =
              next.equals("PENDING")
                  || kind.equals("refund")
                      && next.equals("CONFIRMED")
                      && !result.path("terminal").asBoolean(true);
          Database.execute(
              c,
              "UPDATE provider_intents SET"
                  + " status=?,last_provider_version=?,provider_reference=?,lease_until=NULL,next_attempt_at=now()+(?*interval"
                  + " '1 millisecond'),response_status=COALESCE(response_status,?),response_body=COALESCE(response_body,?),response_headers=CASE"
                  + " WHEN response_status IS NULL THEN ?::jsonb ELSE response_headers"
                  + " END,last_error=NULL,failures=0,updated_at=now() WHERE id=? AND generation=?",
              pending ? "PENDING" : "DONE",
              version,
              ref,
              config.processorPoll,
              reply.status(),
              reply.body(),
              Json.string(reply.headers()),
              row.get("id"),
              row.get("generation"));
          if (next.equals("PENDING"))
            Database.execute(
                c, "UPDATE intent_reservations SET debit_e5=0 WHERE intent_id=?", row.get("id"));
          else {
            Database.execute(c, "DELETE FROM intent_reservations WHERE intent_id=?", row.get("id"));
            Database.execute(c, "DELETE FROM resource_claims WHERE intent_id=?", row.get("id"));
          }
          return reply;
        });
  }

  void event(JsonNode value) throws Exception {
    var row =
        db.raw(
            c -> {
              var current =
                  Database.one(
                      c,
                      "SELECT * FROM provider_intents WHERE client_id=? AND provider_key=? FOR"
                          + " UPDATE",
                      config.client,
                      value.path("provider_key").asText());
              if (current == null) throw new ApiError(404, "not_found", "Unknown provider key");
              decision(Database.str(current, "operation_name"), value);
              if (!Database.str(current, "provider_reference").isEmpty()
                  && !Database.str(current, "provider_reference")
                      .equals(value.path("psp_reference").asText()))
                throw new ApiError(400, "invalid_request", "Provider reference changed");
              if (value.path("version").asInt()
                  <= Database.number(current, "last_provider_version")) return null;
              if (current.get("lease_until") != null
                  && Instant.parse(Database.str(current, "lease_until")).isAfter(Instant.now()))
                throw new ApiError(503, "unavailable", "Processor operation in flight");
              return Database.one(
                  c,
                  "UPDATE provider_intents SET"
                      + " generation=generation+1,lease_until=now()+(?*interval '1 millisecond')"
                      + " WHERE id=? RETURNING *",
                  config.processorLease,
                  current.get("id"));
            });
    if (row != null)
      try {
        finalizeResult(row, value);
      } catch (Exception e) {
        db.raw(
            c -> {
              Database.execute(
                  c,
                  "UPDATE provider_intents SET"
                      + " lease_until=NULL,status='RECONCILE',last_error='event_unresolved' WHERE"
                      + " id=? AND generation=?",
                  row.get("id"),
                  row.get("generation"));
              return null;
            });
        throw new IllegalStateException("Event unresolved");
      }
  }

  void start() {
    if (config.processorMode.equals("http"))
      worker.scheduleWithFixedDelay(
          this::tick, config.processorPoll, config.processorPoll, TimeUnit.MILLISECONDS);
  }

  void tick() {
    try {
      var rows =
          db.raw(
              c ->
                  Database.query(
                      c,
                      "SELECT id FROM provider_intents WHERE client_id=? AND status IN"
                          + " ('IN_FLIGHT','UNCERTAIN','PENDING') AND next_attempt_at<=now() AND"
                          + " (lease_until IS NULL OR lease_until<now()) ORDER BY next_attempt_at"
                          + " LIMIT 4",
                      config.client));
      for (var row : rows) {
        var claimed = claim(Database.str(row, "id"), false);
        if (claimed != null) dispatch(claimed, true);
      }
    } catch (Exception ignored) {
      System.err.println("processor reconciliation retained for retry");
    }
  }

  public void close() {
    worker.shutdownNow();
    try {
      worker.awaitTermination(6, TimeUnit.SECONDS);
    } catch (InterruptedException e) {
      Thread.currentThread().interrupt();
    }
  }
}
