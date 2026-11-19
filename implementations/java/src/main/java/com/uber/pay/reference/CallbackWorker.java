package com.uber.pay.reference;

import com.fasterxml.jackson.databind.JsonNode;
import java.net.URI;
import java.net.http.*;
import java.time.Duration;
import java.util.*;
import java.util.concurrent.*;

final class CallbackWorker implements AutoCloseable {
  final Config config;
  final Database db;
  final PaymentService payments;
  final Contracts contracts;
  final HttpClient http =
      HttpClient.newBuilder()
          .connectTimeout(Duration.ofSeconds(2))
          .followRedirects(HttpClient.Redirect.NEVER)
          .build();
  final ScheduledExecutorService executor =
      Executors.newSingleThreadScheduledExecutor(
          r -> {
            Thread t = new Thread(r, "demo-completion-and-callback");
            t.setDaemon(true);
            return t;
          });

  record Token(String value, long expiry) {}

  final Map<String, Token> tokenCache = new HashMap<>();

  CallbackWorker(Config c, Database d, PaymentService p, Contracts ct) {
    config = c;
    db = d;
    payments = p;
    contracts = ct;
  }

  void start() {
    executor.scheduleWithFixedDelay(
        this::tick, config.interval, config.interval, TimeUnit.MILLISECONDS);
  }

  void tick() {
    try {
      db.atomic(
          c -> {
            for (var t :
                Database.query(
                    c,
                    "SELECT * FROM transactions WHERE client_id=? AND state='AUTHORIZED' AND"
                        + " expires_at<now()",
                    config.client)) payments.expire(c, t);
            for (var waiting :
                Database.query(
                    c,
                    "SELECT * FROM jobs WHERE client_id=? AND job_type='COMPLETION' AND"
                        + " status='WAITING' AND (payload::jsonb->>'expires_at')::bigint<=?",
                    config.client,
                    System.currentTimeMillis())) {
              payments.complete(
                  c,
                  Database.str(waiting, "tx_kind"),
                  Database.str(waiting, "tx_id"),
                  "REJECTED",
                  "MERCHANT_EXPIRED");
            }
            for (var j :
                Database.query(
                    c,
                    "SELECT * FROM jobs WHERE client_id=? AND job_type='COMPLETION' AND"
                        + " status='PENDING' AND next_attempt_at<=now() ORDER BY next_attempt_at"
                        + " LIMIT 20",
                    config.client)) {
              try {
                payments.complete(
                    c,
                    Database.str(j, "tx_kind"),
                    Database.str(j, "tx_id"),
                    Json.parse(Database.str(j, "payload")).path("state").asText(),
                    Json.parse(Database.str(j, "payload"))
                        .path("detail")
                        .asText("MERCHANT_REJECTED"),
                    Json.parse(Database.str(j, "payload")).path("amount_e5"));
              } catch (ApiError e) {
                Database.execute(
                    c, "UPDATE jobs SET status='DONE',lease_until=NULL WHERE id=?", j.get("id"));
              }
            }
            for (var review :
                Database.query(
                    c,
                    "SELECT * FROM jobs WHERE client_id=? AND job_type='REVERSAL' AND"
                        + " status='PENDING' AND next_attempt_at<=now()",
                    config.client)) {
              payments.reverseRefund(c, Database.str(review, "tx_id"));
              Database.execute(c, "UPDATE jobs SET status='DONE' WHERE id=?", review.get("id"));
            }
            return null;
          });
      var j =
          db.atomic(
              c -> {
                var r =
                    Database.one(
                        c,
                        "SELECT * FROM jobs j WHERE client_id=? AND job_type='CALLBACK' AND"
                            + " status='PENDING' AND next_attempt_at<=now() AND (lease_until IS"
                            + " NULL OR lease_until<now()) AND NOT EXISTS(SELECT 1 FROM jobs older"
                            + " WHERE older.client_id=j.client_id AND older.tx_kind=j.tx_kind AND"
                            + " older.tx_id=j.tx_id AND older.job_type='CALLBACK' AND"
                            + " older.event_sequence<j.event_sequence AND older.status<>'DONE')"
                            + " ORDER BY next_attempt_at,event_sequence LIMIT 1 FOR UPDATE SKIP"
                            + " LOCKED",
                        config.client);
                if (r != null)
                  Database.execute(
                      c,
                      "UPDATE jobs SET lease_until=now()+interval '15 seconds',attempts=attempts+1"
                          + " WHERE id=?",
                      r.get("id"));
                return r;
              });
      if (j != null) deliver(j);
    } catch (Exception ignored) {
      System.err.println(
          "worker iteration failed; durable work retained (no request or credentials logged)");
    }
  }

  synchronized String token(String scope) throws Exception {
    Token current = tokenCache.get(scope);
    if (current != null && current.expiry() > System.currentTimeMillis()) return current.value();
    String form =
        "grant_type=client_credentials&client_id="
            + PaymentService.enc(config.uberClient)
            + "&client_secret="
            + PaymentService.enc(config.uberSecret)
            + "&scope="
            + PaymentService.enc(scope);
    HttpResponse<String> r =
        http.send(
            HttpRequest.newBuilder(URI.create(config.tokenUrl))
                .timeout(Duration.ofSeconds(2))
                .header("Content-Type", "application/x-www-form-urlencoded")
                .POST(HttpRequest.BodyPublishers.ofString(form))
                .build(),
            HttpResponse.BodyHandlers.ofString());
    if (r.statusCode() != 200) throw new IllegalStateException("OAuth token unavailable");
    JsonNode b = Json.parse(r.body());
    String value = b.path("access_token").asText();
    long ttl = b.path("expires_in").asLong();
    if (value.isBlank() || ttl <= 0) throw new IllegalStateException("Invalid token response");
    tokenCache.put(
        scope,
        new Token(
            value, System.currentTimeMillis() + Math.max(0, ttl - Math.min(60, ttl / 10)) * 1000));
    return value;
  }

  void deliver(Map<String, Object> j) {
    int attempt = ((Number) j.get("attempts")).intValue() + 1;
    String status = "PENDING", error = null;
    try {
      String name = Database.str(j, "callback_name"), id = Database.str(j, "tx_id");
      JsonNode def = contracts.spec.path("components").path("callbacks").path(name);
      if (def.isMissingNode()) throw new IllegalStateException("Unknown callback");
      String absolute = def.fieldNames().next();
      JsonNode op = def.get(absolute).path("post");
      String scope = op.path("security").get(0).path("uber-oauth2").get(0).asText();
      String path =
          absolute
              .substring("https://api.uber.com".length())
              .replace("{$request.body#/id}", PaymentService.enc(id));
      String url = config.callbackBase + path;
      Config.localUrl(url);
      JsonNode payload = Json.parse(Database.str(j, "payload"));
      contracts.callback(name, payload);
      HttpRequest.Builder request =
          HttpRequest.newBuilder(URI.create(url))
              .timeout(Duration.ofSeconds(2))
              .header("Content-Type", "application/json")
              .header("Authorization", "Bearer " + token(scope))
              .header("X-Correlation-Id", Database.str(j, "id"));
      for (JsonNode param : op.path("parameters"))
        if (contracts.resolve(param).path("name").asText().equals("X-Idempotency-Key"))
          request.header("X-Idempotency-Key", Database.str(j, "id"));
      int code =
          http.send(
                  request
                      .POST(HttpRequest.BodyPublishers.ofString(Database.str(j, "payload")))
                      .build(),
                  HttpResponse.BodyHandlers.discarding())
              .statusCode();
      if (code >= 200 && code < 300) status = "DONE";
      else if (code == 409) {
        status = "RECONCILE";
        error = "Callback outcome requires reconciliation";
      } else if (code == 401) {
        synchronized (this) {
          tokenCache.remove(scope);
        }
        error = "Callback authorization rejected";
      } else if (code == 429 || code >= 500) error = "Retryable callback response";
      else {
        status = "FAILED";
        error = "Permanent callback rejection";
      }
    } catch (Exception e) {
      error = "Callback transport or credential failure";
    }
    if (status.equals("PENDING") && attempt >= config.maxAttempts) status = "FAILED";
    String out = status, err = error;
    long delay =
        Math.min(60000L, config.retryMs * (1L << Math.min(attempt - 1, 10)))
            + ThreadLocalRandom.current().nextInt(Math.max(1, config.retryMs / 4));
    db.atomic(
        c -> {
          Database.execute(
              c,
              "UPDATE jobs SET status=?,last_error=?,lease_until=NULL,next_attempt_at=now()+(? *"
                  + " interval '1 millisecond') WHERE id=? AND status='PENDING' AND attempts=? AND"
                  + " lease_until>now()",
              out,
              err,
              delay,
              j.get("id"),
              attempt);
          return null;
        });
  }

  public void close() {
    executor.shutdownNow();
  }
}
