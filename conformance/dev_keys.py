"""Generate local development keys; private key is never included in an image or Git."""

import argparse
from pathlib import Path
from .wire import keys

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", default=".local")
    args = parser.parse_args()
    keys(Path(args.directory))
    print("Local development keypair ready (synthetic identity only).")
