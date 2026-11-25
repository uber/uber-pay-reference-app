package com.uber.pay.reference;

import jakarta.servlet.*;
import jakarta.servlet.http.*;
import java.io.*;
import java.util.concurrent.atomic.AtomicBoolean;

/** Buffer bounded raw bytes without occupying Tomcat request threads during slow uploads. */
final class BoundedBodyFilter implements Filter {
  static final String BODY = BoundedBodyFilter.class.getName() + ".body";

  public void doFilter(ServletRequest request, ServletResponse response, FilterChain chain)
      throws IOException, ServletException {
    HttpServletRequest req = (HttpServletRequest) request;
    HttpServletResponse res = (HttpServletResponse) response;
    if (!req.getMethod().equals("POST")
        && (req.getContentLengthLong() > 0 || req.getHeader("Transfer-Encoding") != null)) {
      res.setStatus(400);
      return;
    }
    if (!req.getMethod().equals("POST") || req.getContentLengthLong() == 0) {
      chain.doFilter(request, response);
      return;
    }
    if (req.getContentLengthLong() > 1048576) {
      res.setStatus(413);
      return;
    }
    AsyncContext async = req.startAsync();
    async.setTimeout(10000); // Total elapsed upload time, not a renewable idle timeout.
    AtomicBoolean finished = new AtomicBoolean();
    ByteArrayOutputStream bytes = new ByteArrayOutputStream();
    async.addListener(
        new AsyncListener() {
          public void onComplete(AsyncEvent e) {}

          public void onStartAsync(AsyncEvent e) {}

          public void onError(AsyncEvent e) {
            fail(400);
          }

          public void onTimeout(AsyncEvent e) {
            fail(408);
          }

          private void fail(int status) {
            if (finished.compareAndSet(false, true)) {
              res.setStatus(status);
              res.setHeader("Connection", "close");
              async.complete();
            }
          }
        });
    ServletInputStream input = req.getInputStream();
    input.setReadListener(
        new ReadListener() {
          public void onDataAvailable() throws IOException {
            byte[] chunk = new byte[8192];
            while (!finished.get() && input.isReady() && !input.isFinished()) {
              int n = input.read(chunk);
              if (n < 0) break;
              if (bytes.size() + n > 1048576) {
                onError(new IOException("Body limit exceeded"));
                return;
              }
              bytes.write(chunk, 0, n);
            }
          }

          public void onAllDataRead() {
            if (finished.compareAndSet(false, true)) {
              req.setAttribute(BODY, bytes.toByteArray());
              async.dispatch();
            }
          }

          public void onError(Throwable e) {
            if (finished.compareAndSet(false, true)) {
              res.setStatus(400);
              res.setHeader("Connection", "close");
              async.complete();
            }
          }
        });
  }
}
