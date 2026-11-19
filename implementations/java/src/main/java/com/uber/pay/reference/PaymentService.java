package com.uber.pay.reference;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.node.*;
import java.net.URLEncoder;
import java.nio.charset.StandardCharsets;
import java.sql.*;
import java.time.Instant;
import java.util.*;

final class PaymentService {
  final Config config;
  final Database db;
  final Contracts contracts;
  final PspAdapter adapter;

  PaymentService(Config c, Database d, Contracts ct, PspAdapter a) {
    config = c;
    db = d;
    contracts = ct;
    adapter = a;
    if (!a.isLocalOnly())
      throw new IllegalArgumentException(
          "A network PSP adapter requires an intent/reconciliation executor outside the local DB"
              + " transaction");
  }

  static String enc(String s) {
    return URLEncoder.encode(s, StandardCharsets.UTF_8).replace("+", "%20");
  }

  static JsonNode amount(long n, String currency) {
    return Json.obj("value", n, "currency", currency);
  }

  static long value(JsonNode n) {
    if (!n.path("value").isIntegralNumber()
        || !n.path("value").canConvertToLong()
        || n.path("value").asLong() <= 0)
      throw new ApiError(400, "VALIDATION_ERROR", "Amount must be a positive int64");
    return n.path("value").asLong();
  }

  static String ref(String kind, String id) {
    return "demo-" + kind + "-" + Json.hash(id).substring(0, 24);
  }

  Reply execute(
      String operation,
      String template,
      String resource,
      String idem,
      JsonNode body,
      Map<String, String> query) {
    return db.atomic(
        c -> {
          String fingerprint = Json.hash(resource + "\n" + Json.string(Json.canonical(body)));
          if (idem != null) {
            if (idem.isBlank() || idem.length() > 256)
              throw new ApiError(400, "VALIDATION_ERROR", "Invalid idempotency key");
            var old =
                Database.one(
                    c,
                    "SELECT * FROM operations WHERE client_id=? AND operation_name=? AND"
                        + " idem_key=?",
                    config.client,
                    operation,
                    idem);
            if (old != null) {
              if (!Database.str(old, "fingerprint").equals(fingerprint))
                return error(
                    template,
                    new ApiError(
                        template.equals("/payout") ? 409 : 400,
                        template.equals("/payout") ? "DUPLICATE_REQUEST_ERROR" : "VALIDATION_ERROR",
                        "Idempotency key reused with a different request"));
              return Reply.replay(old);
            }
          }
          Savepoint sp = c.setSavepoint();
          Reply r;
          try {
            r =
                switch (operation) {
                  case "charge" -> create(c, "charge", body, fingerprint);
                  case "payout" -> create(c, "payout", body, fingerprint);
                  case "capture" -> capture(c, resource, body);
                  case "cancel" -> cancel(c, resource, body);
                  case "findTransaction" -> find(c, query);
                  case "initRefund" -> refund(c, body, fingerprint);
                  case "getBalance" -> balance(c, body);
                  default -> throw new ApiError(404, "not_found", "Unknown route");
                };
            if (!r.body().isEmpty())
              contracts.response(template, r.status(), Json.parse(r.body()), "");
          } catch (ApiError e) {
            c.rollback(sp);
            r = error(template, e);
          }
          if (idem != null && r.status() < 500)
            Database.execute(
                c,
                "INSERT INTO"
                    + " operations(client_id,operation_name,idem_key,fingerprint,status,response_body,response_headers,tx_id)"
                    + " VALUES(?,?,?,?,?,?,?::jsonb,?)",
                config.client,
                operation,
                idem,
                fingerprint,
                r.status(),
                r.body(),
                Json.string(r.headers()),
                Json.text(body, "id"));
          return r;
        });
  }

  Reply error(String template, ApiError e) {
    int status = e.status;
    if (status == 401 && !Set.of("/charge", "/balance", "/payout").contains(template)) status = 400;
    JsonNode schema =
        contracts
            .op(template, template.equals("/find-transaction") ? "get" : "post")
            .path("responses")
            .path("" + status);
    if (status >= 500 && schema.isMissingNode()) return Reply.empty(500);
    if (schema.isMissingNode()) status = 400;
    if (Set.of("/charge/{id}/capture", "/charge/{id}/cancel", "/payout").contains(template)) {
      String detail = e.code;
      if (template.equals("/payout")) {
        if (status == 409) detail = "DUPLICATE_REQUEST_ERROR";
        else if (!Set.of(
                "VALIDATION_ERROR", "INVALID_PAYMENT_DETAILS_ERROR", "INSUFFICIENT_BALANCE")
            .contains(detail)) detail = "VALIDATION_ERROR";
      } else if (status == 409) detail = "INVALID_CHARGE_STATE";
      else if (status == 404) detail = "RESOURCE_NOT_FOUND";
      else if (status == 422) detail = "BANK_HARD_ERROR";
      else if (!detail.equals("EXCEEDS_CAPTURE_LIMIT")) detail = "VALIDATION_ERROR";
      if (contracts.op(template, "post").path("responses").path("" + status).has("content"))
        return Reply.json(status, Json.obj("detail", detail, "error_description", e.getMessage()));
    }
    return Reply.empty(status);
  }

  private Map<String, Object> transaction(Connection c, String kind, String id)
      throws SQLException {
    return Database.one(
        c,
        "SELECT * FROM transactions WHERE client_id=? AND kind=? AND id=?",
        config.client,
        kind,
        id);
  }

  private Map<String, Object> account(Connection c, String id, String currency)
      throws SQLException {
    var a =
        Database.one(
            c,
            "SELECT * FROM accounts WHERE client_id=? AND account_id=? AND currency=?",
            config.client,
            id,
            currency);
    if (a == null) throw new ApiError(404, "VALIDATION_ERROR", "Unknown account");
    return a;
  }

  private void adjust(Connection c, String id, String cur, long available, long balance)
      throws SQLException {
    Database.execute(
        c,
        "UPDATE accounts SET available_e5=available_e5+?,balance_e5=balance_e5+? WHERE client_id=?"
            + " AND account_id=? AND currency=?",
        available,
        balance,
        config.client,
        id,
        cur);
  }

  private void insert(
      Connection c,
      String kind,
      JsonNode body,
      String id,
      String original,
      String acc,
      long amt,
      String cur,
      String method,
      String funding,
      String state,
      String detail,
      String hash,
      Reply response)
      throws SQLException {
    Database.execute(
        c,
        "INSERT INTO"
            + " transactions(client_id,kind,id,psp_reference,original_id,account_id,amount_e5,currency,capture_method,funding_method,state,detail,request_hash,response_status,response_body,expires_at)"
            + " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,CASE WHEN ?='AUTHORIZED' THEN now()+interval"
            + " '72 hours' ELSE NULL END)",
        config.client,
        kind,
        id,
        ref(kind, id),
        original,
        acc,
        amt,
        cur,
        method,
        funding,
        state,
        detail,
        hash,
        response.status(),
        response.body(),
        state);
  }

  private Reply create(Connection c, String kind, JsonNode b, String hash) throws Exception {
    String id = Json.text(b, "id");
    var old = transaction(c, kind, id);
    if (old != null) {
      if (!Database.str(old, "request_hash").equals(hash))
        throw new ApiError(
            kind.equals("payout") ? 409 : 400,
            kind.equals("payout") ? "DUPLICATE_REQUEST_ERROR" : "VALIDATION_ERROR",
            "Transaction ID reused");
      return new Reply(
          ((Number) old.get("response_status")).intValue(),
          Database.str(old, "response_body"),
          Map.of("Content-Type", "application/json"));
    }
    long n = value(b.path("amount"));
    String cur = b.path("amount").path("currency").asText();
    String acc =
        kind.equals("charge")
                && b.path("authorization")
                    .path("type")
                    .asText()
                    .equals("access_token_authorization")
            ? b.path("authorization").path("account_id").asText()
            : "demo-account";
    if (kind.equals("payout")) {
      String payee = b.path("payee").path("account").path("account_id").asText();
      Map<String, String> declines =
          Map.of(
              "inactive-account",
              "INACTIVE_ACCOUNT_ERROR",
              "fraud-account",
              "PAYMENT_FRAUD_ERROR",
              "hard-decline-account",
              "PAYMENT_PSP_HARD_DECLINE_ERROR",
              "bank-hard-account",
              "BANK_HARD_ERROR",
              "merchant-rejected-account",
              "MERCHANT_REJECTED",
              "network-error-account",
              "PAYMENT_NETWORK_ERROR",
              "psp-error-account",
              "PAYMENT_PSP_ERROR",
              "payment-error-account",
              "PAYMENT_ERROR");
      if (payee.equals("invalid-account")
          || b.path("payee").path("document").path("id").asText().equals("invalid"))
        throw new ApiError(400, "INVALID_PAYMENT_DETAILS_ERROR", "Invalid synthetic wallet");
      if (declines.containsKey(payee))
        return Reply.json(422, Json.obj("detail", declines.get(payee)));
      account(c, payee, cur);
      acc = b.path("payer").path("id").asText();
    }
    var a = account(c, acc, cur);
    String funding = Json.text(b, "funding_method"),
        method = kind.equals("charge") ? Json.text(b, "capture_method") : "AUTOMATIC";
    PspAdapter.Decision decision =
        kind.equals("charge")
            ? adapter.charge(
                new PspAdapter.ChargeCommand(
                    funding, method, n, Database.number(a, "available_e5"), b),
                providerKey("charge", id))
            : adapter.payout(
                new PspAdapter.PayoutCommand(funding, n, Database.number(a, "available_e5"), b),
                providerKey("payout", id));
    String state = decision.state(),
        internal = state.equals("CONFIRMED") && method.equals("MANUAL") ? "AUTHORIZED" : state;
    ObjectNode st = Json.obj("value", state);
    if (decision.detail() != null) st.put("detail", decision.detail());
    ObjectNode result = Json.obj("state", st, "psp_reference", ref(kind, id));
    String actionToken = WalletService.random(), csrf = WalletService.random();
    ObjectNode completion =
        Json.obj("state", funding.equals("DEMO_PENDING_REJECT") ? "REJECTED" : "CONFIRMED");
    boolean action = kind.equals("charge") && decision.action();
    if (action) {
      completion.put("payment_token", actionToken);
      completion.put("csrf", csrf);
      completion.put("expires_at", System.currentTimeMillis() + config.actionTtlMs);
      completion.put("return_url", Json.text(b, "return_url"));
    }
    if (action && funding.equals("DEMO_WEBVIEW"))
      result.set(
          "actions",
          Json.MAPPER
              .createArrayNode()
              .add(
                  Json.obj(
                      "type",
                      "web_view_user_input_action",
                      "url",
                      config.publicBase.substring(
                              0, config.publicBase.length() - config.base.length())
                          + "/demo/action?token="
                          + enc(actionToken))));
    if (action && funding.equals("DEMO_QR"))
      result.set(
          "actions",
          Json.MAPPER
              .createArrayNode()
              .add(
                  Json.obj(
                      "type",
                      "qr_code_input_action",
                      "code",
                      "local-qr:" + actionToken,
                      "expiry_time_millis",
                      System.currentTimeMillis() + config.actionTtlMs)));
    Reply response = Reply.json(200, result);
    if (!state.equals("REJECTED")) adjust(c, acc, cur, -n, internal.equals("CONFIRMED") ? -n : 0);
    insert(
        c,
        kind,
        b,
        id,
        null,
        acc,
        n,
        cur,
        method,
        funding,
        internal,
        decision.detail(),
        hash,
        response);
    Database.execute(
        c,
        "UPDATE transactions SET source_account_id=?,payee_account_id=? WHERE client_id=? AND"
            + " kind=? AND id=?",
        kind.equals("charge")
            ? b.path("source").path("id").asText()
            : b.path("payer").path("id").asText(),
        kind.equals("payout") ? b.path("payee").path("account").path("account_id").asText() : null,
        config.client,
        kind,
        id);
    for (var item : b.path("payment_data"))
      if (item.path("type").asText().equals("order_payment_data"))
        Database.execute(
            c,
            "UPDATE transactions SET order_id=? WHERE client_id=? AND kind=? AND id=?",
            item.path("order_id").asText(),
            config.client,
            kind,
            id);
    if (kind.equals("payout") && state.equals("CONFIRMED"))
      adjust(c, b.path("payee").path("account").path("account_id").asText(), cur, n, n);
    if (state.equals("PENDING"))
      queue(
          c, kind, id, "COMPLETION", "", completion, action ? "WAITING" : "PENDING", config.delay);
    return response;
  }

  private String pathId(String resource) {
    String[] parts = resource.split("/");
    return java.net.URLDecoder.decode(
        parts[parts.length - 2].replace("+", "%2B"), StandardCharsets.UTF_8);
  }

  private String providerKey(String operation, String id) {
    return Json.hash(config.client + "/" + operation + "/" + id);
  }

  String accountForProtectedOperation(String template, String resource, JsonNode body) {
    String id =
        template.equals("/refund")
            ? Json.text(body, "original_transaction_id")
            : template.contains("{id}") ? pathId(resource) : null;
    if (id == null) return null;
    try (Connection c = db.open()) {
      var t = transaction(c, "charge", id);
      return t == null ? null : Database.str(t, "account_id");
    } catch (SQLException e) {
      throw new IllegalStateException("Account binding unavailable", e);
    }
  }

  private Map<String, Object> authorized(Connection c, String id) throws SQLException {
    var t = transaction(c, "charge", id);
    if (t == null) throw new ApiError(404, "CHARGE_NOT_FOUND", "Charge not found");
    expire(c, t);
    t = transaction(c, "charge", id);
    if (!Database.str(t, "state").equals("AUTHORIZED"))
      throw new ApiError(409, "INVALID_CHARGE_STATE", "Charge is not authorized");
    return t;
  }

  private Reply capture(Connection c, String path, JsonNode b) throws SQLException {
    String id = pathId(path);
    var t = authorized(c, id);
    if (b.has("psp_reference")
        && !Json.text(b, "psp_reference").equals(Database.str(t, "psp_reference")))
      throw new ApiError(400, "VALIDATION_ERROR", "Reference mismatch");
    long n = value(b.path("amount_to_capture")), original = Database.number(t, "amount_e5");
    PspAdapter.Decision result =
        adapter.capture(
            new PspAdapter.CaptureCommand(
                Database.str(t, "state"),
                original,
                Database.str(t, "currency"),
                n,
                b.path("amount_to_capture").path("currency").asText(),
                Database.str(t, "funding_method")),
            providerKey("capture", id));
    if (result.state().equals("REJECTED")) {
      adjust(c, Database.str(t, "account_id"), Database.str(t, "currency"), original, 0);
      setState(c, "charge", id, "REJECTED", result.detail());
      return Reply.json(
          422, Json.obj("detail", result.detail() == null ? "BANK_HARD_ERROR" : result.detail()));
    }
    if (!result.state().equals("CONFIRMED"))
      throw new IllegalStateException("Unexpected capture decision");
    adjust(c, Database.str(t, "account_id"), Database.str(t, "currency"), 0, -n);
    setState(c, "charge", id, result.state(), result.detail());
    return Reply.empty(200);
  }

  private Reply cancel(Connection c, String path, JsonNode b) throws SQLException {
    String id = pathId(path);
    var t = authorized(c, id);
    if (b.has("psp_reference")
        && !Json.text(b, "psp_reference").equals(Database.str(t, "psp_reference")))
      throw new ApiError(400, "VALIDATION_ERROR", "Reference mismatch");
    PspAdapter.Decision result =
        adapter.cancel(
            new PspAdapter.CancelCommand(Database.str(t, "state")), providerKey("cancel", id));
    adjust(
        c,
        Database.str(t, "account_id"),
        Database.str(t, "currency"),
        Database.number(t, "amount_e5"),
        0);
    setState(c, "charge", id, result.state(), result.detail());
    return Reply.empty(200);
  }

  private Reply find(Connection c, Map<String, String> q) throws SQLException {
    String id = q.getOrDefault("id", "");
    if (id.isBlank()) throw new ApiError(400, "VALIDATION_ERROR", "ID required");
    var t =
        Database.one(
            c,
            "SELECT * FROM transactions WHERE client_id=? AND id=? AND kind IN ('charge','refund')"
                + " ORDER BY kind LIMIT 1",
            config.client,
            id);
    PspAdapter.Snapshot snapshot =
        t == null
            ? null
            : new PspAdapter.Snapshot(
                id,
                Database.str(t, "psp_reference"),
                Database.str(t, "state"),
                Database.number(t, "amount_e5"),
                Database.str(t, "currency"),
                Database.str(t, "updated_at"));
    snapshot =
        adapter.lookupOperation(
            new PspAdapter.LookupCommand(
                id,
                t == null ? providerKey("lookup", id) : Database.str(t, "psp_reference"),
                snapshot));
    if (snapshot == null) throw new ApiError(404, "not_found", "Unknown transaction");
    if (q.containsKey("merchant_reference")
        && !q.get("merchant_reference").equals(snapshot.reference()))
      throw new ApiError(404, "VALIDATION_ERROR", "Reference mismatch");
    String status = snapshot.state();
    if (status.equals("CANCELLED"))
      throw new ApiError(400, "VALIDATION_ERROR", "Cancelled lookup status is not declared");
    ObjectNode out =
        Json.obj(
            "id",
            id,
            "merchant_reference",
            snapshot.reference(),
            "status",
            status,
            "amount",
            amount(snapshot.amountE5(), snapshot.currency()));
    if (status.equals("CONFIRMED")) out.put("confirmed_at", snapshot.updatedAt());
    return Reply.json(200, out);
  }

  private Reply refund(Connection c, JsonNode b, String hash) throws Exception {
    String id = Json.text(b, "id");
    var old = transaction(c, "refund", id);
    if (old != null) {
      if (!hash.equals(Database.str(old, "request_hash")))
        throw new ApiError(400, "VALIDATION_ERROR", "Refund ID reused");
      return new Reply(
          ((Number) old.get("response_status")).intValue(),
          Database.str(old, "response_body"),
          Map.of("Content-Type", "application/json"));
    }
    var original = transaction(c, "charge", Json.text(b, "original_transaction_id"));
    if (original == null) throw new ApiError(404, "not_found", "Original transaction not found");
    if (!Database.str(original, "state").equals("CONFIRMED")
        || !Database.str(original, "psp_reference")
            .equals(Json.text(b, "original_merchant_reference")))
      throw new ApiError(400, "VALIDATION_ERROR", "Original charge is not refundable");
    long n = value(b.path("amount"));
    String cur = b.path("amount").path("currency").asText();
    if (!cur.equals(Database.str(original, "currency")))
      throw new ApiError(400, "VALIDATION_ERROR", "Currency mismatch");
    var sum =
        Database.one(
            c,
            "SELECT COALESCE(sum(amount_e5),0) AS total FROM transactions WHERE client_id=? AND"
                + " kind='refund' AND original_id=? AND state IN ('PENDING','CONFIRMED')",
            config.client,
            Json.text(b, "original_transaction_id"));
    long remaining =
        java.math.BigDecimal.valueOf(Database.number(original, "amount_e5"))
            .subtract(new java.math.BigDecimal(sum.get("total").toString()))
            .longValueExact();
    String funding = Database.str(original, "funding_method");
    PspAdapter.Decision decision =
        adapter.refund(
            new PspAdapter.RefundCommand(funding, n, remaining, b), providerKey("refund", id));
    String state = decision.state();
    Reply response =
        Reply.json(201, Json.obj("status", state, "merchant_reference", ref("refund", id)));
    String acc = Database.str(original, "account_id");
    if (state.equals("CONFIRMED")) adjust(c, acc, cur, n, n);
    insert(
        c,
        "refund",
        b,
        id,
        Json.text(b, "original_transaction_id"),
        acc,
        n,
        cur,
        "AUTOMATIC",
        funding,
        state,
        null,
        hash,
        response);
    Database.execute(
        c,
        "UPDATE transactions SET source_account_id=? WHERE client_id=? AND kind='refund' AND id=?",
        original.get("source_account_id"),
        config.client,
        id);
    if (state.equals("PENDING"))
      queue(
          c,
          "refund",
          id,
          "COMPLETION",
          "",
          Json.obj("state", "CONFIRMED"),
          "PENDING",
          config.delay);
    return response;
  }

  private Reply balance(Connection c, JsonNode b) throws SQLException {
    var a = account(c, Json.text(b, "account_id"), "BRL");
    PspAdapter.BalanceResult result =
        adapter.getBalance(
            new PspAdapter.BalanceCommand(
                Json.text(b, "funding_method"),
                "BRL",
                Database.number(a, "available_e5"),
                Database.number(a, "balance_e5"),
                b));
    return Reply.json(
        200,
        Json.obj(
            "available_balance",
            amount(result.availableE5(), result.currency()),
            "balance",
            amount(result.balanceE5(), result.currency())));
  }

  void queue(
      Connection c,
      String kind,
      String id,
      String type,
      String callback,
      JsonNode payload,
      String status,
      int delay)
      throws SQLException {
    Database.execute(
        c,
        "INSERT INTO"
            + " jobs(id,client_id,tx_kind,tx_id,job_type,callback_name,payload,status,next_attempt_at)"
            + " VALUES(?,?,?,?,?,?,?,?,now()+(? * interval '1 millisecond')) ON CONFLICT DO"
            + " NOTHING",
        Json.hash(config.client + "/" + kind + "/" + id + "/" + type + "/" + callback),
        config.client,
        kind,
        id,
        type,
        callback,
        Json.string(payload),
        status,
        delay);
  }

  void setState(Connection c, String kind, String id, String state, String detail)
      throws SQLException {
    Database.execute(
        c,
        "UPDATE transactions SET state=?,detail=?,updated_at=now() WHERE client_id=? AND kind=? AND"
            + " id=?",
        state,
        detail,
        config.client,
        kind,
        id);
  }

  void complete(Connection c, String kind, String id, String state) throws SQLException {
    complete(c, kind, id, state, "MERCHANT_REJECTED");
  }

  void complete(Connection c, String kind, String id, String state, String detail)
      throws SQLException {
    if (!Set.of("charge", "refund", "payout").contains(kind)
        || !Set.of("CONFIRMED", "REJECTED").contains(state))
      throw new ApiError(400, "VALIDATION_ERROR", "Invalid completion");
    var t = transaction(c, kind, id);
    if (t == null || !Database.str(t, "state").equals("PENDING"))
      throw new ApiError(400, "VALIDATION_ERROR", "Transaction is not pending");
    long n = Database.number(t, "amount_e5");
    String acc = Database.str(t, "account_id"), cur = Database.str(t, "currency");
    if (kind.equals("refund")) {
      if (state.equals("CONFIRMED")) adjust(c, acc, cur, n, n);
    } else if (state.equals("CONFIRMED")) adjust(c, acc, cur, 0, -n);
    else adjust(c, acc, cur, n, 0);
    if (kind.equals("payout") && state.equals("CONFIRMED"))
      adjust(c, Database.str(t, "payee_account_id"), cur, n, n);
    setState(c, kind, id, state, state.equals("REJECTED") ? detail : null);
    String cb =
        kind.equals("charge")
            ? "chargeCallback"
            : kind.equals("refund") ? "updateRefund" : "payoutCallback";
    ObjectNode payload =
        kind.equals("refund")
            ? Json.obj("status", state, "merchant_reference", Database.str(t, "psp_reference"))
            : Json.obj(
                "state",
                Json.obj("value", state),
                "psp_reference",
                Database.str(t, "psp_reference"));
    if (!kind.equals("refund") && state.equals("REJECTED"))
      ((ObjectNode) payload.path("state")).put("detail", detail);
    if (kind.equals("refund") && state.equals("REJECTED"))
      payload.put("failed_reason", "Demo rejection");
    contracts.callback(cb, payload);
    queue(c, kind, id, "CALLBACK", cb, payload, "PENDING", 0);
    Database.execute(
        c,
        "UPDATE jobs SET status='DONE',lease_until=NULL WHERE client_id=? AND tx_kind=? AND tx_id=?"
            + " AND job_type='COMPLETION'",
        config.client,
        kind,
        id);
  }

  void expire(Connection c, Map<String, Object> t) throws SQLException {
    if (!Database.str(t, "state").equals("AUTHORIZED") || t.get("expires_at") == null) return;
    if (Instant.parse(Database.str(t, "expires_at")).isAfter(Instant.now())) return;
    adjust(
        c,
        Database.str(t, "account_id"),
        Database.str(t, "currency"),
        Database.number(t, "amount_e5"),
        0);
    setState(c, "charge", Database.str(t, "id"), "CANCELLED", "MERCHANT_EXPIRED");
  }
}
