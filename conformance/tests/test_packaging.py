import unittest
import yaml
from conformance.contract import ROOT


class PackagingTest(unittest.TestCase):
    def test_compose_has_three_native_profiles(self):
        compose = yaml.safe_load((ROOT / "compose.yaml").read_text())
        for name in ["node", "java", "go"]:
            service = compose["services"][name]
            self.assertEqual(service["profiles"], [name])
            self.assertTrue((ROOT / service["build"]["dockerfile"]).is_file())
            self.assertEqual(service["environment"]["MODE"], "local")
            self.assertEqual(service["environment"]["ENABLE_TEST_CONTROL"], "false")
            self.assertTrue(all(p.startswith("127.0.0.1:") for p in service["ports"]))

    def test_ci_uses_same_matrix_and_scenarios(self):
        ci = yaml.safe_load((ROOT / ".github/workflows/conformance.yml").read_text())
        self.assertCountEqual(
            ci["jobs"]["conformance"]["strategy"]["matrix"]["language"],
            ["node", "java", "go"],
        )
        self.assertTrue((ROOT / "conformance/fixtures/scenarios.json").is_file())

    def test_private_artifacts_excluded_from_images(self):
        ignored = (ROOT / ".dockerignore").read_text().splitlines()
        self.assertTrue({".local", ".env", ".venv", ".artifacts"}.issubset(ignored))

    def test_no_sibling_context_dependency(self):
        self.assertTrue((ROOT / "contracts/uberpay-openapi.json").is_file())
        self.assertTrue((ROOT / "storage/migrations/001_init.sql").is_file())
        self.assertFalse((ROOT / "context").exists())

    def test_ci_smoke_database_has_no_fixed_host_port(self):
        # !reset is the Compose override mechanism, not YAML's normal list merge.
        class ComposeLoader(yaml.SafeLoader):
            pass

        ComposeLoader.add_constructor(
            "!reset", lambda loader, node: loader.construct_sequence(node)
        )
        override = yaml.load(
            (ROOT / "compose.ci.yaml").read_text(), Loader=ComposeLoader
        )
        self.assertEqual(override, {"services": {"db": {"ports": []}}})
        ci = yaml.safe_load((ROOT / ".github/workflows/conformance.yml").read_text())
        steps = {step.get("name"): step for step in ci["jobs"]["conformance"]["steps"]}
        for name in ["Container clone-and-run smoke", "Stop smoke containers"]:
            self.assertEqual(
                steps[name]["env"]["COMPOSE_FILE"], "compose.yaml:compose.ci.yaml"
            )
        self.assertNotIn(
            "55434", (ROOT / ".github/workflows/conformance.yml").read_text()
        )
