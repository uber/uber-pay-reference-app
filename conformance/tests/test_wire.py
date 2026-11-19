import tempfile
import unittest
from pathlib import Path
from conformance.wire import encode, keys
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding


class WireTest(unittest.TestCase):
    def test_exact_json_integer(self):
        self.assertIn(b"9007199254740993", encode({"value": 9007199254740993}))

    def test_generated_keys_not_baked_in(self):
        with tempfile.TemporaryDirectory() as directory:
            key = keys(Path(directory))
            message = b"synthetic-test"
            key.public_key().verify(
                key.sign(message, padding.PKCS1v15(), hashes.SHA256()),
                message,
                padding.PKCS1v15(),
                hashes.SHA256(),
            )
            self.assertEqual(
                (Path(directory) / "uber-private.pem").stat().st_mode & 0o777, 0o600
            )
            self.assertEqual(
                key.private_numbers(), keys(Path(directory)).private_numbers()
            )
