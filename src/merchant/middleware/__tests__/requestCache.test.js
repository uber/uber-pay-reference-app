const requestCache = require('../requestCache');



test('check if the request cache is added.', () => {
    let res = {};
    requestCache({}, res, ()=>{});
    expect(res.cache != null).toBe(true)
})