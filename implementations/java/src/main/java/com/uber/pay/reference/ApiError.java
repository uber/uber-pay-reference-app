package com.uber.pay.reference;

final class ApiError extends RuntimeException {
  final int status;
  final String code;

  ApiError(int status, String code, String message) {
    super(message);
    this.status = status;
    this.code = code;
  }
}
