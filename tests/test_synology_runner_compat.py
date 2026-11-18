import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RUNNER_DIR = ROOT / "ctrlvee-vlc-test" / "runner"


class SynologyRunnerCompatibilityTests(unittest.TestCase):
    def test_runner_shell_scripts_have_valid_syntax(self):
        scripts = (
            "install-runner.sh",
            "diagnose-runner.sh",
            "ldd-compat.sh",
            "ldconfig-compat.sh",
        )
        for name in scripts:
            with self.subTest(script=name):
                subprocess.run(
                    ["sh", "-n", str(RUNNER_DIR / name)],
                    check=True,
                    capture_output=True,
                    text=True,
                )

    def test_ldd_compatibility_helper_reports_version(self):
        result = subprocess.run(
            ["sh", str(RUNNER_DIR / "ldd-compat.sh"), "--version"],
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertIn("ldd", result.stdout.lower())

    def test_ldconfig_compatibility_helper_discovers_icu_in_supplied_path(self):
        with tempfile.TemporaryDirectory() as directory:
            library = Path(directory) / "libicuuc.so.76"
            library.touch()

            result = subprocess.run(
                ["sh", str(RUNNER_DIR / "ldconfig-compat.sh"), "-NXv", directory],
                check=True,
                capture_output=True,
                text=True,
            )

            self.assertIn("libicuuc.so.76", result.stdout)
            self.assertIn(str(library), result.stdout)

    def test_ldconfig_compatibility_helper_rejects_mutating_or_unknown_options(self):
        result = subprocess.run(
            ["sh", str(RUNNER_DIR / "ldconfig-compat.sh"), "--invalid-option"],
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("unsupported option", result.stderr)


if __name__ == "__main__":
    unittest.main()
