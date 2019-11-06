// eslint-disable-next-line import/no-unresolved
const bodyParser = require('body-parser');

module.exports = bodyParser.json({
  // Gives access to raw request body
  // eslint-disable-next-line no-param-reassign
  verify: (r, s, b) => { r.rawBody = b; },
});
