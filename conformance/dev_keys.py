"""Generate local development keys; private key is never included in an image or Git."""

import argparse
from pathlib import Path
from .wire import keys
from cryptography.hazmat.primitives import serialization

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", default=".local")
    parser.add_argument(
        "--public-directory", help="Publish only the public key for gateway mounts"
    )
    args = parser.parse_args()
    key = keys(Path(args.directory))
    if args.public_directory:
        target = Path(args.public_directory)
        target.mkdir(parents=True, exist_ok=True)
        (target / "uber-public.pem").write_bytes(
            key.public_key().public_bytes(
                serialization.Encoding.PEM,
                serialization.PublicFormat.SubjectPublicKeyInfo,
            )
        )
    print("Local development keypair ready (synthetic identity only).")
