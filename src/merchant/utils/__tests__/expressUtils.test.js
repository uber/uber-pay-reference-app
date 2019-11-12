const expressUtils = require('../expressUtils');

test('bad request util', () => {
  const mockResponse = {
    statusCode: 0,
    statusText: '',
    status(s) {
      this.statusCode = s;
      return this;
    },
    send() { return this; },
  };

  const errorCode = 400;
  const value = { error: 'test error' };

  const res = expressUtils.badRequest(mockResponse, value, errorCode);

  expect(res.statusCode).toBe(400);
  expect(res.statusText).toBe(JSON.stringify(value));
});
