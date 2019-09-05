const path              = require("path");

const dotenv            = require("dotenv");
const express           = require("express");

const jsonMiddleware    = require("../shared/middleware/jsonBodyMiddleware");

const app = express();
const port = 80;

dotenv.config();

// fix for newlines in dotenv: https://github.com/motdotla/dotenv/issues/218
process.env.UBER_PRIV_KEY = JSON.parse(`"${process.env.UBER_PRIV_KEY}"`);

async function main() {
    // -- Middleware
    app.use("/resources", express.static(path.join(__dirname, '../shared/public')));
    
    app.use(jsonMiddleware);
    
    app.use((_, res, next) => {
        res.set('Access-Control-Allow-Origin', '*');
        res.set("Access-Control-Allow-Methods", "POST");
        res.set("Access-Control-Allow-Headers", "Content-Type, Digest, Signature, X-Idempotency-Key");
        res.set("Access-Control-Expose-Headers", "Location");
        next();
    });

    app.set('view engine', 'pug');
    app.set('views', path.join(__dirname, '../shared/public'));

    app.use(require("./controllers/appController.js"));

    app.listen(port, "0.0.0.0", () => {
        console.log(`Uber payments app listening on port ${port}!`);
    });
}
main();