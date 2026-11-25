import hashlib
import io
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
import unittest
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from conformance.contract import ROOT


class MavenBootstrapTest(unittest.TestCase):
    def bootstrap(self, *, corrupt_checksum=False, permanent_status=None):
        real_curl = shutil.which("curl")
        self.assertIsNotNone(real_curl, "The Maven launcher requires curl")
        counts = Counter()
        archive = io.BytesIO()
        with tarfile.open(fileobj=archive, mode="w:gz") as tar:
            content = b"#!/bin/sh\nprintf 'VERIFIED_MAVEN_EXECUTED\\n'\n"
            member = tarfile.TarInfo("apache-maven-3.9.11/bin/mvn")
            member.size, member.mode = len(content), 0o755
            tar.addfile(member, io.BytesIO(content))
        distribution = archive.getvalue()
        checksum = (
            "0" * 128 if corrupt_checksum else hashlib.sha512(distribution).hexdigest()
        ).encode()

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_GET(self):
                counts[self.path] += 1
                status = permanent_status or (429 if counts[self.path] == 1 else 200)
                body = checksum if self.path.endswith(".sha512") else distribution
                if status != 200:
                    body = b"synthetic download failure"
                self.send_response(status)
                self.send_header("Content-Length", str(len(body)))
                if status == 429:
                    self.send_header("Retry-After", "0")
                self.end_headers()
                self.wfile.write(body)

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory(
                prefix="uberpay-maven-bootstrap-"
            ) as temporary:
                root = Path(temporary)
                java, binaries = root / "java", root / "bin"
                (java / ".mvn").mkdir(parents=True)
                binaries.mkdir()
                shutil.copy2(ROOT / "implementations/java/mvnw", java / "mvnw")
                # Test-only transport redirection; production URLs are unchanged.
                proxy = binaries / "curl"
                proxy.write_text(
                    f"#!{sys.executable}\n"
                    "import os, sys\n"
                    "args = [os.environ['FIXTURE_URL'] + '/' + arg.rsplit('/', 1)[-1] "
                    "if arg.startswith('https://repo.maven.apache.org/') else arg for arg in sys.argv[1:]]\n"
                    "os.execv(os.environ['REAL_CURL'], [os.environ['REAL_CURL']] + args)\n"
                )
                proxy.chmod(0o755)
                env = dict(
                    os.environ,
                    PATH=str(binaries) + os.pathsep + os.environ["PATH"],
                    REAL_CURL=real_curl,
                    FIXTURE_URL=f"http://127.0.0.1:{server.server_port}",
                )
                env.pop("MAVEN_HOME", None)
                result = subprocess.run(
                    ["sh", str(java / "mvnw"), "--version"],
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=25,
                )
                installed = (java / ".mvn/apache-maven-3.9.11/bin/mvn").exists()
                self.assertFalse(list((java / ".mvn").glob("bootstrap.*")))
                if result.returncode == 0:
                    before = dict(counts)
                    cached = subprocess.run(
                        ["sh", str(java / "mvnw"), "--version"],
                        env=env,
                        capture_output=True,
                        text=True,
                        timeout=5,
                    )
                    self.assertEqual(cached.returncode, 0, cached.stderr)
                    self.assertEqual(
                        dict(counts), before, "Cached Maven must not download again"
                    )
                return result, installed, dict(counts)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)

    def test_rate_limited_archive_and_checksum_retry_then_use_verified_cache(self):
        result, installed, counts = self.bootstrap()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(installed)
        self.assertEqual(result.stdout.strip(), "VERIFIED_MAVEN_EXECUTED")
        self.assertEqual(sorted(counts.values()), [2, 2])

    def test_checksum_mismatch_never_installs_or_executes_distribution(self):
        result, installed, _ = self.bootstrap(corrupt_checksum=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(installed)
        self.assertIn("Maven checksum mismatch", result.stderr)
        self.assertNotIn("VERIFIED_MAVEN_EXECUTED", result.stdout)

    def test_permanent_http_failure_fails_closed_without_fetching_checksum(self):
        result, installed, counts = self.bootstrap(permanent_status=404)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(installed)
        self.assertEqual(len(counts), 1)
        self.assertEqual(list(counts.values()), [1])
