// this middleware logs incoming requests as both a debugging helper and tracker.
module.exports = async function(req, res, next) {
    console.log(" info : " + req.method + " - " + req.url);
    await next();
    console.log(" info : " + res.statusCode);
};