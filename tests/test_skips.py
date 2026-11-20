import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app


KEY = "a" * 64


def package(name, kind="aur"):
    return {"id": name, "name": name, "kind": kind, "source": "aur" if kind == "aur" else "extra", "version": "1"}


def selection(key):
    return "", "", [{"id": key}]


class SkipTests(unittest.TestCase):
    def setUp(self):
        self.output = io.StringIO()
        self.enterContext(contextlib.redirect_stdout(self.output))
        self.directory = self.enterContext(tempfile.TemporaryDirectory())
        for key, value in (("AUR_HELPER", "paru"), ("HELPER_STYLE", "pacman"),
                           ("TRAUR_ENABLED", False), ("WRAPPER_ENABLED", False), ("LAST_SKIPPED", []),
                           ("CONFIG_FILE", Path(self.directory) / "config.json")):
            self.enterContext(patch.object(app, key, value))

    def blocked(self, environment, key=KEY):
        event = {"version": 1, "blocked": True, "reason": "Malicious recipe / dependency", "approval_key": key}
        Path(environment["PACKAGE_MENU_GATE_EVENT"]).write_text(json.dumps(event) + "\n")
        return 1

    def test_multi_install_runs_repository_and_healthy_aur_despite_wrapper_denial(self):
        calls = []

        def transaction(command, **kwargs):
            calls.append(command)
            return self.blocked(kwargs["env"]) if command[-1] == "bad" else 0

        with patch.object(app.subprocess, "call", side_effect=transaction):
            app.perform("install", [package("repo", "repo"), package("bad"), package("good")])
        self.assertEqual([command[-1] for command in calls], ["extra/repo", "bad", "good"])
        self.assertEqual([item["name"] for item in app.LAST_SKIPPED], ["bad"])
        self.assertIn("SKIPPED bad", self.output.getvalue())

    def test_single_install_is_skipped_without_an_override_prompt(self):
        with patch.object(app.subprocess, "call", side_effect=lambda command, **kw: self.blocked(kw["env"])), patch("builtins.input", side_effect=AssertionError("default must not ask")):
            app.perform("install", [package("bad")])
        self.assertEqual(app.LAST_SKIPPED[0]["approval_keys"], [KEY])

    def test_blocked_dependency_skips_current_target_but_not_unrelated_target(self):
        with patch.object(app.subprocess, "call", side_effect=lambda command, **kw: self.blocked(kw["env"]) if command[-1] == "needs-bad-dependency" else 0) as transaction:
            app.perform("install", [package("needs-bad-dependency"), package("independent")])
        self.assertEqual(transaction.call_count, 2)
        self.assertEqual([item["name"] for item in app.LAST_SKIPPED], ["needs-bad-dependency"])

    def test_system_repositories_update_before_any_aur_security_checks(self):
        events = []

        def transaction(command, **kwargs):
            events.append(command)
            return self.blocked(kwargs["env"]) if command[-1] == "bad" else 0

        with patch.object(app, "aur_update_names", return_value=["bad", "good"]), patch.object(app.subprocess, "call", side_effect=transaction):
            app.update_system()
        self.assertEqual(events[0], ["paru", "-Syu", "--repo"])
        self.assertEqual([command[-1] for command in events[1:]], ["bad", "good"])
        self.assertNotIn("--needed", events[1])
        self.assertEqual([item["name"] for item in app.LAST_SKIPPED], ["bad"])

    def test_dependencies_already_updated_are_not_reinstalled(self):
        with patch.object(app, "aur_update_names", side_effect=[["first", "dependency"], []]), patch.object(app.subprocess, "call", return_value=0) as transaction:
            app.update_system()
        self.assertEqual([item.args[0] for item in transaction.call_args_list],
                         [["paru", "-Syu", "--repo"], ["paru", "-S", "--aur", "--", "first"]])

    def test_system_update_reports_each_aur_target_and_progress(self):
        with patch.object(app, "aur_update_names", side_effect=[["first", "second"], ["second"]]), patch.object(app, "run_aur_target", return_value=True) as update, patch.object(app.subprocess, "call", return_value=0):
            app.update_system()
        self.assertEqual([call.args[0]["name"] for call in update.call_args_list], ["first", "second"])
        self.assertIn("Updating repository packages...", self.output.getvalue())
        self.assertIn("Found 2 AUR update(s).", self.output.getvalue())
        self.assertIn("Updating AUR package 1/2: first", self.output.getvalue())
        self.assertIn("Finished AUR package: second", self.output.getvalue())

    def test_aur_update_failure_identifies_the_package(self):
        with patch.object(app, "aur_update_names", return_value=["broken"]), patch.object(app.subprocess, "call", side_effect=[0, 7]) as transaction:
            with self.assertRaisesRegex(app.Error, "AUR package broken operation stopped \\(exit 7\\)"):
                app.update_system()
        self.assertEqual(transaction.call_count, 2)

    def test_preflight_warning_skips_only_bad_target(self):
        app.TRAUR_ENABLED = True

        def audit(records, strict=False):
            if records[0]["name"] == "bad":
                raise app.Error("Traur says Suspicious")

        with patch.object(app, "audit", side_effect=audit), patch.object(app.subprocess, "call", return_value=0) as transaction:
            app.perform("install", [package("bad"), package("good")])
        self.assertEqual(transaction.call_args.args[0][-1], "good")
        self.assertTrue(app.LAST_SKIPPED[0]["preflight"])

    def test_explicit_wrapper_does_not_duplicate_a_remote_preflight_scan(self):
        app.TRAUR_ENABLED, app.WRAPPER_ENABLED = True, True
        with patch.object(app, "with_wrapper", side_effect=lambda command: command), patch.object(app, "audit") as audit, patch.object(app.subprocess, "call", return_value=0):
            app.perform("install", [package("good")])
        audit.assert_not_called()

    def test_unrelated_helper_failure_stops_instead_of_skipping_or_retrying(self):
        with patch.object(app.subprocess, "call", return_value=7) as transaction, self.assertRaises(app.Error):
            app.perform("install", [package("first"), package("second")])
        transaction.assert_called_once()
        self.assertEqual(app.LAST_SKIPPED, [])

    def test_user_interrupt_stops_remaining_targets_even_after_a_denial(self):
        def transaction(command, **kwargs):
            self.blocked(kwargs["env"])
            return 130

        with patch.object(app.subprocess, "call", side_effect=transaction) as call, self.assertRaises(KeyboardInterrupt):
            app.perform("install", [package("first"), package("second")])
        call.assert_called_once()

    def test_malformed_wrapper_event_does_not_authorize_continuation(self):
        def transaction(command, **kwargs):
            Path(kwargs["env"]["PACKAGE_MENU_GATE_EVENT"]).write_text('{"blocked": true}')
            return 1

        with patch.object(app.subprocess, "call", side_effect=transaction) as call, self.assertRaises(app.Error):
            app.perform("install", [package("first"), package("second")])
        call.assert_called_once()

    def test_installer_does_not_inherit_ambient_override_and_removes_protocol_files(self):
        paths = []

        def transaction(command, **kwargs):
            self.assertNotIn("PACKAGE_MENU_GATE_APPROVAL", kwargs["env"])
            paths.append(Path(kwargs["env"]["PACKAGE_MENU_GATE_EVENT"]))
            return 0

        with patch.dict(app.os.environ, {"PACKAGE_MENU_GATE_APPROVAL": "/stale/approval"}), patch.object(app.subprocess, "call", side_effect=transaction):
            app.perform("install", [package("good")])
        self.assertTrue(all(not path.exists() for path in paths))

    def test_install_anyway_requires_typing_the_selected_package_name(self):
        app.skip_package(package("bad"), "review me", [KEY])
        with patch.object(app, "picker", side_effect=[selection("0"), None]), patch("builtins.input", return_value="") as input_, patch.object(app.subprocess, "call") as transaction:
            app.skipped_menu()
        input_.assert_called_once()
        transaction.assert_not_called()
        self.assertEqual(len(app.LAST_SKIPPED), 1)

    def test_manual_override_is_private_scoped_and_not_a_permanent_whitelist(self):
        app.skip_package(package("bad"), "review me", [KEY])
        app.skip_package(package("other"), "other reason", ["b" * 64])
        paths = []

        def transaction(command, **kwargs):
            path = Path(kwargs["env"]["PACKAGE_MENU_GATE_APPROVAL"])
            paths.append(path)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(json.loads(path.read_text()), {"version": 1, "keys": [KEY]})
            return 0

        with patch.object(app, "picker", side_effect=[selection("0"), None]), patch("builtins.input", return_value="bad"), patch.object(app.subprocess, "call", side_effect=transaction):
            app.skipped_menu()
        self.assertEqual([item["name"] for item in app.LAST_SKIPPED], ["other"])
        self.assertTrue(all(not path.exists() for path in paths))
        self.assertFalse(app.CONFIG_FILE.exists())

    def test_changed_recipe_can_be_denied_again_during_an_explicit_override(self):
        app.skip_package(package("bad"), "old report", [KEY])
        new_key = "b" * 64
        with patch.object(app, "picker", side_effect=[selection("0"), None]), patch("builtins.input", return_value="bad"), patch.object(app.subprocess, "call", side_effect=lambda command, **kw: self.blocked(kw["env"], new_key)):
            app.skipped_menu()
        self.assertEqual(app.LAST_SKIPPED[0]["approval_keys"], [new_key])

    def test_unscoped_wrapper_failure_has_no_install_anyway_action(self):
        app.skip_package(package("bad"), "integrity checks disabled")
        with patch.object(app, "picker", side_effect=[selection("0"), None]), patch.object(app, "pause"), patch("builtins.input", side_effect=AssertionError("not an overridable verdict")), patch.object(app.subprocess, "call") as transaction:
            app.skipped_menu()
        transaction.assert_not_called()

    def test_skipped_packages_remain_visible_in_the_main_menu(self):
        app.skip_package(package("bad"), "review me", [KEY])
        with patch.object(app, "picker", return_value=None) as picker:
            app.main()
        self.assertIn("skipped", [record["id"] for record in picker.call_args.args[0]])
        self.assertIn("bad", picker.call_args.kwargs["keys"])


if __name__ == "__main__":
    unittest.main()
