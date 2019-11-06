const axios = require('axios');
const uuid = require('uuid');

const uberBaseUrl = 'https://sandbox-api.uber.com';

class UberPaymentsClient {
  constructor(oauth, clientId) {
    this.oauthPacket = oauth;
    this.clientId = clientId;
  }

  // getDepositAsync is used to wrap the response from the GET /v1/payments/deposits route.
  async getDepositAsync(id) {
    const response = axios({
      url: `${uberBaseUrl}/v1/payments/deposits`,
      method: 'GET',
      headers: {
        Authorization: `Bearer ${this.oauthPacket.access_token}`,
      },
    });
    return response;
  }

  // createDepositAsync is used to wrap the response and add preliminary headers; it won't return
  // anything on exception.
  async createDepositAsync(deposit) {
    const depositId = uuid.v4();

    const response = axios({
      url: `${uberBaseUrl}/v1/payments/deposits`,
      method: 'POST',
      headers: {
        Authorization: `Bearer ${this.oauthPacket.access_token}`,
        'Content-Type': 'application/json',
        'X-Idempotency-Key': `create-${depositId}`,
        'X-Correlation-Id': depositId,
      },
      data: JSON.stringify(deposit),
    });
    return response;
  }

  // confirmDepositAsync confirms a pending deposit. Otherwise it fails
  // and rejects the promise.
  async confirmDepositAsync(id) {
    const response = await axios({
      url: `${uberBaseUrl}/v1/payments/deposits/${id}/confirm`,
      method: 'POST',
      headers: {
        Authorization: `Bearer ${this.oauthPacket.access_token}`,
        'Content-Type': 'application/json',
        'X-Idempotency-Key': `confirm-${id}`,
      },
      data: '{}',
    });
    return response;
  }

  // cancelDepositAsync cancels a pending deposit. Otherwise it fails
  // and rejects the promise.
  // param: id - deposit id
  // param: opts - additional options (cancel_reason)
  async cancelDepositAsync(id, opts = null) {
    const response = await axios({
      url: `${uberBaseUrl}/v1/payments/deposits/${id}/cancel`,
      method: 'POST',
      headers: {
        Authorization: `Bearer ${this.oauthPacket.access_token}`,
        'Content-Type': 'application/json',
        'X-Idempotency-Key': `cancel-${id}`,
      },
      data: opts == null ? '{}' : JSON.stringify(opts),
    });
    return response;
  }
}

module.exports = UberPaymentsClient;
