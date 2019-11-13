const express = require('express');
const crypto = require('crypto');
const utils = require('../utils/expressUtils');

const publicKey =  JSON.parse(`"${process.env.UBER_PUB_KEY}"`);

// This is an API router, this means that this should NEVER return a render of one of the pages.
/** @param {UberPaymentsClient} uberClient */
/** @param {SimpleIdGenerator} idGenerator */
/** @param {SignatureHandler} signatureHandler */
class MerchantController {
  constructor(uberClient, idGenerator, signatureHandler) {
    this.uberClient = uberClient;
    this.idGenerator = idGenerator;
    this.signatureHandler = signatureHandler;
  }

  // For more info read the integration guide located here
  // https://developer.uber.com/docs/secured/payments/references/api/v1/init-deposit
  async initDepositAsync(req, res) {
    // Generate a session Id for this deposit.
    const sessionId = this.idGenerator.generateId();
    const host = process.env.DEVELOPMENT
      ? `http://${req.connection.localAddress}:${req.connection.localPort}`
      : `https://${req.hostname}`;
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

    if (!this.signatureHandler.validateDigest(req.get('digest'), req.rawBody)) {
      return utils.badRequest(res, {
        error: 'invalid digest',
      });
    }

    if (!this.signatureHandler.validateSignature(
      req.get('signature'),  
      publicKey, 
      `${req.method.toLowerCase()} ${req.originalUrl}`, 
      req.get('date'), 
      req.get('host'), 
      req.get('digest')  
    )) {
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
      return utils.badRequest(res, ex);
    }

    return res.status(201)
      .set('Location', `${host}/payments/init?sessionId=${sessionId}`)
      .send();
  }

  validateInitiatedAt(time) {
    return true;
  }

  getRouter() {
    return express.Router()
      .post('/init', (req, res) => this.initDepositAsync(req, res));
  }
}

module.exports = MerchantController;