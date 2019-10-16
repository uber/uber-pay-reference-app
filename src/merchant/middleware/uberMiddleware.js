const axios = require('axios');

 const uberBaseUrl = process.env.DEVELOPMENT 
        ? "https://sandbox-api.uber.com"
        : "https://api.uber.com";

// TODO (Mike): Reformat this for test tendencies and move these requests to a mock client.
class UberPaymentsClient {
    constructor(oauth) {
        console.log(oauth);
        this.oauthPacket = oauth;
    }

    // getDepositAsync is used to wrap the response from the GET /v1/payments/deposits route.
    async getDepositAsync(id) {
        let response = axios({
            url: "/v1/payments/deposits",
            method: "GET",
            baseUrl: uberBaseUrl,
            headers: {
                "Authorization": "Bearer " + this.oauthPacket.access_token
            }
        });
        return response;
    }

    // createDepositAsync is used to wrap the response and add preliminary headers; it won't return
    // anything on exception.
    async createDepositAsync(deposit) {
        try {
            let response = axios({
                url: uberBaseUrl + "/v1/payments/deposits",
                method: "POST",
                headers: {
                    "Authorization": "Bearer " + this.oauthPacket.access_token,
                    "Content-Type": "application/json",
                    "X-Idempotency-Key": "c900d4dd-7070-4e0b-9323-8f24cfde0490"
                },
                data: JSON.stringify(deposit)
            });
            return response;
        } catch(ex) {
            return null;
        }
    }

    // confirmDepositAsync confirms a pending deposit. Otherwise it fails
    // and rejects the promise.
    async confirmDepositAsync(id) {
        let response = await axios({
            url: uberBaseUrl + `/v1/payments/deposits/c0efc253-66c3-4b6c-b3b3-317fbe093f50/confirm`,
            method: "POST",
            headers: {
                "Authorization": "Bearer " + this.oauthPacket.access_token,
                "Content-Type": "application/json",
                "X-Idempotency-Key": "c0efc253-66c3-4b6c-b3b3-317fbe093f50"
            },
            data: "{}"
        });
        return response;
    }

    // cancelDepositAsync cancels a pending deposit. Otherwise it fails
    // and rejects the promise.
    async cancelDepositAsync(id) {
        try {
            let response = await axios({
                url: `/v1/payments/deposits/c0efc253-66c3-4b6c-b3b3-317fbe093f50/cancel`,
                method: "POST",
                baseUrl: uberBaseUrl,
                headers: {
                    "Authorization": "Bearer " + this.oauthPacket.access_token,
                    "Content-Type": "application/json",
                    "X-Idempotency-Key": "c900d4dd-7070-4e0b-9323-8f24cfde0490"
                },
                data: "{}"
            });
            return response;
        } catch(e) {
            console.log(e);
            return null;
        }
    }
}

module.exports = (packet) => {
    let client = new UberPaymentsClient(packet);
    return (_, res, next) => {
        res.uber = client;
        next();
    };
}