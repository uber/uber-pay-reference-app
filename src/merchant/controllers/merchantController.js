const express   = require("express");
const crypto    = require("crypto");
const utils     = require("../utils/expressUtils");
const generator = require("../utils/simpleIdGenerator");

const Router    = express.Router();

const newline   = "\n";

// For more info read the integration guide located here 
// https://developer.uber.com/docs/secured/payments/references/api/v1/init-deposit
Router.post('/v1/init-deposit', async (req, res) => {
    // Generate a session Id for this deposit.
    let sessionId = generator.generateId();

    // Check if the request is valid; else return a bad request.
    if(!req.body.amount) {
        return utils.badRequest(res, {
            error: "required parameter 'amount' undefined."
        })
    }

    if(Number(req.body.amount.value) <= 0) {
        return utils.badRequest(res, {
            error: "required parameter 'amount.value' invalid or missing."
        });
    }

    if(!req.body.amount.currency) {
        return utils.badRequest(res, {
            error: "required parameter 'amount.currency' invalid or missing."
        });
    }

    if(!validateDigest(req)) {
        return utils.badRequest(res, {
            error: "invalid digest"
        });
    }

    if(!validateSignature(req)) {
        return utils.badRequest(res, {
            error: "signature could not be verified."
        });    
    }

    // Make sure this response has not timed out yet.
    if(!validateInitiatedAt(req.body.initiated_at)) {
        return res.status(403).send();
    }

    console.log("success");

    try
    {
        // Add the merchant metadata.
        req.body.merchant_reference = "599f4164-4417-4361-9d11-1d9d8b4b9096";
        
        req.body.source = {};
        req.body.source.owner_id = "a7b46663-3b97-40be-9b3e-ab805a56b269",
        req.body.source.id = "a148d933-8ed1-4304-8b70-514232386240"
        
        // These values don't need to go into the createDepositAsync
        delete req.body.return_url;
        delete req.body.initiated_at;
        
        let x = await res.uber.createDepositAsync(req.body);
        if(!x.status.toString().startsWith('2')) {
            throw x.status + ": " + x.statusText;    
        }
    
        res.cache.put(`session:${sessionId}`, {
            data: req.body,
            res: x.data
        });
    }
    catch(ex) {
        console.log(ex);
        return utils.badRequest(res, ex);
    }
    
    let host = process.env.DEVELOPMENT
        ? `http://${req.connection.localAddress}:${req.connection.localPort}`
        : `https://${req.hostname}`;
    return res.status(201)
        .set("Location", `${host}/payments/init?sessionId=${sessionId}`)
        .send();
});

function validateDigest(req) {
    // Fetch the digest of the body
    let digest = req.get("digest");

    // Create own digest
    let currentDigest = "SHA-256=" + crypto.createHash("sha256")
        .update(req.rawBody)
        .digest("base64");

    console.log(currentDigest);
    // Verify equality of digests.
    return digest == currentDigest;
}

function parseSignatureHeader(header) {
    let dict = {};
    let items = header.split(',');
    for(var idx in items) {
        let i = 0;
        let nameBuf = "";
        do {
            nameBuf += items[idx][i];
            i++;
        } while(items[idx][i] != '=');
        i++; // skip equals
        i++; // skip first quote
        let valueBuf = "";
        do {
            valueBuf += items[idx][i];
            i++;
        } while(items[idx][i] != '"');
        dict[nameBuf] = valueBuf;
    }

    return dict;
}

function validateSignature(req) {
    const publicKey = process.env.UBER_PUB_KEY;
    const signature = parseSignatureHeader(req.get("signature"));

    let payload = "";
    payload += "(request-target):" + " " + req.method.toLowerCase() + " " + req.path + newline;
    payload += "host:" + " " + req.get("host") + newline;
    payload += "date:" + " " + req.get("date") + newline;
    payload += "digest:" + " " + req.get("digest");

    const incomingBuffer = Buffer.from(signature["signature"], "base64");

    return crypto.createVerify("sha256")
        .update(payload)
        .verify({
            key: publicKey, 
            padding: crypto.constants.RSA_PKCS1_PADDING
        }, incomingBuffer);
}

function validateInitiatedAt(time) {
    return true;
}

module.exports = Router;