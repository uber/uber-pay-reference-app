const LoggingMiddleware = require('../loggingMiddleware');

mockWriter = {
    buffer: "",
    write: function(any) {
        this.buffer = any;
    }
}

test('log request to stream', async () => {
    let mockRequest = {
        method: "GET",
        url: "example.com"
    };
    let mockResponse = {
        statusCode: 200
    };
    
    const logger = new LoggingMiddleware(mockWriter);
    await logger.route(mockRequest, mockResponse, () => {
        expect(mockWriter.buffer).toBe(`info : ${mockRequest.method} - ${mockRequest.url}\n`);
    })
    expect(mockWriter.buffer).toBe(`info : ${mockResponse.statusCode}\n`);
})