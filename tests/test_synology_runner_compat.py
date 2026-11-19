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
            "container-entrypoint.sh",
            "setup-container-runner.sh",
        )
        for name in scripts:
            with self.subTest(script=name):
                shell = "bash" if name == "container-entrypoint.sh" else "sh"
                subprocess.run(
                    [shell, "-n", str(RUNNER_DIR / name)],
                    check=True,
                    capture_output=True,
                    text=True,
                )

    def test_container_runner_image_uses_debian_bookworm_and_pinned_runner(self):
        dockerfile = (ROOT / "Dockerfile.actions-runner").read_text()
        self.assertIn("FROM debian:bookworm-slim", dockerfile)
        self.assertIn("RUNNER_VERSION=2.338.2", dockerfile)
        self.assertIn("sha256sum --check --status", dockerfile)
        self.assertIn("gosu", dockerfile)

    def test_container_runner_entrypoint_reads_registration_token_from_stdin(self):
        entrypoint = (RUNNER_DIR / "container-entrypoint.sh").read_text()
        self.assertIn("read -r RUNNER_TOKEN", entrypoint)
        self.assertIn('--token "$RUNNER_TOKEN"', entrypoint)
        self.assertIn("synology-vlc-lan-container", entrypoint)
        self.assertNotIn("RUNNER_TOKEN:", entrypoint)

    def test_container_runner_compose_has_no_docker_socket_and_read_only_media(self):
        compose = (ROOT / "ctrlvee-vlc-test" / "docker-compose.yml").read_text()
        self.assertIn("network_mode: host", compose)
        self.assertIn("actions-runner-data:/runner", compose)
        self.assertIn(
            "/volume1/docker/ctrlvee-vlc-test/media:/volume1/docker/ctrlvee-vlc-test/media:ro",
            compose,
        )
        self.assertNotIn("/var/run/docker.sock", compose)
        self.assertIn("read_only: true", compose)

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
