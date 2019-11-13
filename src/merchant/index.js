const path = require('path');

const express = require('express');
const axios = require('axios');
require('dotenv').config();

const jsonMiddleware = require('../shared/middleware/jsonBodyMiddleware');
const memCacheMiddleware = require('./middleware/requestCache');
const loggingMiddleware = require('./middleware/loggingMiddleware');

const IdGenerator = require('./services/simpleIdGenerator');
const UberPaymentsClient = require('./services/uberPaymentsClient');
const signature = require('../shared/utils/signature')

const PaymentsController = require('./controllers/paymentsController');
const MerchantController = require('./controllers/merchantController');


const app = express();
const port = 8080;

const scope = 'payments.deposits';
const grantType = 'client_credentials';

process.env.UBER_PUB_KEY = JSON.parse(`"${process.env.UBER_PUB_KEY}"`);

const clientId = process.env.UBER_CLIENT_ID;
const secretId = process.env.UBER_SECRET_ID;

function formUrlEncoded(x) {
  return Object.keys(x)
    .reduce((p, c) => `${p}&${c}=${encodeURIComponent(x[c])}`, '');
}

async function getUberAccessKey() {
  const loginRequest = await axios({
    url: 'https://login.uber.com/oauth/v2/token',
    method: 'POST',
    headers: {
      'Content-Type': 'application/x-www-form-urlencoded',
    },
    data: formUrlEncoded({
      client_id: clientId,
      client_secret: secretId,
      scope,
      grant_type: grantType,
    }),
  });
  return loginRequest.data;
}

async function main() {
  const login = await getUberAccessKey();

  // -- Middleware
  app.use('/resources', express.static(path.join(__dirname, '../shared/public')));

  const log = new loggingMiddleware(process.stdout);

  app.use(jsonMiddleware);
  app.use((req, res, next) => log.route(req, res, next));
  app.use(memCacheMiddleware);

  app.use((_, res, next) => {
    res.set('Access-Control-Allow-Origin', '*');
    res.set('Access-Control-Allow-Methods', 'POST');
    res.set('Access-Control-Allow-Headers', 'Content-Type, Digest, Signature, X-Idempotency-Key');
    res.set('Access-Control-Expose-Headers', 'Location');
    next();
  });

  app.set('view engine', 'pug');
  app.set('views', path.join(__dirname, '../shared/views'));

  // -- Services
  const uberPaymentsClient = new UberPaymentsClient(login, clientId);
  const generator = new IdGenerator();

  // -- Routers
  const merchantController = new MerchantController(uberPaymentsClient, generator, signature);
  app.use('/api/deposit', merchantController.getRouter());

  const paymentsController = new PaymentsController(uberPaymentsClient);
  app.use('/payments', paymentsController.getRouter());

  app.listen(port, '0.0.0.0', () => {
    console.log(`Uber payments deposit mock api listening on port ${port}!`);
  });
}

main();
