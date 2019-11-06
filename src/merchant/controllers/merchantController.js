const express = require('express');
const crypto = require('crypto');
const utils = require('../utils/expressUtils');

// This is an API router, this means that this should NEVER return a render of one of the pages.
class MerchantController {
  constructor(uberClient, idGenerator) {
    this.uberClient = uberClient;
    this.idGenerator = idGenerator;
  }

  // For more info read the integration guide located here
  // https://developer.uber.com/docs/secured/payments/references/api/v1/init-deposit
  async initDeposit(req, res) {
    // Generate a session Id for this deposit.
    const sessionId = this.idGenerator.generateId();

    // Check if the request is valid; else return a bad request.
    if (!req.body.amount) {
      return utils.badRequest(res, {
        error: "required parameter 'amount' undefined.",
      });
    }

    if (Number(req.body.amount.value) <= 0) {
      return utils.badRequest(res, {
        error: "required parameter 'amount.value' invalid or missing.",
      });
    }

    if (!req.body.amount.currency) {
      return utils.badRequest(res, {
        error: "required parameter 'amount.currency' invalid or missing.",
      });
    }

    if (!this.validateDigest(req)) {
      return utils.badRequest(res, {
        error: 'invalid digest',
      });
    }

    if (!this.validateSignature(req)) {
      return utils.badRequest(res, {
        error: 'signature could not be verified.',
      });
    }

    // Make sure this response has not timed out yet.
    if (!this.validateInitiatedAt(req.body.initiated_at)) {
      return res.status(403).send();
    }

    try {
      // Add the merchant metadata.
      req.body.merchant_reference = '599f4164-4417-4361-9d11-1d9d8b4b9096';

      req.body.source = {};
      req.body.source.owner_id = 'a7b46663-3b97-40be-9b3e-ab805a56b269';
      req.body.source.id = 'a148d933-8ed1-4304-8b70-514232386240';

      // These values don't need to go into the createDepositAsync
      delete req.body.return_url;
      delete req.body.initiated_at;

      const x = await this.uberClient.createDepositAsync(req.body);
      if (!x.status.toString().startsWith('2')) {
        throw new Error(`${x.status}: ${x.statusText}`);
      }

      res.cache.put(`session:${sessionId}`, {
        session: sessionId,
        data: req.body,
        res: x.data,
      });
    } catch (ex) {
      console.log(ex);
      return utils.badRequest(res, ex);
    }

    const host = process.env.DEVELOPMENT
      ? `http://${req.connection.localAddress}:${req.connection.localPort}`
      : `https://${req.hostname}`;
    return res.status(201)
      .set('Location', `${host}/payments/init?sessionId=${sessionId}`)
      .send();
  }

  validateDigest(req) {
    // Fetch the digest of the body
    const digest = req.get('digest');

    // Create own digest
    const currentDigest = `SHA-256=${crypto.createHash('sha256')
      .update(req.rawBody)
      .digest('base64')}`;

    // Verify equality of digests.
    return digest === currentDigest;
  }

  // A quick helper function that parses the signature header into a JS object for utility.
  parseSignatureHeader(header) {
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
  validateSignature(req) {
    const publicKey = process.env.UBER_PUB_KEY;
    const signature = this.parseSignatureHeader(req.get('signature'));
    const newline = '\n';

    let payload = '';
    payload += `${'(request-target): '}${req.method.toLowerCase()} ${req.originalUrl}${newline}`;
    payload += `${'host: '}${req.get('host')}${newline}`;
    payload += `${'date: '}${req.get('date')}${newline}`;
    payload += `${'digest: '}${req.get('digest')}`;

    const incomingBuffer = Buffer.from(signature.signature, 'base64');

    return crypto.createVerify('sha256')
      .update(payload)
      .verify({
        key: publicKey,
        padding: crypto.constants.RSA_PKCS1_PADDING,
      }, incomingBuffer);
  }

  validateInitiatedAt(time) {
    return true;
  }

  getRouter() {
    return express.Router()
      .post('/init', (req, res) => this.initDeposit(req, res));
  }
}

module.exports = MerchantController;
