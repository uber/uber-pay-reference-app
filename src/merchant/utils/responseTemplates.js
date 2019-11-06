
module.exports = {
  // A default error template that can be used to debug.
  // @param message: the error message for context
  ErrorJSON(message) {
    return JSON.stringify({
      success: false,
      data: {
        message,
      },
    });
  },
  // Returns a default OK response template.
  // @param payload: any data that should be returned with the route.
  OkJSON(payload = null) {
    return JSON.stringify({
      success: true,
      data: payload,
    });
  },
};
