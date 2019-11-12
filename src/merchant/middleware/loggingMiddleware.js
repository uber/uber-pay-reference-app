// this middleware logs incoming requests as both a debugging helper and tracker.
class LoggingMiddleware {
  constructor(stream) {
    this.stream = stream;
  }

  async route(req, res, next) {
    this.stream.write(`info : ${req.method} - ${req.url}\n`);
    await next();
    this.stream.write(`info : ${res.statusCode}\n`);
  }
}

module.exports = LoggingMiddleware;