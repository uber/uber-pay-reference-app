const MerchantController = require('../merchantController');

test("run controller", async () => {
    const mockUberPaymentsClient = {
        createDepositAsync: function() { 
            return {
                status: 200, 
                data: {},
            } 
        }
    };
    const mockIdGenerator = {
        generateId: () => "0", 
    }
    const mockSignature =  {
        validateDigest: (digest, body) => true,
        validateSignature: (signature, publicKey, requestTarget, date, host, digest) => true,
    };
    const controller = new MerchantController(
        mockUberPaymentsClient, mockIdGenerator, mockSignature);

    let mockRequest = {
        hostname: "example.com",
        body: {
            amount: {
                value: 1,
                currency: "EUR"
            },   
        },
        rawBody: "abcd",
        get: function(str) {
            return "";
        }
    };

    let mockResponse = {
        headers: {},
        statusCode: 200,
        status: function(s){
            this.statusCode = s;
            return this;
        },
        send: function() {
            return this;
        },
        set: function(name, value) {
            this.headers[name] = value;
            return this;
        },
        cache: {
            put: function(str) {}
        }
    };

    const res = await controller.initDepositAsync(mockRequest, mockResponse);

    // Verify that the request got cached.
    expect(Object.values(mockResponse.cache).length).toBe(1);

    // Assert proper response code.
    expect(res.headers["Location"]).toBe("https://example.com/payments/init?sessionId=0");
    expect(res.statusCode).toBe(201);
})