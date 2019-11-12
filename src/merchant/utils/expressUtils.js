function badRequest(res, value, errorCode) {
  res.statusText = JSON.stringify(value);
  return res.status(errorCode || 403).send();
}

module.exports = {
  badRequest,
};
