"""Build native gateways and run exactly the same wire suite against each language."""

import argparse
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
import unittest
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path

from .contract import ROOT
from .coverage import report as coverage_report
from .mock_uber import MockUber
from .suite import GatewaySuite
from .wire import Client, eventually, keys


def port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def command(language, build=True):
    if language == "node":
        npm = shutil.which("npm")
        if not npm:
            raise RuntimeError(
                "Node 24 and npm are required; see README or use Docker Compose"
            )
        if build:
            subprocess.run(
                [
                    npm,
                    "--prefix",
                    "implementations/node",
                    "ci",
                    "--registry=https://registry.npmjs.org",
                ],
                cwd=ROOT,
                check=True,
            )
            subprocess.run(
                [npm, "--prefix", "implementations/node", "run", "build"],
                cwd=ROOT,
                check=True,
            )
        return [shutil.which("node"), "implementations/node/dist/main.js"]
    if language == "go":
        binary = ROOT / ".artifacts/bin/uberpay-go"
        binary.parent.mkdir(parents=True, exist_ok=True)
        if build:
            subprocess.run(
                ["go", "build", "-o", str(binary), "./cmd/psp"],
                cwd=ROOT / "implementations/go",
                check=True,
            )
        return [str(binary)]
    if language == "java":
        if build:
            subprocess.run(
                ["implementations/java/mvnw", "-q", "package"], cwd=ROOT, check=True
            )
        return [
            shutil.which("java"),
            "-jar",
            "implementations/java/target/psp-reference-java-2.0.0.jar",
        ]
    raise ValueError(language)


class GatewayProcess:
    def __init__(self, argv, environment, log):
        self.argv, self.environment = argv, environment
        self.log = open(log, "ab", buffering=0)
        self.process = None

    def start(self):
        self.process = subprocess.Popen(
            self.argv,
            cwd=ROOT,
            env=self.environment,
            stdout=self.log,
            stderr=self.log,
            start_new_session=True,
        )

    def stop(self):
        if self.process and self.process.poll() is None:
            os.killpg(self.process.pid, signal.SIGTERM)
            try:
                self.process.wait(timeout=8)
            except subprocess.TimeoutExpired:
                os.killpg(self.process.pid, signal.SIGKILL)
                self.process.wait(timeout=3)

    def restart(self, *, hard=False):
        if hard and self.process and self.process.poll() is None:
            os.killpg(self.process.pid, signal.SIGKILL)
            self.process.wait(timeout=3)
        else:
            self.stop()
        self.start()
        self.wait_ready()

    def wait_ready(self):
        def ready():
            if self.process.poll() is not None:
                raise RuntimeError(
                    f"Gateway exited with {self.process.returncode}; see log"
                )
            return (
                self.client.request("GET", "/health", signed=False, outside=True).status
                == 200
            )

        eventually(ready, timeout=35, description="gateway DB readiness")

    def close(self):
        self.stop()
        self.log.close()


class Report(unittest.TextTestResult):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.cases = []

    def startTest(self, test):
        self.started = time.monotonic()
        super().startTest(test)

    def addSuccess(self, test):
        super().addSuccess(test)
        self.cases.append(
            {
                "name": test.id(),
                "status": "passed",
                "seconds": time.monotonic() - self.started,
            }
        )

    def addFailure(self, test, error):
        super().addFailure(test, error)
        self.cases.append(
            {
                "name": test.id(),
                "status": "failed",
                "seconds": time.monotonic() - self.started,
                "error": self._exc_info_to_string(error, test),
            }
        )

    def addError(self, test, error):
        super().addError(test, error)
        self.cases.append(
            {
                "name": test.id(),
                "status": "error",
                "seconds": time.monotonic() - self.started,
                "error": self._exc_info_to_string(error, test),
            }
        )

    def addSkip(self, test, reason):
        # A missing test is never a passing parity result.
        super().addSkip(test, reason)
        self.cases.append({"name": test.id(), "status": "skipped", "reason": reason})

    def addSubTest(self, test, subtest, error):
        super().addSubTest(test, subtest, error)
        if error:
            self.cases.append(
                {
                    "name": subtest.id(),
                    "status": "failed",
                    "seconds": time.monotonic() - self.started,
                    "error": self._exc_info_to_string(error, test),
                }
            )


def write_report(directory, language, result, events):
    summary = {
        "language": language,
        "tests": result.testsRun,
        "failures": len(result.failures),
        "errors": len(result.errors),
        "skipped": len(result.skipped),
        "cases": result.cases,
        "callbacks": events,
    }
    (directory / f"{language}.json").write_text(json.dumps(summary, indent=2))
    suite = ET.Element(
        "testsuite",
        name=language,
        tests=str(result.testsRun),
        failures=str(len(result.failures)),
        errors=str(len(result.errors)),
    )
    for item in result.cases:
        case = ET.SubElement(
            suite, "testcase", name=item["name"], time=str(item.get("seconds", 0))
        )
        if item["status"] != "passed":
            ET.SubElement(case, "failure", message=item["status"]).text = item.get(
                "error", item.get("reason")
            )
    ET.ElementTree(suite).write(
        directory / f"{language}.xml", encoding="utf-8", xml_declaration=True
    )
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--languages",
        nargs="+",
        choices=["node", "go", "java"],
        default=["node", "java", "go"],
    )
    parser.add_argument(
        "--database-url",
        default=os.getenv(
            "DATABASE_URL", "postgresql://uberpay:uberpay@127.0.0.1:5432/uberpay"
        ),
    )
    parser.add_argument("--skip-build", action="store_true")
    parser.add_argument("--output", default=str(ROOT / ".artifacts/conformance"))
    parser.add_argument(
        "--match",
        help="Run matching scenario names while developing; not a full release gate",
    )
    args = parser.parse_args()
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)
    private = keys(ROOT / ".local")
    summaries = []
    success = True
    for language in args.languages:
        mock = MockUber(port=0).start()
        server_port = port()
        namespace, secret = f"test-{language}-{uuid.uuid4().hex[:12]}", uuid.uuid4().hex
        env = dict(
            os.environ,
            MODE="local",
            HOST="127.0.0.1",
            PORT=str(server_port),
            BASE_PATH="/uberpay/v1",
            DATABASE_URL=args.database_url,
            UBER_PUBLIC_KEY_FILE=str(ROOT / ".local/uber-public.pem"),
            UBER_KEY_ID="demo-uber-key",
            CLIENT_ID=namespace,
            CLIENT_SECRET="demo-secret",
            UBER_CLIENT_ID="demo-uber-client",
            UBER_CLIENT_SECRET="demo-uber-secret",
            UBER_CALLBACK_BASE_URL=mock.url,
            UBER_TOKEN_URL=mock.url + "/oauth/token",
            PUBLIC_BASE_URL=f"http://127.0.0.1:{server_port}/uberpay/v1",
            ENABLE_TEST_CONTROL="true",
            TEST_CONTROL_SECRET=secret,
            ASYNC_DELAY_MS="500",
            ACTION_TTL_MS="600000",
            WORKER_INTERVAL_MS="100",
            CALLBACK_RETRY_MS="100",
            CALLBACK_MAX_ATTEMPTS="5",
            CONTRACT_FILE=str(ROOT / "contracts/runtime.json"),
            MIGRATION_FILE=str(ROOT / "storage/migrations/001_init.sql"),
        )
        process = None
        try:
            argv = command(language, build=not args.skip_build)
            client = Client(env["PUBLIC_BASE_URL"], private, control_secret=secret)
            process = GatewayProcess(argv, env, output / f"{language}.log")
            process.client = client
            process.start()
            process.wait_ready()
            test_class = type(
                f"{language.title()}Conformance",
                (GatewaySuite,),
                {
                    "client": client,
                    "mock": mock,
                    "process": process,
                    "client_id": namespace,
                    "client_secret": "demo-secret",
                    "database_url": args.database_url,
                },
            )
            tests = unittest.defaultTestLoader.loadTestsFromTestCase(test_class)
            if args.match:
                tests = unittest.TestSuite(t for t in tests if args.match in t.id())
            runner = unittest.TextTestRunner(verbosity=2, resultclass=Report)
            result = runner.run(tests)
            summary = write_report(output, language, result, mock.events)
            summary["response_coverage"] = coverage_report(client.observed)
            (output / f"{language}.coverage.json").write_text(
                json.dumps(summary["response_coverage"], indent=2)
            )
            (output / f"{language}.json").write_text(json.dumps(summary, indent=2))
            summaries.append(summary)
            if not args.match and summary["response_coverage"]["missing"]:
                success = False
                print(
                    f"{language}: missing declared status coverage: {summary['response_coverage']['missing']}",
                    file=sys.stderr,
                )
            success &= (
                result.wasSuccessful() and not result.skipped and result.testsRun > 0
            )
        except Exception as error:
            success = False
            summaries.append({"language": language, "startup_error": str(error)})
            print(
                f"{language}: {error}; inspect {output / (language + '.log')}",
                file=sys.stderr,
            )
        finally:
            if process:
                process.close()
            mock.close()
    (output / "summary.json").write_text(json.dumps(summaries, indent=2))
    return 0 if success else 1


if __name__ == "__main__":
    raise SystemExit(main())
