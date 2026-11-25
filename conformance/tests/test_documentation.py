import re
import unittest
from urllib.parse import unquote, urlsplit

from conformance.contract import ROOT


class DocumentationTest(unittest.TestCase):
    def test_quickstart_clones_public_upstream(self):
        readme = (ROOT / "README.md").read_text()
        self.assertIn(
            "git clone https://github.com/uber/uber-pay-reference-app.git",
            readme,
        )
        self.assertNotIn(
            "https://github.com/Arka-Uber-Open/uber-pay-reference-app.git",
            readme,
        )

    def test_psp_documentation_links_resolve(self):
        documents = [
            ROOT / "README.md",
            *sorted((ROOT / "docs").glob("*.md")),
            *sorted((ROOT / "implementations").glob("*/README.md")),
            *sorted((ROOT / "contracts").glob("*.md")),
        ]
        for document in documents:
            for target in re.findall(r"\[[^\]]*\]\(([^)\s]+)\)", document.read_text()):
                link = urlsplit(target)
                if link.scheme or link.netloc or not link.path:
                    continue
                with self.subTest(
                    document=str(document.relative_to(ROOT)), link=target
                ):
                    destination = document.parent / unquote(link.path)
                    self.assertTrue(destination.exists(), f"Missing target: {target}")
