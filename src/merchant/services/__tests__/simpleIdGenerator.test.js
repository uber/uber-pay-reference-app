const SimpleIdGenerator = require('../simpleIdGenerator')

const amount = 8
const generator = new SimpleIdGenerator(amount);

test('Creates a random ID', () => {
    let id = generator.generateId();
    expect(id.length).toBe(amount * 2);
    expect(id.includes('g')).toBe(false);
})