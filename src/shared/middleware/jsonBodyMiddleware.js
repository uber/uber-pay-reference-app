const bodyParser    = require("body-parser");

module.exports = bodyParser.json({
    // Gives access to raw request body
    verify: (r,s,b,e) => { r.rawBody = b; }
});