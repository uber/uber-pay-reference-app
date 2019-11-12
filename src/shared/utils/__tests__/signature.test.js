const signature = require('../signature');

// Not an actual private key; this key was generated purely for the use
// in this specific test case.
// WARNING: Please do not use in your own projects as that would be
// VERY unsafe.
const mockPrivateKey = `-----BEGIN RSA PRIVATE KEY-----
MIICXAIBAAKBgQCcaq7X8B7/yZJq8kfNysQhT85TfGupcBbgeJChM00Xci4gOw3j
1bm1dp+yTUf80CaLTK/hV7A+0YZ7bgyaKJw6K+Y1rAS+W++tlb8gfxLWNd0EUZMH
fhiTi+yAYx/gpwfu8OFco8Zr27N2Zzy+jqSw+SttXk8Ryq1Fvu3s6pm1fQIDAQAB
AoGAJGd7/AwJ2vR6HGP5LD9V+H7PgYTCtfO9j07gMnRzK9LKQ3wbFnzGtSI/tsrf
x0pWvjMB+EtlXKl9SDrrufFbspy2qynKY0JhMFg4pfyV95Uux9njTVfG5V1B9/X2
wAV3gLcpA8yyEVa0WRE5u5qStZFQ2k9UGkScaklJttgueeECQQDb9pwF9hFkOs7g
h4DAzo2Eva6aB8XpHX4gxbcvwmKWNar3IgAbBBk8qZj/pvgVx/fZFh615GU6siMF
eWEDHNWZAkEAtgrkcgTbPmzOTa3x3ILzdltizARyUfW+fmnZdw7pddDvwt6lkH+f
y8Wkt4MH5/smQHOjJLWAhvChfu5PJkDFhQJBAIRabSNBHtHaeOStZBGft5OptsfD
6ZgNQPljPoikREHx5P9zG/EllDHmMsORqjEg88dSAgScezTIPz5p/K+sx/ECQAm3
Zx4W2ETqtdA7mo80hh4pHorZKejp/vLsX2kVaKybLFDwZVHjHfpYYERTQBbHtvD1
ux14epW5PkVyHtNh3akCQEa5MYTopcBdOTWiMKphzF2nksDTk4xvZxMk8bvl2r52
2t9BBGov+daTyV5OzYUf1IR1h4h/s2dGaYQCQODXgtE=
-----END RSA PRIVATE KEY-----`;

test('create a digest', () => {
  expect(signature.createDigest('test string'))
    .toBe('SHA-256=1VecRt/MfxggcBPmW0Tky04sIpj0rEV7qPgnQ/Mekws=');
});

test('create a signature', () => {
  expect(signature.createSignature(
    mockPrivateKey, new Date(0), 'example.com', 'abcd',
  )).toBe('keyId="key-rsa-1",'
    + 'algorithm="rsa-sha256",'
    + 'headers="(request-target) host date digest",'
    + 'signature="FLDOWadKxHXItaiTOW+oIUPAZr+3ZnzbSfrw4O3keihahmdaJGLMy6HZ2Qy01jkng8Ed/HV1hlMMJDZoy5SE+kspEi35HjQn3PCSs4eFm2U28pSit6afIIuz7VtOTx0KiV67ZXLIk79qqgBPjnzmr00SWeDPxXlpJEoilypzZ6g="');
});
