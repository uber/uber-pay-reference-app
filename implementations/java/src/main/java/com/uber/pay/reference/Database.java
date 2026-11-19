package com.uber.pay.reference;

import java.net.*;
import java.nio.file.Files;
import java.sql.*;
import java.util.*;

final class Database implements AutoCloseable {
  final com.zaxxer.hikari.HikariDataSource pool;
  final Config config;
  final String url, user, password;

  Database(Config c) {
    config = c;
    URI uri = URI.create(c.db);
    if (!Set.of("postgres", "postgresql").contains(uri.getScheme()) || uri.getHost() == null)
      throw new IllegalArgumentException("DATABASE_URL must be PostgreSQL");
    if (!Set.of("localhost", "127.0.0.1", "::1", "[::1]", "db", "postgres").contains(uri.getHost()))
      throw new IllegalArgumentException("Local mode refuses a nonlocal PostgreSQL database");
    String[] auth = Optional.ofNullable(uri.getUserInfo()).orElse("uberpay:uberpay").split(":", 2);
    user = URLDecoder.decode(auth[0], java.nio.charset.StandardCharsets.UTF_8);
    password =
        auth.length > 1 ? URLDecoder.decode(auth[1], java.nio.charset.StandardCharsets.UTF_8) : "";
    url =
        "jdbc:postgresql://"
            + uri.getHost()
            + ":"
            + (uri.getPort() < 0 ? 5432 : uri.getPort())
            + uri.getPath()
            + "?connectTimeout=5";
    var pc = new com.zaxxer.hikari.HikariConfig();
    pc.setJdbcUrl(url);
    pc.setUsername(user);
    pc.setPassword(password);
    pc.setMaximumPoolSize(10);
    pc.setConnectionTimeout(5000);
    pc.setInitializationFailTimeout(-1);
    pc.addDataSourceProperty("ApplicationName", config.client);
    pc.addDataSourceProperty("options", "-c statement_timeout=5000 -c lock_timeout=1500");
    pool = new com.zaxxer.hikari.HikariDataSource(pc);
  }

  Connection open() throws SQLException {
    return pool.getConnection();
  }

  public void close() {
    pool.close();
  }

  void initialize() {
    try (Connection c = open()) {
      String sql = Files.readString(config.migration);
      try (Statement st = c.createStatement()) {
        st.execute(sql);
      }
      for (String account : List.of("demo-account", "demo-settlement", "insufficient-account"))
        execute(
            c,
            "INSERT INTO accounts(client_id,account_id,currency,available_e5,balance_e5)"
                + " VALUES(?,?,'BRL',?,?) ON CONFLICT DO NOTHING",
            config.client,
            account,
            !account.equals("insufficient-account") ? 1000000000000000000L : 100000L,
            !account.equals("insufficient-account") ? 1000000000000000000L : 100000L);
    } catch (Exception e) {
      throw new IllegalStateException("Database initialization failed", e);
    }
  }

  interface Work<T> {
    T run(Connection c) throws Exception;
  }

  <T> T atomic(Work<T> w) {
    return transaction(w, true);
  }

  <T> T raw(Work<T> w) {
    return transaction(w, false);
  }

  <T> T transaction(Work<T> w, boolean serialize) {
    try (Connection c = open()) {
      c.setAutoCommit(false);
      try {
        if (serialize) execute(c, "SELECT pg_advisory_xact_lock(hashtext(?))", config.client);
        T result = w.run(c);
        c.commit();
        return result;
      } catch (Exception e) {
        c.rollback();
        if (e instanceof RuntimeException r) throw r;
        throw new IllegalStateException("Database operation failed", e);
      }
    } catch (SQLException e) {
      throw new IllegalStateException("Database unavailable", e);
    }
  }

  static int execute(Connection c, String sql, Object... args) throws SQLException {
    try (PreparedStatement st = c.prepareStatement(sql)) {
      bind(st, args);
      if (st.execute()) return 0;
      return st.getUpdateCount();
    }
  }

  static List<Map<String, Object>> query(Connection c, String sql, Object... args)
      throws SQLException {
    try (PreparedStatement st = c.prepareStatement(sql)) {
      bind(st, args);
      try (ResultSet rs = st.executeQuery()) {
        List<Map<String, Object>> out = new ArrayList<>();
        while (rs.next()) {
          Map<String, Object> row = new LinkedHashMap<>();
          for (int i = 1; i <= rs.getMetaData().getColumnCount(); i++) {
            Object v = rs.getObject(i);
            if (v instanceof Timestamp ts) v = ts.toInstant().toString();
            row.put(rs.getMetaData().getColumnLabel(i), v);
          }
          out.add(row);
        }
        return out;
      }
    }
  }

  static Map<String, Object> one(Connection c, String sql, Object... args) throws SQLException {
    List<Map<String, Object>> rows = query(c, sql, args);
    return rows.isEmpty() ? null : rows.get(0);
  }

  static void bind(PreparedStatement s, Object... args) throws SQLException {
    for (int i = 0; i < args.length; i++) s.setObject(i + 1, args[i]);
  }

  static String str(Map<String, Object> r, String k) {
    return Objects.toString(r.get(k), "");
  }

  static long number(Map<String, Object> r, String k) {
    return ((Number) r.get(k)).longValue();
  }
}
