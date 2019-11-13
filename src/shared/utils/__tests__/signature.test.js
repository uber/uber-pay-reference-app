const signature = require('../signature');

// Not an actual private key; this key was generated purely for the use
// in this specific test case.
// WARNING: Please do not use in your own projects as that would be
// VERY unsafe.
const mockPrivateKey = `-----BEGIN RSA PRIVATE KEY-----
MIIEowIBAAKCAQEAwZJG+scbOi10akXxxn+WU4DtYdC61dQv4obkgy87AW+KZSHV
09EefMh1tz2i8IcJRtave8QFhJlImPAsZRUPDinVOGBrCC1sOJgGw4spJ/mP5Kfa
M0UUK2YZJz+f7asadcnOGbHXPxj4BW7SUeS7CXvCfhUAyX8DiOJIgw0S2PsIYlOX
co5r0S6NYSMke25+r2paFSbouuJlkQo+Aktr+eaZzaGYfTdkTd00AmeNujcu1mkA
FIfeC/yx/bsxjouAMoH0WQWF6Wala+rXVfoqVoYEwY3vSjyw0UwKkEU1wssFReTQ
7en8JNKHNEv26LvNtcf3OX5zV2mm8Bezdh5ekwIDAQABAoIBAD1dSfMmO5Nt5YHr
OoJa4CUIsjhYG2K+KY/+g2KzDlmeKpl06HyFxsx4IBk/CPCHXoF+fZMWyUkH7uPM
/8XrryImUNnWdxfYjn5Cc6J580og62TpcVrfg01eyIqQuPp3S6QbnaK3pizylW3j
e/lKMaEIAthFgEJgaZZzZdllw5vVKrhqKa7ZS9KMOSLz2/pMW7C3obRzq4GZX2i5
4ryCTmG0xOOTAckWGEE6vyYheFLl6cTgSDpDXmsbJNx5jj7t7EpEpgU80+rq7MLv
OkjJRB9t1W9JglNr8cya8LatHUa+0hbMLKqfdUrBWuFHjBJFr4pdBxRgbfjJ/BY/
4Wwqc8ECgYEA/VpfudV3kvm4bkuK8ZaapIKCnOmBBfa2Yu6+tMEee1srzkvD3JDV
QFEGu5Zh7NxlEBJXiwi1XNKUxsTav8ggwZW3XJ04GlRqwugDvhO7qoe8D02GO5+Z
A926/7v7dp7WFSOxRwAZCv8MjL183/wj70Xgz7G/ORUl+X6qc/Eed9cCgYEAw5gC
bdEZrDz+TpuYaL0Y+nIQa7JuyGG70bb62+FllchjYEDOyH2WEr7MBEoavhbq51UM
Ej4NJg+XPEF46ChysIOLZwOg8tSUSUHO4ievAwC+EeDexfDYpQ8Cu44H2YLVlK9h
7NCWSm3ydwfhwL86GfnaH+wzhHiUcvwr/s3lx6UCgYEA3UZMawslB2oxiatS7BPE
dsBCAXfzp1jGDPxF9arhy41iYihMJV0d07Gw9q+uUWhgoDvU1+rANBT7uer+vxe0
fD8dybbK5DwsjpSDiETF2Hf6aUclYPfQdc733xm40+6APjBRShisxqzhsh5hbWNT
o0xdwCiXQezVoOXfS6ohGTsCgYAKSNXn3FTTP6nHjFrpNvg3QwQmBY3Zr3nmvw5V
vXZP9+dxyjaVSDU56xpjJ1pcoGvdmhU2aFjcpXk4S+EFpArTyTbtRb2Sxya8617r
eVa0e7eCzSYplk5t72ofXop/H2gHl2k4wIcGht128M0oC2YMs5yKjSrwTj7QBP5z
OXRZmQKBgC8uttjJ2M+PPYRqjoG77d+PdLrITff4O2DJnJ3EnuNSWxE4fIxAnd5c
rPb5DpcLsqv7pdZnpkMlEb/lhtoJ5lIMZ4Ec+5LPBOSV0UzOJbf9HUSUTT89WAwT
lg0O8wxmM9mVXzOIXgKyMbPcCMSdCcpIbQ3ss0qZ0IcRM4PC2Y+1
-----END RSA PRIVATE KEY-----`;

// Also not an actual public key; this key was generated purely for the use
// in this specific test case.
// WARNING: Please do not use in your own projects as that would be
// VERY unsafe.
const mockPublicKey = `-----BEGIN PUBLIC KEY-----
MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEAwZJG+scbOi10akXxxn+W
U4DtYdC61dQv4obkgy87AW+KZSHV09EefMh1tz2i8IcJRtave8QFhJlImPAsZRUP
DinVOGBrCC1sOJgGw4spJ/mP5KfaM0UUK2YZJz+f7asadcnOGbHXPxj4BW7SUeS7
CXvCfhUAyX8DiOJIgw0S2PsIYlOXco5r0S6NYSMke25+r2paFSbouuJlkQo+Aktr
+eaZzaGYfTdkTd00AmeNujcu1mkAFIfeC/yx/bsxjouAMoH0WQWF6Wala+rXVfoq
VoYEwY3vSjyw0UwKkEU1wssFReTQ7en8JNKHNEv26LvNtcf3OX5zV2mm8Bezdh5e
kwIDAQAB
-----END PUBLIC KEY-----`

test('create a digest', () => {
  expect(signature.createDigest('test string'))
    .toBe('SHA-256=1VecRt/MfxggcBPmW0Tky04sIpj0rEV7qPgnQ/Mekws=');
});

test('validate a digest', () => {
  expect(signature.validateDigest(
    signature.createDigest('test string'), 'test string'))
    .toBe(true)
});

test('create a signature', () => {
  const requestTarget = "POST /example";
  const date = new Date(0);
  const host = 'example.com';
  const digest = 'abcd';

  const sign = signature.createSignature(
    mockPrivateKey, requestTarget, date, host, digest)

  expect(sign).toBe('keyId="key-rsa-1",'
    + 'algorithm="rsa-sha256",'
    + 'headers="(request-target) host date digest",'
    + 'signature="HdAs93/CbDLt61l1DwWEXlqR7G/b3OP37tisH9qLFevbv0c4gx6NHQN2b/lW4on4tRT4pKspvv9c4nHRIFBFY63BBaxrqLiopsQ0adboEh2YzpPZqQTFcQk9XlWv4DtPw+S2YF0uszcoTYRQsOxhWR63ko3EPd9AhcXKTxb6Wk6mvS1tVAdrv1vL8TkCqFxig79idx5IJVLk/vPMGkN8zip74WCW/Y+ahdblkPCCLJiBKxOjYoU/+nyyRhkJ8K6EejeXDAi8Wk1/gnRtZDdGcmXsAuAGDF6Fmt1ePISXxRMq52uXnBTJYVQhgKZd7pQFFe39bVNc+Y1yiobQA9Kc1A==\"');
});

test('validate a signature', () => {
  const requestTarget = "POST /example";
  const date = new Date(0);
  const host = 'example.com';
  const digest = 'abcd';

  const sign = signature.createSignature(
    mockPrivateKey, requestTarget, date, host, digest)
  
  const result = signature.validateSignature(
    sign, mockPublicKey, requestTarget, date, host, digest);

  expect(result).toBe(true);
});