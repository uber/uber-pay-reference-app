var path                    = require("path");

const express               = require("express");
const axios                 = require("axios");
const dotenv                = require("dotenv");

const jsonMiddleware        = require("../shared/middleware/jsonBodyMiddleware");
const memCacheMiddleware    = require("./middleware/requestCache");
const uberMiddleware        = require("./middleware/uberMiddleware");
const loggingMiddleware     = require("./middleware/loggingMiddleware");

// Load .env file
dotenv.config();

const app = express();
const port = 8080;

const scope = "payments.deposits";
const grantType = "client_credentials";

process.env.UBER_PUB_KEY = JSON.parse(`"${process.env.UBER_PUB_KEY}"`);

let clientId = process.env.UBER_CLIENT_ID;
let secretId = process.env.UBER_SECRET_ID;

const formUrlEncoded = function(x) { 
    return Object.keys(x)
        .reduce((p, c) => p + `&${c}=${encodeURIComponent(x[c])}`, '');
}

async function getUberAccessKey() {
    let loginRequest = await axios({
        url: "https://login.uber.com/oauth/v2/token",
        method: "POST",
        headers: {
            "Content-Type": "application/x-www-form-urlencoded"
        },
        data: formUrlEncoded({
            client_id: clientId,
            client_secret: secretId,
            scope: scope,
            grant_type: grantType
        })
    });
    return loginRequest.data;
}

async function main() {
    let login = await getUberAccessKey();
    
    // -- Middleware
    app.use("/resources", express.static(path.join(__dirname, "../shared/public")));

    app.use(jsonMiddleware);
    app.use(loggingMiddleware);
    app.use(memCacheMiddleware);
    app.use(uberMiddleware(login));

    app.use((_, res, next) => {
        res.set('Access-Control-Allow-Origin', '*');
        res.set("Access-Control-Allow-Methods", "POST");
        res.set("Access-Control-Allow-Headers", "Content-Type, Digest, Signature, X-Idempotency-Key");
        res.set("Access-Control-Expose-Headers", "Location");
        next();
    })

    app.set('view engine', 'pug');
    app.set('views', path.join(__dirname, "../shared/public"));

    // -- Routers
    app.use(require('./controllers/merchantController'));
    app.use(require('./controllers/paymentsController'));

    app.listen(port, "0.0.0.0", () => {
        console.log(`Uber payments deposit mock api listening on port ${port}!`);
    });
}

main();