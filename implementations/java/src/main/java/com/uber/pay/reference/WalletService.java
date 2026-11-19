package com.uber.pay.reference;

import com.fasterxml.jackson.databind.JsonNode;
import java.nio.charset.StandardCharsets;
import java.security.*;
import java.sql.*;
import java.util.*;

final class WalletService {
  static final Set<String> SCOPES = Set.of("charge", "refund", "get_balance", "offline_access");
  final Config config;
  final Database db;
  final Contracts contracts;
  final WalletProvider provider;

  WalletService(Config c, Database d, Contracts ct, WalletProvider provider) {
    config = c;
    db = d;
    contracts = ct;
    this.provider = provider;
    if (!provider.isLocalOnly())
      throw new IllegalArgumentException(
          "Real identity/consent needs reviewed sandbox integration, not the synthetic local"
              + " executor");
  }

  static String random() {
    byte[] b = new byte[32];
    new SecureRandom().nextBytes(b);
    return Base64.getUrlEncoder().withoutPadding().encodeToString(b);
  }

  static boolean equal(String a, String b) {
    return a != null
        && b != null
        && MessageDigest.isEqual(
            a.getBytes(StandardCharsets.UTF_8), b.getBytes(StandardCharsets.UTF_8));
  }

  String scopes(String v) {
    Set<String> s = new TreeSet<>(Arrays.asList(v.split(" ")));
    if (s.isEmpty() || !SCOPES.containsAll(s))
      throw new ApiError(400, "invalid_scope", "Invalid scope");
    return String.join(" ", s);
  }

  void put(
      Connection c,
      String token,
      String kind,
      String account,
      String scope,
      String redirect,
      String state,
      String family,
      int seconds)
      throws SQLException {
    Database.execute(
        c,
        "INSERT INTO"
            + " tokens(token_hash,kind,client_id,account_id,scope,redirect_uri,state,family_id,expires_at)"
            + " VALUES(?,?,?,?,?,?,?,?,now()+(? * interval '1 second'))",
        Json.hash(token),
        kind,
        config.client,
        account,
        scope,
        redirect,
        state,
        family,
        seconds);
  }

  Reply authorize(Map<String, String> q) {
    for (String k :
        List.of(
            "client_id",
            "response_type",
            "redirect_uri",
            "scope",
            "state",
            "authorization_details"))
      if (q.getOrDefault(k, "").isBlank())
        throw new ApiError(400, "invalid_request", "Missing " + k);
    if (!q.get("client_id").equals(config.client)
        || !q.get("response_type").equals("code")
        || !config.redirects.contains(q.get("redirect_uri")))
      throw new ApiError(400, "invalid_request", "Unregistered authorization request");
    String scope = scopes(q.get("scope"));
    JsonNode details = Json.parse(q.get("authorization_details"));
    contracts.validate(
        Json.obj(
            "type", "array", "items", Json.obj("$ref", "#/components/schemas/AuthorizationDetail")),
        details);
    if (details.isEmpty())
      throw new ApiError(400, "invalid_request", "Authorization details required");
    WalletProvider.Identity identity = provider.identityForConsent();
    String consent = random(), csrf = random();
    db.atomic(
        c -> {
          put(
              c,
              consent,
              "CONSENT",
              identity.accountId(),
              scope,
              q.get("redirect_uri"),
              Json.string(Json.obj("state", q.get("state"), "csrf", csrf)),
              UUID.randomUUID().toString(),
              600);
          return null;
        });
    return Reply.html(
        "<!doctype html><html><head><title>Local demo consent</title></head><body><h1>Local fake"
            + " wallet consent</h1><p>Authorize synthetic account demo-account. No real login or"
            + " money movement.</p><form method='post' action='/demo/consent'><input type='hidden'"
            + " name='consent_token' value='"
            + consent
            + "'><input type='hidden' name='csrf' value='"
            + csrf
            + "'><button name='decision' value='approve'>Approve</button><button name='decision'"
            + " value='deny'>Deny</button></form></body></html>");
  }

  Reply consent(Map<String, String> q) {
    return db.atomic(
        c -> {
          String token = q.getOrDefault("consent_token", "");
          var r =
              Database.one(
                  c,
                  "SELECT * FROM tokens WHERE token_hash=? AND kind='CONSENT' AND consumed=false"
                      + " AND expires_at>now()",
                  Json.hash(token));
          if (r == null) throw new ApiError(400, "invalid_request", "Invalid consent");
          JsonNode state = Json.parse(Database.str(r, "state"));
          if (!equal(state.path("csrf").asText(), q.get("csrf")))
            throw new ApiError(400, "invalid_request", "Invalid CSRF token");
          String decision = q.getOrDefault("decision", "");
          if (!Set.of("approve", "deny").contains(decision))
            throw new ApiError(400, "invalid_request", "Explicit decision required");
          Database.execute(
              c, "UPDATE tokens SET consumed=true WHERE token_hash=?", Json.hash(token));
          String uri = Database.str(r, "redirect_uri"), params;
          if (decision.equals("approve")) {
            String code = random();
            put(
                c,
                code,
                "CODE",
                Database.str(r, "account_id"),
                Database.str(r, "scope"),
                uri,
                null,
                Database.str(r, "family_id"),
                600);
            params = "code=" + PaymentService.enc(code);
          } else params = "error=access_denied";
          params += "&state=" + PaymentService.enc(state.path("state").asText());
          return new Reply(
              302,
              "",
              Map.of(
                  "Location",
                  uri + (uri.contains("?") ? "&" : "?") + params,
                  "Cache-Control",
                  "no-store"));
        });
  }

  Reply token(JsonNode b) {
    if (!equal(Json.text(b, "client_id"), config.client)
        || !equal(Json.text(b, "client_secret"), config.secret))
      return Reply.json(400, Json.obj("error", "invalid_client"));
    try {
      return db.atomic(
          c -> {
            String grant = Json.text(b, "grant_type"),
                acc = "demo-account",
                scope = "",
                family = UUID.randomUUID().toString();
            if (grant.equals("authorization_code")) {
              var r =
                  Database.one(
                      c,
                      "SELECT * FROM tokens WHERE token_hash=? AND kind='CODE' AND client_id=? AND"
                          + " expires_at>now()",
                      Json.hash(Json.text(b, "code")),
                      config.client);
              if (r == null
                  || !equal(Database.str(r, "redirect_uri"), Json.text(b, "redirect_uri")))
                return Reply.json(400, Json.obj("error", "invalid_grant"));
              if (Boolean.TRUE.equals(r.get("consumed"))) {
                Database.execute(
                    c,
                    "UPDATE tokens SET consumed=true WHERE family_id=?",
                    Database.str(r, "family_id"));
                return Reply.json(400, Json.obj("error", "invalid_grant"));
              }
              Database.execute(
                  c, "UPDATE tokens SET consumed=true WHERE token_hash=?", r.get("token_hash"));
              acc = Database.str(r, "account_id");
              scope = Database.str(r, "scope");
              family = Database.str(r, "family_id");
            } else if (grant.equals("refresh_token")) {
              var r =
                  Database.one(
                      c,
                      "SELECT * FROM tokens WHERE token_hash=? AND kind='REFRESH' AND client_id=?"
                          + " AND consumed=false AND expires_at>now()",
                      Json.hash(Json.text(b, "refresh_token")),
                      config.client);
              if (r == null) return Reply.json(400, Json.obj("error", "invalid_grant"));
              String access = random();
              put(
                  c,
                  access,
                  "ACCESS",
                  Database.str(r, "account_id"),
                  Database.str(r, "scope"),
                  null,
                  null,
                  Database.str(r, "family_id"),
                  600);
              Reply out =
                  Reply.json(
                      200,
                      Json.obj(
                          "access_token", access, "token_type", "bearer", "expires_in", "600"));
              contracts.response("/token", 200, Json.parse(out.body()), grant);
              return out;
            } else if (grant.equals("urn:uberpay:params:oauth:grant-type:wallet-grant")) {
              WalletProvider.Identity identity =
                  provider.exchangeWalletReference(
                      b.path("wallet_reference_data").path("payment_identifier").asText(),
                      Json.text(b, "account_id"),
                      Json.text(b, "funding_method"),
                      Json.text(b, "country_iso2"));
              acc = identity.accountId();
              scope = scopes(Json.text(b, "scope"));
            } else return Reply.json(400, Json.obj("error", "unsupported_grant_type"));
            String refresh = random();
            put(c, refresh, "REFRESH", acc, scope, null, null, family, 2592000);
            WalletProvider.Identity identity = provider.describeAccount(acc);
            JsonNode detail =
                Json.obj(
                    "type",
                    "authorized_account",
                    "account_id",
                    acc,
                    "fingerprint",
                    identity.fingerprint(),
                    "display_name",
                    identity.displayName());
            Reply out =
                Reply.json(
                    200,
                    Json.obj(
                        "refresh_token",
                        refresh,
                        "token_type",
                        "bearer",
                        "scope",
                        scope,
                        "authorization_details",
                        Json.MAPPER.createArrayNode().add(detail),
                        "expires_in",
                        "2592000"));
            contracts.response("/token", 200, Json.parse(out.body()), grant);
            return out;
          });
    } catch (ApiError e) {
      return Reply.json(
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
                  : "invalid_request"));
    }
  }

  String bearer(String header, String scope, String account) {
    if (header == null || !header.regionMatches(true, 0, "Bearer ", 0, 7) || header.length() > 2048)
      throw new ApiError(401, "invalid_token", "Bearer token required");
    try (Connection c = db.open()) {
      var r =
          Database.one(
              c,
              "SELECT * FROM tokens WHERE token_hash=? AND kind='ACCESS' AND client_id=? AND"
                  + " consumed=false AND expires_at>now()",
              Json.hash(header.substring(7)),
              config.client);
      if (r == null
          || !Set.of(Database.str(r, "scope").split(" ")).contains(scope)
          || account != null && !account.equals(Database.str(r, "account_id")))
        throw new ApiError(401, "invalid_token", "Token scope/account invalid");
      return Database.str(r, "account_id");
    } catch (SQLException e) {
      throw new IllegalStateException("Token store unavailable", e);
    }
  }
}
