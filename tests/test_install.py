from pathlib import Path
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


class InstallTests(unittest.TestCase):
    def test_install_is_standalone_and_handles_paths_with_spaces(self):
        with tempfile.TemporaryDirectory(prefix="package-menu test ") as directory:
            prefix = Path(directory) / "prefix with spaces;literal"
            subprocess.run(["make", "install", f"PREFIX={prefix}"], cwd=ROOT,
                           check=True, capture_output=True, text=True)
            command = prefix / "bin/package-menu"
            result = subprocess.run([str(command), "--version"], check=True,
                                    capture_output=True, text=True)
            self.assertEqual(result.stdout.strip(), "package-menu " + (ROOT / "VERSION").read_text().strip())
            result = subprocess.run([str(command), "--help"], check=True,
                                    capture_output=True, text=True)
            self.assertIn("--aur-helper", result.stdout)
            self.assertNotIn("Noctalia", result.stdout)
            self.assertTrue((prefix / "share/licenses/package-menu/LICENSE-OMARCHY").is_file())
            wrapper = command.read_text()
            self.assertNotIn("plugins/materialized", wrapper)

    def test_destdir_staging_does_not_embed_the_build_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            subprocess.run(["make", "install", f"DESTDIR={directory}", "PREFIX=/usr"],
                           cwd=ROOT, check=True, capture_output=True, text=True)
            wrapper = Path(directory, "usr/bin/package-menu")
            self.assertTrue(wrapper.is_file())
            self.assertNotIn(directory, wrapper.read_text())
            result = subprocess.run([str(wrapper), "--version"], check=True,
                                    capture_output=True, text=True)
            self.assertIn("package-menu", result.stdout)


if __name__ == "__main__":
    unittest.main()
