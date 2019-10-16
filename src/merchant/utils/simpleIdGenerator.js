const crypto = require('crypto');

module.exports = {
    generateId: function() {
        return crypto.randomBytes(16)
            .toString("hex");
    }
}