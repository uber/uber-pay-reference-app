const Router = require('express').Router();
const axios = require('axios');
const signature = require('../../shared/utils/signature');

const privateKey = JSON.parse(`"${process.env.UBER_PRIV_KEY}"`);
// Address to the merchant app.
const hostUrl = '127.0.0.1:8080';

Router.get('/', (req, res) => res.render('index'));

Router.post('/init-deposit', async (req, res) => {
  // Create digest hash from request body
  const digest = signature.createDigest(req.rawBody);
  const date = new Date();
  const url = new URL(`http://${hostUrl}/api/deposit/init`);

  const newSignature = signature.createSignature(privateKey
    , "post /api/deposit/init", date, url.host, digest);
  if (newSignature == null) {
    return res.status(500)
      .send();
  }

  const response = await axios({
    url: url.toString(),
    method: 'POST',
    data: req.body,
    headers: {
      'Content-Type': 'application/json',
      Date: date.toUTCString(),
      Digest: digest,
      Signature: newSignature,
      'X-Idempotency-Key': 'c900d4dd-7070-4e0b-9323-8f24cfde0490',
    },
  });
  if (response.status >= 200 && response < 300) {
    return res.status(response.status)
      .send('error');
  }
  return res.status(200)
    .set('Location', response.headers.location)
    .send();
});

module.exports = Router;
