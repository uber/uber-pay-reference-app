// this middleware logs incoming requests as both a debugging helper and tracker.
async function log(req, res, next) {
  console.log(` info : ${req.method} - ${req.url}`);
  await next();
  console.log(` info : ${res.statusCode}`);
}

module.exports = log;
