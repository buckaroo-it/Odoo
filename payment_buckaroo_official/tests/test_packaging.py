# Part of Odoo. See LICENSE file for full copyright and licensing details.

"""Guards the pip packaging declaration for the Buckaroo SDK dependency.

A clean Odoo 19 install has no access to the repo-specific PYTHONPATH mount
used by local Docker development (see the root CLAUDE.md). The addon must
ship a ``requirements.txt`` pinning an installable, importable Buckaroo SDK
release so the manifest's ``external_dependencies`` check can actually be
satisfied without that mount.
"""

import ast
import re
from pathlib import Path

from odoo.tests import BaseCase, tagged

ADDON_ROOT = Path(__file__).resolve().parent.parent


@tagged("post_install", "-at_install")
class TestPackagingRequirements(BaseCase):
    def _read_requirements(self):
        path = ADDON_ROOT / "requirements.txt"
        self.assertTrue(path.exists(), "requirements.txt must ship with the addon")
        return path.read_text()

    def test_requirements_pins_buckaroo_sdk(self):
        """requirements.txt declares an exact-pinned buckaroo-sdk release."""
        content = self._read_requirements()
        match = re.search(r"^buckaroo-sdk==1\.0\.0\s*$", content, re.MULTILINE)
        self.assertIsNotNone(
            match,
            "requirements.txt must pin buckaroo-sdk==1.0.0",
        )

    def test_manifest_declares_buckaroo_external_dependency(self):
        """The manifest's external_dependencies matches the package requirements.txt installs."""
        manifest_path = ADDON_ROOT / "__manifest__.py"
        manifest = ast.literal_eval(manifest_path.read_text())
        python_deps = manifest.get("external_dependencies", {}).get("python", [])
        self.assertIn("buckaroo", python_deps)
