import contextlib
import io
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app


def source_files():
    files = {path: f"contents for {path}".encode() for path in app.UPDATE_FILES}
    files["app.py"] = b"print('updated app')\n"
    files["guard.py"] = b"print('updated guard')\n"
    files["VERSION"] = b"1.2.1\n"
    return files


def source_archive(files=None, root="package-menu-main"):
    files = source_files() if files is None else files
    result = io.BytesIO()
    with tarfile.open(fileobj=result, mode="w:gz") as archive:
        for path, data in files.items():
            member = tarfile.TarInfo(root + "/" + path)
            member.size = len(data)
            archive.addfile(member, io.BytesIO(data))
        extra = tarfile.TarInfo(root + "/README.md")
        extra.size = 0
        archive.addfile(extra, io.BytesIO())
    return result.getvalue()


class UpdaterTests(unittest.TestCase):
    def test_extracts_only_expected_regular_files_from_github_archive(self):
        files, version = app.extract_update_files(source_archive())
        self.assertEqual(version, "1.2.1")
        self.assertEqual(set(files), set(app.UPDATE_FILES))
        self.assertEqual(files["app.py"], source_files()["app.py"])

    def test_invalid_or_incomplete_archive_is_rejected(self):
        files = source_files()
        del files["guard.py"]
        with self.assertRaises(app.Error):
            app.extract_update_files(source_archive(files))

        files = source_files()
        files["VERSION"] = b"not-a-version\n"
        with self.assertRaises(app.Error):
            app.extract_update_files(source_archive(files))
        with self.assertRaises(app.Error):
            app.extract_update_files(source_archive(root="../escaped"))

    def test_package_manager_owned_install_is_not_overwritten(self):
        with patch.object(app, "installed_prefix", return_value=Path("/usr")), patch.object(app, "package_owner", return_value="package-menu-git"), patch.object(app, "github_update_files") as download:
            with self.assertRaisesRegex(app.Error, "managed by package-menu-git"):
                app.update_package_menu()
        download.assert_not_called()

    def test_identical_install_does_not_prompt_or_reinstall(self):
        with tempfile.TemporaryDirectory() as directory:
            prefix = Path(directory)
            files = source_files()
            for source, (target, _) in app.UPDATE_FILES.items():
                destination = prefix / target
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(files[source])
            with patch.object(app, "installed_prefix", return_value=prefix), patch.object(app, "package_owner", return_value=""), patch.object(app, "current_version", return_value="1.2.1"), patch.object(app, "github_update_files", return_value=(files, "1.2.1")), patch.object(app, "confirm") as confirm, patch.object(app, "install_update_files") as install, contextlib.redirect_stdout(io.StringIO()):
                self.assertFalse(app.update_package_menu())
            confirm.assert_not_called()
            install.assert_not_called()

    def test_changed_install_requires_confirmation_before_installing(self):
        with tempfile.TemporaryDirectory() as directory:
            prefix = Path(directory)
            files = source_files()
            with patch.object(app, "installed_prefix", return_value=prefix), patch.object(app, "package_owner", return_value=""), patch.object(app, "current_version", return_value="1.2.1"), patch.object(app, "github_update_files", return_value=(files, "1.2.1")), patch.object(app, "confirm", return_value=True) as confirm, patch.object(app, "install_update_files") as install, contextlib.redirect_stdout(io.StringIO()):
                self.assertTrue(app.update_package_menu())
            confirm.assert_called_once()
            install.assert_called_once_with(prefix, files)

    def test_main_menu_opens_the_updater(self):
        selection = ("", "", [{"id": "app-update"}])
        with patch.object(app, "picker", side_effect=[selection, None]), patch.object(app, "update_package_menu", return_value=False) as update, patch.object(app, "pause") as pause, patch.object(app.os, "execv") as restart:
            app.main()
        update.assert_called_once_with()
        pause.assert_called_once_with("Packages")
        restart.assert_not_called()

    def test_successful_update_restarts_before_using_new_callbacks(self):
        selection = ("", "", [{"id": "app-update"}])
        with patch.object(app, "picker", side_effect=[selection, None]), patch.object(app, "update_package_menu", return_value=True), patch.object(app, "pause"), patch.object(app.os, "execv") as restart, patch.object(sys, "argv", ["package-menu", "--aur-helper", "yay"]):
            app.main()
        restart.assert_called_once_with(sys.executable, [
            sys.executable, "-I", str(app.HERE), "--aur-helper", "yay",
        ])

    def test_cancelled_update_never_writes_files(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(app, "installed_prefix", return_value=Path(directory)), patch.object(app, "package_owner", return_value=""), patch.object(app, "current_version", return_value="1.2.1"), patch.object(app, "github_update_files", return_value=(source_files(), "1.2.1")), patch.object(app, "confirm", return_value=False), patch.object(app, "install_update_files") as install, contextlib.redirect_stdout(io.StringIO()):
            self.assertFalse(app.update_package_menu())
        install.assert_not_called()

    def test_invalid_python_is_rejected_without_running_it(self):
        files = source_files()
        files["app.py"] = b"def invalid syntax\n"
        with self.assertRaisesRegex(app.Error, "invalid Python"):
            app.extract_update_files(source_archive(files))

    def test_python_validation_does_not_execute_downloaded_code(self):
        files = source_files()
        files["app.py"] = b"raise AssertionError('downloaded code must not run during validation')\n"
        extracted, _ = app.extract_update_files(source_archive(files))
        self.assertEqual(extracted["app.py"], files["app.py"])

    def test_unpacked_archive_size_is_bounded_even_for_unused_files(self):
        files = source_files()
        files["unrelated"] = b"x" * 10000
        with patch.object(app, "MAX_UPDATE_ARCHIVE_SIZE", 5000), self.assertRaisesRegex(app.Error, "unpacked size"):
            app.extract_update_files(source_archive(files))

    def test_update_checks_ownership_of_launcher_and_support_files(self):
        with tempfile.TemporaryDirectory() as directory:
            prefix = Path(directory)
            launcher = prefix / "bin/package-menu"
            with patch.object(app, "installed_prefix", return_value=prefix), patch.object(app, "package_owner", side_effect=lambda path: "another-package" if path == launcher else ""), patch.object(app, "github_update_files") as download:
                with self.assertRaisesRegex(app.Error, "managed by another-package"):
                    app.update_package_menu()
            download.assert_not_called()

    def write_install(self, prefix):
        for source, (destination, mode) in app.UPDATE_FILES.items():
            path = prefix / destination
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"old " + source.encode())
            path.chmod(int(mode, 8))

    def assert_install(self, prefix, expected):
        for source, (destination, mode) in app.UPDATE_FILES.items():
            path = prefix / destination
            self.assertEqual(path.read_bytes(), expected[source])
            self.assertEqual(path.stat().st_mode & 0o777, int(mode, 8))
        self.assertFalse(list(prefix.rglob(".*package-menu-update-*")))

    def test_real_update_replaces_only_install_files_and_preserves_modes(self):
        with tempfile.TemporaryDirectory(prefix="package-menu update ; ") as directory:
            prefix = Path(directory)
            self.write_install(prefix)
            config = prefix / "config.json"
            config.write_text("unchanged")
            app.install_update_files(prefix, source_files())
            self.assert_install(prefix, source_files())
            self.assertEqual(config.read_text(), "unchanged")

    def test_failed_or_interrupted_replacement_restores_previous_install(self):
        call = subprocess.call
        for interrupt in (False, True):
            with self.subTest(interrupt=interrupt), tempfile.TemporaryDirectory() as directory:
                prefix = Path(directory)
                self.write_install(prefix)
                old = {source: (prefix / destination).read_bytes()
                       for source, (destination, _) in app.UPDATE_FILES.items()}
                fail_target = str(prefix / app.UPDATE_FILES["app.py"][0])
                failed = False

                def install(command):
                    nonlocal failed
                    if command[0] == "mv" and command[-1] == fail_target and not failed:
                        failed = True
                        if interrupt:
                            raise KeyboardInterrupt
                        return 1
                    self.assertNotIn("sudo", command)
                    return call(command)

                expected = KeyboardInterrupt if interrupt else app.Error
                with patch.object(app.subprocess, "call", side_effect=install), self.assertRaises(expected):
                    app.install_update_files(prefix, source_files())
                self.assert_install(prefix, old)

    def test_pkgbuild_tracks_main_and_uses_project_install_target(self):
        build = (Path(__file__).resolve().parents[1] / "PKGBUILD").read_text()
        self.assertIn("branch=main", build)
        self.assertIn("make DESTDIR=\"$pkgdir\" PREFIX=/usr install", build)

    def test_updater_refuses_symlinked_install_directories_before_writing(self):
        with tempfile.TemporaryDirectory() as directory:
            prefix = Path(directory)
            (prefix / "share").mkdir()
            (prefix / "outside").mkdir()
            (prefix / "share/package-menu").symlink_to(prefix / "outside")
            with patch.object(app.subprocess, "call") as install, self.assertRaisesRegex(app.Error, "symlink"):
                app.install_update_files(prefix, source_files())
        install.assert_not_called()


if __name__ == "__main__":
    unittest.main()
