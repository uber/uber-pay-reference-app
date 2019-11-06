class MemoryStore {
  constructor() {
    this.kvStore = {};
  }

  get(id) {
    return this.kvStore[id];
  }

  put(id, val) {
    this.kvStore[id] = val;
  }
}

const memStore = new MemoryStore();

module.exports = async (req, res, next) => {
  res.cache = memStore;
  await next();
};
