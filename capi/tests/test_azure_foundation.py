from __future__ import annotations

import io
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from scripts.lib.azure.common import load_azure_configuration, names
from scripts.lib.azure.foundation import preflight
from tests.azure_fixtures import DEFAULTS, SUBSCRIPTION


def completed(stdout: str = "", returncode: int = 0):
    return subprocess.CompletedProcess([], returncode, stdout=stdout, stderr="")


class AzureFoundationTests(unittest.TestCase):
    def make_root(self) -> Path:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        (root / "config" / "azure").mkdir(parents=True)
        (root / "config" / "azure" / "defaults.env").write_text(
            DEFAULTS,
            encoding="utf-8",
        )
        local = root / "config" / "azure.local.env"
        local.write_text(
            f"AZURE_SUBSCRIPTION_ID={SUBSCRIPTION}\n"
            "AZURE_LOCATION=westus2\n"
            "AZURE_PREFIX=yy-cv\n",
            encoding="utf-8",
        )
        local.chmod(0o600)
        return root

    def test_configuration_and_names_preserve_foundation_contract(self):
        config = load_azure_configuration(self.make_root())
        self.assertEqual(config["AZURE_SUBSCRIPTION_ID"], SUBSCRIPTION)
        self.assertEqual(names(config)["aks"], "yy-cv-mgmt")

    def test_preflight_output_omits_subscription_identifier(self):
        root = self.make_root()
        config = load_azure_configuration(root)
        with (
            patch(
                "scripts.lib.azure.foundation._active_subscription",
                return_value={"id": SUBSCRIPTION, "state": "Enabled"},
            ),
            patch(
                "scripts.lib.azure.foundation._az",
                return_value=completed("Registered\n"),
            ),
            patch("scripts.lib.azure.foundation._sku_available"),
            patch("scripts.lib.azure.foundation._reference_image_available"),
            patch("scripts.lib.azure.foundation.run", return_value=completed()),
            redirect_stdout(io.StringIO()) as output,
        ):
            result = preflight(root, config)
        self.assertEqual(result["subscriptionId"], SUBSCRIPTION)
        self.assertNotIn(SUBSCRIPTION, output.getvalue())


if __name__ == "__main__":
    unittest.main()
