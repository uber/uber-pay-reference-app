const crypto = require('crypto');

const newline = '\n';

// Create a digest from the request body
/** @param body request body of a web request. */
function createDigest(body) {
  const buffer = Buffer.from(body);
  const digest = crypto.createHash('sha256')
    .update(buffer)
    .digest();
  // return HashAlgorithm=DigestBody, as shown in https://tools.ietf.org/html/rfc3230#section-4.1.1
  return `SHA-256=${digest.toString('base64')}`;
}

// Validate a digest with the current request body.
function validateDigest(digest, requestBody) {
  // Verify equality of digests.
  return digest === createDigest(requestBody);
}

function createPayload(requestTarget, date, host, digest) {
  let payload = '';
  payload += `(request-target): ${requestTarget}${newline}`;
  payload += `${'host: '}${host}${newline}`;
  payload += `${'date: '}${date.toUTCString()}${newline}`;
  payload += `${'digest: '}${digest}`;
  return payload;
}

function createSignature(privateKey, requestTarget, date, host, digest) {
  if (privateKey === null
    || privateKey.length === 0) {
    throw new Error('Private key is not set up in .env file. Please enter your private key and restart this app.');
  }

  let payload = createPayload(requestTarget, date, host, digest);

  // Sign the signature with sha256 as hashing algorithm
  const signedPayload = crypto.createSign('sha256')
  // Hash the payload built above
    .update(payload)
    .sign({
      // Use your Private key to sign it
      key: privateKey,
      // Uber uses RSA-RSS, so make sure to set padding to such.
      padding: crypto.constants.RSA_PKCS1_PADDING,
    });
    
  // Encode the signed payload to a base64 string
  const basePayload = signedPayload.toString('base64');

  // Start building the signature headers
  let signature = '';
  signature += 'keyId="key-rsa-1",';
  signature += 'algorithm="rsa-sha256",';
  signature += 'headers="(request-target) host date digest",'; 
  // Add the base64 payload to the signature
  signature += `signature="${basePayload}"`;

  return signature;
}


// A quick helper function that parses the signature header into a JS object for utility.
function parseSignatureHeader(header) {
  const dict = {};
  // Splits the signature objects into seperate string arrays
  const items = header.split(',');
  for (let idx = 0; idx < items.length; idx++) {
    let i = 0;
    let nameBuf = '';
    do {
      nameBuf += items[idx][i];
      i++;
    } while (items[idx][i] !== '=');
    i += 2; // skip equals & quote
    let valueBuf = '';
    do {
      valueBuf += items[idx][i];
      i++;
    } while (items[idx][i] !== '"');
    // Assigns the aggregated name to the value. This would convert a signature object that
    // would look like "hash=md5" to dict.hash with the value "md5".
    dict[nameBuf] = valueBuf;
  }
  return dict;
}

// Reconstructs the signature with the current payload, and validates it with the public
// key from the same pair.
/** @param signature the signature you want to validate */
/** @param publicKey the public key used to validate the signature */
/** @param date the date header */
/** @param host the host header */
/** @param digest the digest of the current payload */
/** @returns @type boolean */
function validateSignature(signature, publicKey, requestTarget, date, host, digest) {
  const parsedSignature = parseSignatureHeader(signature);

  let payload = createPayload(requestTarget, date, host, digest);

  const incomingBuffer = Buffer.from(parsedSignature.signature, 'base64');

  return crypto.createVerify('sha256')
    .update(payload)
    .verify({
      key: publicKey,
      padding: crypto.constants.RSA_PKCS1_PADDING,
    }, incomingBuffer);
  }

module.exports = {
  createDigest,
  createSignature,
  validateDigest,
  validateSignature,
};
