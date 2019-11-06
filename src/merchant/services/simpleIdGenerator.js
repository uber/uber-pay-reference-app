const crypto = require('crypto');

class SimpleIdGenerator {
  constructor(idLength = 16) {
    this.idLength = idLength;
  }

  generateId() {
    return crypto.randomBytes(this.idLength)
      .toString('hex');
  }
}

module.exports = SimpleIdGenerator;
