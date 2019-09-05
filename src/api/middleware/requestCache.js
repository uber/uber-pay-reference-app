class MemoryStore {
    constructor() { 
        this._kvStore = {};
    }

    get(id) {
        return this._kvStore[id];
    }

    put(id, val) { 
        this._kvStore[id] = val;
    }
}

const memStore = new MemoryStore();

module.exports = async (req, res, next) => {
    res.cache = memStore;
    await next();
};