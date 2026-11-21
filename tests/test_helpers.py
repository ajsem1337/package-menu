import contextlib
import io
import json
import os
import re
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import call, patch
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app


def selected(key):
    return "", "", [{"id": key}]


def package(kind="aur"):
    return {"id": "aur/example", "name": "example", "source": "aur", "version": "1", "kind": kind}


class HelperSettingsTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        for key, value in (("AUR_HELPER", "paru"), ("HELPER_STYLE", "pacman"),
                           ("HELPER_INSTALL_ARGS", ["-S", "--needed", "--aur", "--"]),
                           ("HELPER_UPDATE_ARGS", ["-Syu"]),
                           ("TRAUR_ENABLED", False), ("WRAPPER_ENABLED", False),
                           ("PREVIEW_MODE", "details"),
                           ("LAST_SKIPPED", []),
                           ("CONFIG_FILE", self.directory / "settings/config.json")):
            mocked = patch.object(app, key, value)
            mocked.start()
            self.addCleanup(mocked.stop)

    def test_missing_helpers_use_pacman_without_errors_or_creating_settings(self):
        with patch.object(app.shutil, "which", return_value=None), patch.object(app, "picker", side_effect=[selected("update"), None]), patch.object(app.subprocess, "call", return_value=0) as transaction, patch.object(app, "pause") as pause, contextlib.redirect_stdout(io.StringIO()) as output:
            app.run([])
        transaction.assert_called_once_with(["sudo", "pacman", "-Syu"])
        self.assertEqual(app.AUR_HELPER, "")
        pause.assert_not_called()
        self.assertNotIn("STOP", output.getvalue())
        self.assertFalse(app.CONFIG_FILE.exists())

    def test_auto_uses_first_available_known_helper(self):
        for available in ("paru", "yay", "aura"):
            with self.subTest(available=available), patch.object(app.shutil, "which", side_effect=lambda name: "/bin/" + name if name == available else None):
                settings = app.read_settings()
                settings.update(helper_enabled=True, helper="auto")
                app.activate_settings(settings)
            self.assertEqual(app.AUR_HELPER, available)
            self.assertEqual(app.HELPER_STYLE, "aura" if available == "aura" else "pacman")

    def test_pacman_selection_disables_auto_even_when_helpers_are_installed(self):
        settings = app.read_settings()
        settings["helper"] = "pacman"
        with patch.object(app.shutil, "which") as which:
            app.activate_settings(settings)
        which.assert_not_called()
        self.assertEqual(app.AUR_HELPER, "")

    def test_missing_explicit_helper_does_not_switch_to_another_helper(self):
        with patch.object(app.shutil, "which", return_value=None) as which, patch.object(app, "main") as main:
            app.run(["--aur-helper", "missing-helper"])
        which.assert_called_once_with("missing-helper")
        main.assert_called_once_with()
        self.assertEqual(app.AUR_HELPER, "")

    def test_pacman_install_view_does_not_request_aur_or_run_helper_callbacks(self):
        app.configure_helper("pacman")
        with patch.object(app, "catalog", return_value=[]) as catalog, patch.object(app, "picker", return_value=None) as picker, contextlib.redirect_stdout(io.StringIO()):
            app.browse("install")
        catalog.assert_called_once_with("repos", "")
        self.assertFalse(picker.call_args.kwargs["merged"])
        self.assertIn("repositories", picker.call_args.args[1])
        with patch.object(app, "capture") as capture, patch.object(app, "aur_search") as search:
            self.assertEqual(app.catalog("aur", "example"), [])
        capture.assert_not_called()
        search.assert_not_called()

    def test_pacman_installed_catalog_does_not_need_aur_metadata(self):
        app.configure_helper("pacman")
        with patch.object(app, "capture", side_effect=["Name : foreign\nVersion : 1\n", ""]), patch.object(app, "aur_info") as info:
            records = app.installed_catalog()
        info.assert_not_called()
        self.assertEqual(records[0]["name"], "foreign")

    def test_pacman_mode_guards_stale_aur_selections_before_any_transaction(self):
        app.configure_helper("pacman")
        with patch.object(app.subprocess, "call") as transaction, self.assertRaises(app.Error):
            app.perform("install", [package()])
        transaction.assert_not_called()
        self.assertEqual(app.transaction_command("install", [{**package("repo"), "source": "extra"}]),
                         ["sudo", "pacman", "-S", "--needed", "--", "extra/example"])

    def test_settings_save_presets_and_reload_them_on_next_launch(self):
        for helper in ("paru", "yay", "aura", "pacman", "auto"):
            with self.subTest(helper=helper), patch.object(app, "picker", return_value=selected(helper)), patch.object(app.shutil, "which", side_effect=lambda name: "/bin/" + name):
                app.helper_menu()
                self.assertEqual(app.read_settings()["helper"], helper)
                app.configure_helper("pacman")
                with patch.object(app, "main"):
                    app.run([])
            self.assertEqual(app.AUR_HELPER, "" if helper == "pacman" else "paru" if helper == "auto" else helper)
        self.assertEqual(app.CONFIG_FILE.stat().st_mode & 0o777, 0o600)

    def test_missing_saved_helper_activates_when_it_becomes_available(self):
        with patch.object(app, "picker", return_value=selected("yay")), patch.object(app.shutil, "which", return_value=None):
            app.helper_menu()
        self.assertEqual(app.AUR_HELPER, "")
        self.assertEqual(app.read_settings()["helper"], "yay")
        with patch.object(app.shutil, "which", return_value="/bin/yay"), patch.object(app, "main"):
            app.run([])
        self.assertEqual(app.AUR_HELPER, "yay")

    def test_custom_helper_supports_custom_arguments_without_shell_execution(self):
        helper = "/opt/helper with spaces;literal/app"
        with patch.object(app, "picker", side_effect=[selected("custom"), selected("custom")]), patch("builtins.input", side_effect=[helper, 'install --channel "release x" --', "upgrade --all"]), patch.object(app.shutil, "which", return_value=helper):
            app.helper_menu()
        self.assertEqual(app.transaction_command("install", [package()]),
                         [helper, "install", "--channel", "release x", "--", "example"])
        self.assertEqual(app.update_commands(), [[helper, "upgrade", "--all"]])
        with patch.object(app, "aur_search", return_value=[]) as search, patch.object(app, "capture") as capture:
            app.catalog("aur", "example")
        search.assert_called_once_with("example", set())
        capture.assert_not_called()

    def test_custom_helper_can_select_aura_syntax(self):
        with patch.object(app, "picker", side_effect=[selected("custom"), selected("aura")]), patch("builtins.input", return_value="/opt/custom-aura"), patch.object(app.shutil, "which", return_value="/opt/custom-aura"):
            app.helper_menu()
        self.assertEqual(app.transaction_command("install", [package()]), ["/opt/custom-aura", "-A", "--", "example"])

    def test_aura_uses_native_install_and_two_stage_update_commands(self):
        app.configure_helper("aura")
        self.assertEqual(app.transaction_command("install", [package()]), ["aura", "-A", "--", "example"])
        self.assertEqual(app.update_commands(), [["aura", "-Syu"], ["aura", "-Au"]])
        with patch.object(app, "aur_search", return_value=[]) as search, patch.object(app, "capture") as capture:
            app.catalog("aur", "example")
        search.assert_called_once_with("example", set())
        capture.assert_not_called()

    def test_aura_update_stops_at_first_error_without_retry_or_fallback(self):
        app.configure_helper("aura")
        with patch.object(app, "picker", side_effect=[selected("update"), None]), patch.object(app.subprocess, "call", return_value=7) as transaction, patch.object(app, "pause") as pause, contextlib.redirect_stdout(io.StringIO()):
            app.main()
        transaction.assert_called_once_with(["aura", "-Syu"])
        pause.assert_called_once_with()

    def test_aura_success_runs_both_stages_without_extra_confirmation_or_enter(self):
        app.configure_helper("aura")
        with patch.object(app, "aur_update_names", return_value=["example"]), patch.object(app, "picker", side_effect=[selected("update"), None]), patch.object(app.subprocess, "call", return_value=0) as transaction, patch.object(app, "confirm", side_effect=AssertionError("no extra prompt")), patch.object(app, "pause") as pause:
            app.main()
        self.assertEqual([item.args[0] for item in transaction.call_args_list], [["aura", "-Syu"], ["aura", "-A", "--", "example"]])
        pause.assert_not_called()

    def test_helper_failure_is_not_retried_with_pacman(self):
        app.configure_helper("yay")
        with patch.object(app.subprocess, "call", return_value=7) as transaction, contextlib.redirect_stdout(io.StringIO()), self.assertRaises(app.Error):
            app.perform("install", [package()])
        transaction.assert_called_once()
        self.assertEqual(transaction.call_args.args[0], ["yay", "-S", "--needed", "--aur", "--", "example"])

    def test_settings_can_be_opened_from_main_and_cli(self):
        with patch.object(app, "picker", side_effect=[selected("settings"), None]), patch.object(app, "settings_menu") as settings:
            app.main()
        settings.assert_called_once_with()
        with patch.object(app, "settings_menu") as settings, patch.object(app.shutil, "which", return_value=None):
            app.run(["--settings"])
        settings.assert_called_once_with()

    def test_settings_and_helper_menus_have_a_panel_without_status(self):
        def run(arguments, **kwargs):
            self.assertIn("--header=", arguments)
            self.assertIn("--preview-window=right,48%,wrap", arguments)
            self.assertIn("--bind=shift-up:preview-up,shift-down:preview-down", arguments)
            preview = next(arg for arg in arguments if arg.startswith("--preview="))
            command = shlex.split(preview.removeprefix("--preview="))
            database = Path(command[command.index("--preview") + 1])
            panel = json.loads((database.parent / "panel.json").read_text())
            self.assertFalse(panel["multi"])
            self.assertFalse(panel["global_shortcuts"])
            self.assertNotIn("notes", panel)
            with contextlib.redirect_stdout(io.StringIO()) as output:
                app.preview(database, next(iter(json.loads(database.read_text()))), show_shortcuts=True)
            plain = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", output.getvalue())
            self.assertRegex(plain, r"Alt\+P\s+Show or hide panel")
            self.assertNotIn("Ctrl+A", plain)
            self.assertNotIn("Status", plain.splitlines())
            return subprocess.CompletedProcess(arguments, 130, "")

        with patch.object(app.subprocess, "run", side_effect=run):
            app.settings_menu()
            app.helper_menu()

    def test_cancelled_settings_do_not_write_files_or_change_the_helper(self):
        with patch.object(app, "picker", return_value=None):
            app.helper_menu()
        self.assertFalse(app.CONFIG_FILE.exists())
        self.assertEqual(app.AUR_HELPER, "paru")
        with patch.object(app, "picker", return_value=selected("custom")), patch("builtins.input", return_value=""):
            app.helper_menu()
        self.assertFalse(app.CONFIG_FILE.exists())

    def test_invalid_custom_arguments_do_not_save_partial_settings(self):
        with patch.object(app, "picker", side_effect=[selected("custom"), selected("custom")]), patch("builtins.input", side_effect=["helper", "'unterminated"]), self.assertRaises(app.Error):
            app.helper_menu()
        self.assertFalse(app.CONFIG_FILE.exists())
        self.assertEqual(app.AUR_HELPER, "paru")

    def test_corrupt_settings_are_errors_not_permission_to_run_another_command(self):
        app.CONFIG_FILE.parent.mkdir()
        for value in ("[]", "{bad", '{"helper": "--noconfirm"}', '{"install_args": "not-a-list"}', '{"style": "unknown"}'):
            app.CONFIG_FILE.write_text(value)
            with self.subTest(value=value), self.assertRaises(app.Error):
                app.read_settings()

    def test_cli_override_is_temporary_and_does_not_overwrite_saved_settings(self):
        settings = app.read_settings()
        settings["helper"] = "yay"
        app.save_settings(settings)
        with patch.object(app.shutil, "which", return_value="/bin/paru"), patch.object(app, "main"):
            app.run(["--aur-helper", "paru"])
        self.assertEqual(app.AUR_HELPER, "paru")
        self.assertEqual(app.read_settings()["helper"], "yay")

    def test_callbacks_preserve_custom_settings_and_native_mode(self):
        for helper, style in (("/opt/a helper/app", "custom"), ("aura", "aura"), ("pacman", "auto")):
            with self.subTest(helper=helper):
                app.configure_helper(helper, style, ["install", "a b;$(literal)"], ["upgrade"])
                command = app.frontend_command()
                self.assertEqual(shlex.split(shlex.join(command)), command)
                with patch.object(app.shutil, "which", return_value=helper), patch.object(app, "main"):
                    app.run(command[3:])
                self.assertEqual(app.AUR_HELPER, "" if helper == "pacman" else helper)
                if style == "custom":
                    self.assertEqual(app.HELPER_INSTALL_ARGS, ["install", "a b;$(literal)"])
                    self.assertEqual(app.HELPER_UPDATE_ARGS, ["upgrade"])

    def test_default_is_pacman_even_with_an_installed_helper(self):
        with patch.object(app.shutil, "which") as which:
            app.activate_settings(app.read_settings())
        which.assert_not_called()
        self.assertEqual(app.AUR_HELPER, "")
        self.assertFalse(app.TRAUR_ENABLED)
        self.assertFalse(app.WRAPPER_ENABLED)

    def test_settings_order_and_helper_fields_follow_the_toggle(self):
        settings = app.read_settings()
        self.assertEqual([record["id"] for record in app.settings_records(settings)],
                         ["helper_enabled", "traur_enabled", "wrapper_enabled", "vt_key", "preview_mode", "back"])
        settings["helper_enabled"] = True
        self.assertEqual([record["id"] for record in app.settings_records(settings)][:2], ["helper_enabled", "helper"])
        settings["wrapper_enabled"] = True
        self.assertNotIn("wrapper_command", [record["id"] for record in app.settings_records(settings)])

    def test_disabling_helper_preserves_choice_and_reenabling_uses_it(self):
        settings = app.read_settings()
        settings.update(helper_enabled=True, helper="yay")
        app.save_settings(settings)
        with patch.object(app, "picker", side_effect=[selected("helper_enabled"), None]):
            app.settings_menu()
        self.assertEqual(app.AUR_HELPER, "")
        self.assertEqual(app.read_settings()["helper"], "yay")
        with patch.object(app, "picker", side_effect=[selected("helper_enabled"), None]), patch.object(app.shutil, "which", return_value="/bin/yay"):
            app.settings_menu()
        self.assertEqual(app.AUR_HELPER, "yay")

    def test_settings_toggles_and_details_are_independent_and_persist(self):
        choices = [selected("traur_enabled"), selected("wrapper_enabled"), selected("preview_mode"), None]
        with patch.object(app, "picker", side_effect=choices):
            app.settings_menu()
        settings = app.read_settings()
        self.assertFalse(settings["helper_enabled"])
        self.assertTrue(settings["traur_enabled"])
        self.assertTrue(settings["wrapper_enabled"])
        self.assertEqual(settings["preview_mode"], "summary")
        self.assertEqual(app.update_commands(), [["sudo", "pacman", "-Syu"]])
        with patch.object(app, "capture") as capture:
            app.audit_update()
        capture.assert_not_called()

    def test_vt_key_settings_use_hidden_input_and_never_display_the_key(self):
        key = "a" * 64
        path = self.directory / "virustotal.key"
        with patch.object(app, "VT_KEY", path), patch.dict(os.environ, {"VT_API_KEY": ""}), patch.object(app, "picker", side_effect=[selected("vt_key"), None]) as picker, patch.object(app.getpass, "getpass", return_value=key) as prompt, contextlib.redirect_stdout(io.StringIO()) as output:
            app.settings_menu()
        prompt.assert_called_once()
        self.assertEqual(path.read_text(), key)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertNotIn(key, output.getvalue())
        self.assertNotIn(key, repr(picker.call_args_list))
        self.assertFalse(app.CONFIG_FILE.exists())

    def test_empty_vt_key_input_leaves_existing_key_unchanged(self):
        path = self.directory / "virustotal.key"
        path.write_text("old-key")
        with patch.object(app, "VT_KEY", path), patch.object(app, "picker", side_effect=[selected("vt_key"), None]), patch.object(app.getpass, "getpass", return_value=""):
            app.settings_menu()
        self.assertEqual(path.read_text(), "old-key")

    def test_wrapper_uses_explicit_makepkg_option_for_paru_and_yay(self):
        wrapper = str(app.BUNDLED_WRAPPER)
        app.WRAPPER_ENABLED = True
        for helper in ("paru", "yay"):
            app.configure_helper(helper)
            with self.subTest(helper=helper):
                self.assertEqual(app.transaction_command("install", [package()]),
                                 [helper, "--makepkg", wrapper, "-S", "--needed", "--aur", "--", "example"])
                self.assertEqual(app.update_commands(), [[helper, "-Syu", "--repo"], [helper, "--makepkg", wrapper, "-Sua"]])

    def test_missing_bundled_wrapper_blocks_before_transactions(self):
        app.WRAPPER_ENABLED = True
        with patch.object(app, "BUNDLED_WRAPPER", self.directory / "missing"), patch.object(app.subprocess, "call") as transaction, self.assertRaises(app.Error):
            app.perform("install", [package()])
        transaction.assert_not_called()

    def test_disabled_wrapper_does_not_override_makepkg_or_need_guard_files(self):
        with patch.object(app, "BUNDLED_WRAPPER", self.directory / "missing"):
            self.assertEqual(app.transaction_command("install", [package()]),
                             ["paru", "-S", "--needed", "--aur", "--", "example"])

    def test_legacy_external_wrapper_path_is_ignored_and_removed_on_save(self):
        app.CONFIG_FILE.parent.mkdir(parents=True)
        app.CONFIG_FILE.write_text(json.dumps({"wrapper_enabled": True, "wrapper_command": "/old/local/wrapper"}))
        settings = app.read_settings()
        self.assertNotIn("wrapper_command", settings)
        self.assertTrue(settings["wrapper_enabled"])
        app.save_settings(settings)
        self.assertNotIn("wrapper_command", json.loads(app.CONFIG_FILE.read_text()))

    def test_unsupported_wrapper_helper_is_not_silently_unprotected(self):
        app.WRAPPER_ENABLED = True
        for style in ("aura", "custom"):
            app.configure_helper("helper", style)
            with self.subTest(style=style), patch.object(app.subprocess, "call") as transaction, self.assertRaises(app.Error):
                app.perform("install", [package()])
            transaction.assert_not_called()

    def scan_result(self, tier="Ok", score=85, **changes):
        report = {"schema_version": 2, "package": "example", "status": "complete", "tier": tier,
                  "score": score, "signals": [], "override_gate_fired": None}
        report.update(changes)
        return json.dumps(report)

    def test_enabled_traur_shows_score_before_helper_transaction(self):
        app.TRAUR_ENABLED = True
        events = []
        with patch.object(app, "capture", side_effect=lambda *a, **kw: events.append("scan") or (self.scan_result(), 0)) as capture, patch.object(app.subprocess, "call", side_effect=lambda command, **kwargs: events.append("install") or 0), contextlib.redirect_stdout(io.StringIO()) as output:
            app.perform("install", [package()])
        self.assertEqual(events, ["scan", "install"])
        self.assertIn("Trust score: 85 / 100", output.getvalue())
        capture.assert_called_once_with(["traur", "scan", "example", "--json"], timeout=90, accept_report=True, return_status=True)

    def test_traur_skips_bad_aur_reports_without_blocking_selected_repository_packages(self):
        app.TRAUR_ENABLED = True
        results = [(self.scan_result("Malicious", 0), 1), (self.scan_result("Suspicious", 20), 1),
                   (self.scan_result(status="incomplete"), 0), (self.scan_result(score=True), 0),
                   (self.scan_result(package="other"), 0), (self.scan_result(), 2),
                   (self.scan_result(override_gate_fired="bad-script"), 0), ("not JSON", 0)]
        for result in results:
            app.LAST_SKIPPED.clear()
            with self.subTest(result=result), patch.object(app, "capture", return_value=result), patch.object(app.subprocess, "call", return_value=0) as transaction, contextlib.redirect_stdout(io.StringIO()):
                app.perform("install", [{**package("repo"), "name": "repo-example", "source": "extra"}, package()])
            transaction.assert_called_once_with(["sudo", "pacman", "-S", "--needed", "--", "extra/repo-example"])
            self.assertEqual([item["name"] for item in app.LAST_SKIPPED], ["example"])

    def test_disabled_traur_and_repository_only_installs_do_not_scan(self):
        for enabled, records in ((False, [package()]), (True, [{**package("repo"), "source": "extra"}])):
            app.TRAUR_ENABLED = enabled
            with self.subTest(enabled=enabled), patch.object(app, "capture") as capture, patch.object(app.subprocess, "call", return_value=0), contextlib.redirect_stdout(io.StringIO()):
                app.perform("install", records)
            capture.assert_not_called()

    def test_update_scans_only_aur_upgrade_candidates_before_starting_update(self):
        app.TRAUR_ENABLED = True
        with patch.object(app, "capture", side_effect=["example 1 -> 2\n", (self.scan_result(), 0)]) as capture, patch.object(app, "picker", side_effect=[selected("update"), None]), patch.object(app.subprocess, "call", return_value=0) as transaction, patch.object(app, "pause") as pause, contextlib.redirect_stdout(io.StringIO()):
            app.main()
        capture.assert_any_call(["paru", "-Qua", "--color", "never"], timeout=60, empty_ok=True)
        self.assertEqual([item.args[0] for item in transaction.call_args_list],
                          [["paru", "-Syu", "--repo"], ["paru", "-S", "--aur", "--", "example"]])
        pause.assert_called_once_with("Packages")

    def test_repository_only_update_has_no_traur_scans_or_audit_banner(self):
        app.TRAUR_ENABLED = True
        with patch.object(app, "capture", return_value="") as capture, patch.object(app, "audit") as audit, patch.object(app, "aur_info") as info, patch.object(app, "picker", side_effect=[selected("update"), None]), patch.object(app.subprocess, "call", return_value=0) as transaction, contextlib.redirect_stdout(io.StringIO()) as output:
            app.main()
        capture.assert_called_once_with(["paru", "-Qua", "--color", "never"], timeout=60, empty_ok=True)
        audit.assert_not_called()
        info.assert_not_called()
        self.assertNotIn("Traur:", output.getvalue())
        self.assertNotIn("AUR report", output.getvalue())
        transaction.assert_called_once_with(["paru", "-Syu", "--repo"])

    def test_paru_and_yay_update_queries_skip_ignored_packages_and_deduplicate(self):
        output = "update-me 1 -> 2\nignored 1 -> 2 [ignored]\nupdate-me 1 -> 2\nvcs-git 1 -> latest-commit\n"
        for helper in ("paru", "yay"):
            app.configure_helper(helper)
            with self.subTest(helper=helper), patch.object(app, "capture", return_value=output) as capture:
                self.assertEqual(app.aur_update_names(), ["update-me", "vcs-git"])
            capture.assert_called_once_with([helper, "-Qua", "--color", "never"], timeout=60, empty_ok=True)

    def test_update_query_failure_stops_without_scanning_everything_or_running_update(self):
        app.TRAUR_ENABLED = True
        for result in (app.Error("query failed"), "unrecognized query output\n", "--noconfirm 1 -> 2\n"):
            parameters = {"side_effect": result} if isinstance(result, Exception) else {"return_value": result}
            with self.subTest(result=result), patch.object(app, "capture", **parameters), patch.object(app, "audit") as audit, patch.object(app.subprocess, "call") as transaction, patch.object(app, "picker", side_effect=[selected("update"), None]), patch.object(app, "pause") as pause, contextlib.redirect_stdout(io.StringIO()):
                app.main()
            audit.assert_not_called()
            transaction.assert_called_once_with(["paru", "-Syu", "--repo"])
            pause.assert_called_once_with()

    def test_aura_and_custom_compare_versions_not_all_installed_aur_packages(self):
        installed = "same 1.0-1\nnewer-local 2.0-1\nupdate-me 1:1.0-1\nlocal-only 1\n"
        metadata = {"same": {"Version": "1.0-1"}, "newer-local": {"Version": "1.0-1"},
                    "update-me": {"Version": "1:2.0-1"}}
        for style in ("aura", "custom"):
            app.configure_helper("helper", style)
            with self.subTest(style=style), patch.object(app, "capture", side_effect=[installed, "0\n", "-1\n", "1\n"]) as capture, patch.object(app, "aur_info", return_value=metadata):
                self.assertEqual(app.aur_update_names(), ["update-me"])
            self.assertEqual(capture.call_args_list, [call(["pacman", "-Qm"], empty_ok=True),
                             call(["vercmp", "1.0-1", "1.0-1"]), call(["vercmp", "1.0-1", "2.0-1"]),
                             call(["vercmp", "1:2.0-1", "1:1.0-1"])])

    def test_no_foreign_packages_or_no_newer_aur_versions_produces_no_scan(self):
        app.configure_helper("aura")
        app.TRAUR_ENABLED = True
        for queries, metadata in (([""], {}), (["example 1.0\n", "0\n"], {"example": {"Version": "1.0"}})):
            with self.subTest(queries=queries), patch.object(app, "capture", side_effect=queries), patch.object(app, "aur_info", return_value=metadata), patch.object(app, "audit") as audit:
                app.audit_update()
            audit.assert_not_called()

    def test_native_and_disabled_traur_modes_do_not_query_aur_updates(self):
        for helper, enabled in (("pacman", True), ("paru", False)):
            app.configure_helper(helper)
            app.TRAUR_ENABLED = enabled
            with self.subTest(helper=helper), patch.object(app, "capture") as capture, patch.object(app, "audit") as audit:
                app.audit_update()
            capture.assert_not_called()
            audit.assert_not_called()

    def test_runtime_callbacks_keep_scan_wrapper_and_preview_preferences(self):
        app.TRAUR_ENABLED, app.WRAPPER_ENABLED = True, True
        app.PREVIEW_MODE = "summary"
        command = app.frontend_command()
        with patch.object(app.shutil, "which", return_value="/bin/paru"), patch.object(app, "main"):
            app.run(command[3:])
        self.assertTrue(app.TRAUR_ENABLED)
        self.assertTrue(app.WRAPPER_ENABLED)
        self.assertNotIn("wrapper_command", app.frontend_command()[-1])
        self.assertEqual(app.PREVIEW_MODE, "summary")

    def test_compact_preference_initializes_package_preview(self):
        app.PREVIEW_MODE = "summary"

        def run(arguments, **kwargs):
            path = next(self.directory.glob("package-menu-*/preview-mode"))
            self.assertEqual(path.read_text(), "summary")
            return subprocess.CompletedProcess(arguments, 130, "")

        with patch.dict(os.environ, {"XDG_RUNTIME_DIR": str(self.directory)}), patch.object(app.subprocess, "run", side_effect=run):
            app.picker([package()], "Packages")

    def test_empty_queries_accept_only_the_documented_no_results_status(self):
        for code in (0, 1, 2, -9):
            process = SimpleNamespace(returncode=code, communicate=lambda timeout: ("", ""))
            with self.subTest(code=code), patch.object(app.subprocess, "Popen", return_value=process):
                if code in (0, 1):
                    self.assertEqual(app.capture(["pacman", "-Qmq"], empty_ok=True), "")
                else:
                    with self.assertRaises(app.Error):
                        app.capture(["pacman", "-Qmq"], empty_ok=True)

    def test_report_capture_retains_nonzero_scanner_status(self):
        report = self.scan_result("Malicious", 0)
        process = SimpleNamespace(returncode=1, communicate=lambda timeout: (report, ""))
        with patch.object(app.subprocess, "Popen", return_value=process):
            self.assertEqual(app.capture(["traur", "scan", "example", "--json"], accept_report=True, return_status=True), (report, 1))

    def test_invalid_option_types_and_modes_are_rejected_before_saving(self):
        for key, value in (("helper_enabled", "true"), ("traur_enabled", 1), ("wrapper_enabled", []),
                           ("preview_mode", "anything")):
            settings = app.read_settings()
            settings[key] = value
            with self.subTest(key=key), self.assertRaises(app.Error):
                app.save_settings(settings)
        self.assertFalse(app.CONFIG_FILE.exists())


if __name__ == "__main__":
    unittest.main()
