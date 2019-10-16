const Router    = require('express').Router();
const axios     = require("axios");
const crypto    = require("crypto");

const privateKey = JSON.parse("\"" + process.env.UBER_PRIV_KEY + "\"");
// Address to the merchant app.
const host = "127.0.0.1:8080";

Router.get("/", (req, res) => {
    return res.render("index");
});

Router.post("/init-deposit", async (req, res) => {
    // Create digest hash from request body
    let digest = createDigest(req.rawBody);
    let date = new Date();

    let signature = await createSignature(date, host, digest);
    if (signature == null) {
      console.error("Signature creation failed.");
      return
    }

    let response = await axios({
        // TODO: turn this into endpoint
        url: `http://${host}/v1/init-deposit`,
        method: "POST",
        data: req.body,
        headers: {
            "Content-Type": "application/json",
            "Date": date.toUTCString(),
            "Digest": digest,
            "Signature": signature,
            "X-Idempotency-Key": "c900d4dd-7070-4e0b-9323-8f24cfde0490",
        }
    })
    if(response.status >= 200 && response < 300) {
        console.log(response.status);
        return res.status(response.status)
            .send("error");
    }
    console.log("success");
    return res.status(200)
        .set("Location", response.headers.location)
        .send();
});

// Create a digest from the request body
// @param body: string - request body of a web request.
function createDigest(body) {
    const buffer = Buffer.from(body);
    const digest = crypto.createHash("sha256")
        .update(buffer)
        .digest();
    // return HashAlgorithm=DigestBody, as shown in https://tools.ietf.org/html/rfc3230#section-4.1.1
    return "SHA-256=" + digest.toString("base64");
}

async function createSignature(date, host, digest) {
    const newline = "\n";
    
    if(privateKey == null 
        || privateKey.length == 0) {
        throw "Private key is not set up in .env file. Please enter your private key and restart this app.";
    }

    // Start building the signature headers
    let signature = "";
    signature += "keyId=\"key-rsa-1\",";
    signature += "algorithm=\"rsa-sha256\",";
    signature += "headers=\"(request-target) host date digest\",";

    // Start building the payload
    let payload = "";
    payload += "(request-target): post /v1/init-deposit" + newline;
    payload += "host:" + " " + host + newline;
    payload += "date:" + " " + date.toUTCString() + newline;
    payload += "digest:" + " " + digest;

    console.log(payload);

    // Sign the signature with sha256 as hashing algorithm
    let signedPayload = await crypto.createSign("sha256")
    // Hash the payload built above
        .update(payload)
        .sign({
            // Use your Private key to sign it
            key: privateKey,
            // Uber uses RSA-RSS, so make sure to set padding to such.
            padding: crypto.constants.RSA_PKCS1_PADDING
        });
  
    // Encode the signed payload to a base64 string
    let basePayload = signedPayload.toString("base64");

    // Add the base64 payload to the signature
    signature += `signature="${basePayload}"`;

    return signature;
}

module.exports = Router;
