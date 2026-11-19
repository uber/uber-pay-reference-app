package com.uber.pay.reference;

import java.util.*;

final class Ui {
  static Reply html(String content, String language) {
    var base = Reply.html(content);
    var headers = new HashMap<>(base.headers());
    headers.put("Content-Language", ReferenceFeatures.locale(language));
    return new Reply(base.status(), base.body(), headers);
  }
}
