package com.uber.pay.reference;

import java.net.URI;

final class ReturnUrls {
  static boolean allowed(Config c, String value) {
    try {
      if (value == null || value.matches("(?s).*[\\r\\n\\\\].*")) return false;
      URI u = URI.create(value).normalize();
      if (u.getHost() == null
          || u.getUserInfo() != null
          || u.getFragment() != null
          || !java.util.Set.of("https", "http", "uber").contains(u.getScheme())) return false;
      for (String entry : c.returnUrls) {
        URI r = URI.create(entry.trim()).normalize();
        String path = u.getPath() == null ? "" : u.getPath(),
            rp = r.getPath() == null ? "" : r.getPath();
        if (java.util.Arrays.stream(path.split("/")).anyMatch(p -> p.equals(".") || p.equals("..")))
          return false;
        if (u.getScheme().equals(r.getScheme())
            && u.getHost().equalsIgnoreCase(r.getHost())
            && u.getPort() == r.getPort()
            && (path.equals(rp) || rp.endsWith("/") && path.startsWith(rp))
            && r.getQuery() == null
            && r.getFragment() == null) return true;
      }
    } catch (IllegalArgumentException e) {
      return false;
    }
    return false;
  }
}
