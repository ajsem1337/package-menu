"""Pre-execution Traur gate for makepkg. This is a scanner, not a sandbox."""

import getopt
import configparser
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import shutil
import stat
import subprocess
import sys
import time

HOME = Path.home()
STATE = Path(os.environ.get("XDG_STATE_HOME") or HOME / ".local/state") / "package-menu/guard"
CACHE = Path(os.environ.get("XDG_CACHE_HOME") or HOME / ".cache")
SCANNER = Path(shutil.which("traur") or "/usr/bin/traur")
REPORTS = STATE / "reports"
PROVENANCE = STATE / "provenance"
DISPLAY = STATE / "display"
REAL_MAKEPKG = "/usr/bin/makepkg"
SHORT_OPTIONS = "AcCdD:efFghiLmop:rRsSV"
LONG_OPTIONS = [
    "allsource", "check", "clean", "cleanbuild", "config=", "dir=", "force", "geninteg",
    "help", "holdver", "ignorearch", "install", "key=", "log", "noarchive", "nobuild",
    "nocolor", "nocheck", "nodeps", "noextract", "noprepare", "nosign", "packagelist",
    "printsrcinfo", "repackage", "rmdeps", "sign", "skipchecksums", "skipinteg",
    "skippgpcheck", "source", "syncdeps", "verifysource", "version", "asdeps",
    "noconfirm", "needed", "noprogressbar",
]
ALLOWED = {"Trusted", "Ok"}


class Blocked(Exception):
    def __init__(self, message, approval_key=None):
        super().__init__(message)
        self.approval_key = approval_key


def private_protocol_file(variable, flags):
    value = os.environ.get(variable)
    if not value:
        return None
    descriptor = os.open(value, flags | os.O_NOFOLLOW | os.O_NONBLOCK)
    info = os.fstat(descriptor)
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        os.close(descriptor)
        raise OSError("Insecure package-menu protocol file.")
    return descriptor


def approval_key(directory, result):
    # Helper metadata calls can rewrite identical files. Approvals bind to
    # actual bytes, not timestamps; the before/after scan check remains strict.
    data = [str(directory), fingerprint(directory, include_times=False), result]
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


def approved_once(key):
    try:
        descriptor = private_protocol_file("PACKAGE_MENU_GATE_APPROVAL", os.O_RDONLY)
        if descriptor is None:
            return False
        with os.fdopen(descriptor) as source:
            data = json.loads(source.read(65537))
        return (isinstance(data, dict) and data.get("version") == 1
                and isinstance(data.get("keys"), list) and key in data["keys"])
    except (OSError, ValueError, TypeError):
        return False


def report_block(error):
    try:
        descriptor = private_protocol_file("PACKAGE_MENU_GATE_EVENT", os.O_WRONLY | os.O_APPEND)
        if descriptor is None:
            return
        event = {"version": 1, "blocked": True, "reason": safe_text(error)}
        key = getattr(error, "approval_key", None)
        if key is not None:
            event["approval_key"] = key
        with os.fdopen(descriptor, "a") as output:
            output.write(json.dumps(event) + "\n")
    except OSError:
        pass  # Failure to notify the UI never turns a denial into approval.


def safe_text(value):
    return re.sub(r"[\x00-\x1f\x7f-\x9f]", "?", str(value))[:800]


def request_directory(argv, cwd):
    """Parse options without sourcing the PKGBUILD, including metadata calls."""
    try:
        options, trailing = getopt.gnu_getopt(argv, SHORT_OPTIONS, LONG_OPTIONS)
    except getopt.GetoptError as error:
        raise Blocked(f"Unsupported makepkg arguments: {error}") from error
    if any(option in ("-h", "--help", "-V", "--version") for option, _ in options):
        return None
    directory = Path(cwd)
    script = "PKGBUILD"
    fakeroot = any(option == "-F" for option, _ in options)
    for option, value in options:
        if option in ("-D", "--dir"):
            if not fakeroot:
                directory = Path(value) if Path(value).is_absolute() else Path(cwd) / value
        elif option == "-p":
            script = value
        elif option in ("--skipchecksums", "--skipinteg", "--skippgpcheck"):
            raise Blocked("Source integrity/signature checks must not be disabled.")
    # makepkg accepts environment overrides after options. Do not accept overrides
    # that can select a different input file or executable after the scan.
    allowed_overrides = {"CFLAGS", "CXXFLAGS", "CPPFLAGS", "LDFLAGS", "MAKEFLAGS", "RUSTFLAGS", "CC", "CXX"}
    for argument in trailing:
        name, separator, _ = argument.partition("=")
        if not separator or name.rstrip("+") not in allowed_overrides:
            raise Blocked(f"Unsupported makepkg environment override: {safe_text(name)}")
    directory = directory.resolve(strict=True)
    candidate = directory / script
    if candidate.absolute() != directory / "PKGBUILD":
        raise Blocked("Alternate build scripts are not covered; use the standard PKGBUILD filename.")
    if not candidate.exists():
        raise Blocked(f"No PKGBUILD in {safe_text(directory)}")
    if candidate.is_symlink() or not candidate.is_file():
        raise Blocked("PKGBUILD must be a regular, non-symlink file.")
    return directory


def fingerprint(directory, include_times=True):
    """Detect input edits during scanning; never cache an approval by package name."""
    digest = hashlib.sha256()
    count = 0
    for parent, dirs, files in os.walk(directory, followlinks=False):
        # Generated build outputs are not AUR recipes. They are NOT claimed to
        # have been audited by this gate; downloaded/upstream code is a limitation.
        if Path(parent) == directory:
            dirs[:] = [name for name in dirs if name not in {".git", "src", "pkg"}]
        dirs.sort()
        for name in sorted(dirs + files):
            path = Path(parent) / name
            relative = str(path.relative_to(directory))
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode):
                try:
                    resolved = path.resolve(strict=True)
                    target_relative = resolved.relative_to(directory)
                    target_info = resolved.stat()
                except (OSError, ValueError, RuntimeError):
                    raise Blocked(f"Recipe symlink escapes the recipe or has no readable target: {safe_text(relative)}") from None
                if not stat.S_ISREG(target_info.st_mode) or target_relative.parts[0] in {".git", "src", "pkg"}:
                    raise Blocked(f"Recipe symlink does not target a checked regular input: {safe_text(relative)}")
                # REUSE license links such as LICENSES/0BSD.txt -> ../LICENSE
                # are normal. Bind approval to BOTH the link and target bytes.
                digest.update(f"symlink:{os.readlink(path)}".encode() + b"\0")
                if include_times:
                    digest.update(f"{info.st_mtime_ns}:{info.st_ctime_ns}".encode())
                path, info = resolved, target_info
            if stat.S_ISDIR(info.st_mode):
                continue
            if not stat.S_ISREG(info.st_mode):
                raise Blocked(f"Non-regular recipe input: {safe_text(relative)}")
            count += 1
            if count > 4096:
                raise Blocked("Too many local inputs to check automatically.")
            digest.update(relative.encode() + b"\0")
            digest.update(str(info.st_size).encode())
            if include_times:
                digest.update(f"{info.st_mtime_ns}:{info.st_ctime_ns}".encode())
            if not include_times:
                with path.open("rb") as source:
                    for chunk in iter(lambda: source.read(1024 * 1024), b""):
                        digest.update(chunk)
            elif info.st_size <= 1024 * 1024:
                with path.open("rb") as source:
                    digest.update(source.read(1024 * 1024 + 1))
    return digest.hexdigest()


def scanner_environment():
    # No caller-controlled ignore list, whitelist, loader or Python environment.
    return {
        "PATH": "/usr/bin:/bin", "HOME": str(HOME), "LANG": "C.UTF-8",
        "XDG_CONFIG_HOME": str(STATE / "config"),
        "XDG_CACHE_HOME": str(CACHE),
        "NO_COLOR": "1",
    }


def verdict_allowed(result, returncode=0):
    return (
        isinstance(result, dict) and result.get("schema_version") == 2
        and result.get("status") == "complete" and result.get("tier") in ALLOWED
        and type(result.get("score")) is int and 0 <= result["score"] <= 100
        and isinstance(result.get("signals"), list)
        and result.get("override_gate_fired") is None and returncode == 0
    )


def report_session():
    """Group makepkg calls from one helper invocation; never cache approvals."""
    pid = os.getppid()
    for _ in range(16):
        try:
            process = Path("/proc") / str(pid)
            fields = (process / "stat").read_text().rsplit(") ", 1)[1].split()
            if (process / "comm").read_text().strip() in {"paru", "yay", "shelly"}:
                return f"{pid}:{fields[19]}"
            pid = int(fields[1])
            if pid <= 1:
                break
        except (OSError, ValueError, IndexError):
            break
    return f"manual:{os.getpid()}"


def show_report_once(directory, result, allowed, phase):
    """Presentation-only deduplication. Every local scan and gate still runs."""
    try:
        DISPLAY.mkdir(mode=0o700, parents=True, exist_ok=True)
        key = hashlib.sha256(f"{report_session()}:{directory}:{phase}".encode()).hexdigest()
        path = DISPLAY / f"{key}.json"
        if not allowed:
            path.unlink(missing_ok=True)
            return True
        digest = hashlib.sha256(json.dumps(result, sort_keys=True).encode()).hexdigest()
        try:
            previous = json.loads(path.read_text())
            if isinstance(previous, dict) and previous.get("digest") == digest and 0 <= time.time() - previous["shown_at"] < 600:
                return False
        except (OSError, ValueError, KeyError, TypeError):
            pass
        temporary = DISPLAY / f".{key}.{time.time_ns()}.{os.getpid()}.tmp"
        with open(temporary, "x", opener=lambda name, flags: os.open(name, flags, 0o600)) as destination:
            json.dump({"digest": digest, "shown_at": time.time()}, destination)
        temporary.replace(path)
    except OSError:
        # Failure to save display state must never hide a report or change policy.
        pass
    return True


def print_scanner_report(stderr):
    if stderr:
        visible = re.sub(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]", "?", stderr[:32768])
        print(visible, end="" if visible.endswith("\n") else "\n", file=sys.stderr)


def run_scan(directory, scanner=SCANNER, timeout=45, package=None):
    if not scanner.is_file() or not os.access(scanner, os.X_OK):
        raise Blocked("Traur is unavailable; nothing was checked.")
    arguments = [package] if package else ["--dir", str(directory)]
    target = "AUR package " + safe_text(package) if package else "build recipe " + safe_text(directory)
    print(f"[AUR protection] Scanning {target} with Traur...", file=sys.stderr, flush=True)
    process = subprocess.Popen(
        [str(scanner), "scan", *arguments, "--json", "--show-report"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        env=scanner_environment(), start_new_session=True,
    )
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except (subprocess.TimeoutExpired, KeyboardInterrupt) as error:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.communicate()
        if isinstance(error, KeyboardInterrupt):
            raise
        raise Blocked("Traur timed out; nothing has been approved.") from None
    try:
        result = json.loads(stdout)
    except (ValueError, TypeError):
        print_scanner_report(stderr)
        raise Blocked(f"Traur returned no valid report: {safe_text(stderr)}") from None
    if not isinstance(result, dict) or result.get("schema_version") != 2:
        print_scanner_report(stderr)
        raise Blocked("Unrecognized Traur report format.")
    if package is not None and result.get("package") != package:
        print_scanner_report(stderr)
        raise Blocked("Traur returned a report for a different AUR package.")
    allowed = verdict_allowed(result, process.returncode)
    if show_report_once(directory, result, allowed, "aur" if package else "local"):
        print_scanner_report(stderr)
    return result, allowed


def aur_package(directory):
    """Read static metadata, never shell-evaluate it (split packages included)."""
    in_helper_cache = any(directory.is_relative_to(cache) for cache in (
        CACHE / "paru/clone", CACHE / "yay",
    ))
    origin = ""
    config = directory / ".git/config"
    if config.is_file() and not config.is_symlink():
        parser = configparser.ConfigParser(interpolation=None, strict=False)
        try:
            parser.read_string(config.read_text())
            origin = parser.get('remote "origin"', "url", fallback="")
        except (OSError, UnicodeError, configparser.Error):
            raise Blocked("Cannot read the recipe's repository identity.") from None
    match = re.fullmatch(r"(?:https://aur\.archlinux\.org/|ssh://aur@aur\.archlinux\.org/|aur@aur\.archlinux\.org:)([A-Za-z0-9@._+-]+?)(?:\.git)?", origin)
    if not in_helper_cache and not match:
        return None
    base = match.group(1) if match else directory.name
    package = base
    srcinfo = directory / ".SRCINFO"
    if srcinfo.is_file():
        for line in srcinfo.read_text().splitlines():
            key, separator, value = line.strip().partition(" = ")
            if separator and key == "pkgname":
                package = value
                break
    if not re.fullmatch(r"[A-Za-z0-9@._+-]+", package) or ".." in package or package.startswith("-"):
        raise Blocked("Invalid package identity in AUR metadata.")
    return package


def provenance_check(directory, fingerprint_value, package, scanner=SCANNER, cache=PROVENANCE):
    """Short content-bound cache for network checks; local code is ALWAYS rescanned."""
    scanner_hash = hashlib.sha256(scanner.read_bytes()).hexdigest()
    key = hashlib.sha256(f"{package}:{fingerprint_value}:{scanner_hash}:v1".encode()).hexdigest()
    cache.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = cache / f"{key}.json"
    try:
        cached = json.loads(path.read_text())
        if 0 <= time.time() - cached["checked_at"] < 600 and verdict_allowed(cached["result"]):
            return cached["result"], True
    except (OSError, ValueError, KeyError, TypeError):
        pass
    result, allowed = run_scan(directory, scanner=scanner, timeout=90, package=package)
    if allowed:
        temporary = cache / f".{key}.{time.time_ns()}.{os.getpid()}.tmp"
        with open(temporary, "x", opener=lambda name, flags: os.open(name, flags, 0o600)) as destination:
            json.dump({"checked_at": int(time.time()), "result": result}, destination)
        temporary.replace(path)
    return result, allowed


def save_report(directory, fingerprint_value, result, reports=REPORTS):
    reports.mkdir(mode=0o700, parents=True, exist_ok=True)
    record = {"checked_at": int(time.time()), "directory": str(directory), "input_fingerprint": fingerprint_value, "result": result}
    path = reports / f"{time.time_ns()}-{os.getpid()}.json"
    with open(path, "x", opener=lambda name, flags: os.open(name, flags, 0o600)) as destination:
        json.dump(record, destination, ensure_ascii=True, indent=2)
    return path


def inconclusive(directory, error):
    return {"schema_version": 2, "package": directory.name, "status": "incomplete",
            "tier": "Unknown", "score": 0, "signals": [], "override_gate_fired": None,
            "reason": str(error)}


def gate(directory, scanner=SCANNER, reports=REPORTS):
    before = fingerprint(directory)
    try:
        result, allowed = run_scan(directory, scanner=scanner)
    except Blocked as error:
        result, allowed = inconclusive(directory, error), False
    report = save_report(directory, before, result, reports=reports)
    package = aur_package(directory)
    if allowed and package:
        try:
            result, allowed = provenance_check(directory, before, package, scanner=scanner)
        except Blocked as error:
            result, allowed = inconclusive(directory, error), False
        report = save_report(directory, before, result, reports=reports)
    if before != fingerprint(directory):
        raise Blocked("Recipe inputs changed during the scan. Run the command again.")
    if not allowed:
        key = approval_key(directory, result)
        if before != fingerprint(directory):
            raise Blocked("Recipe inputs changed while preparing the report. Review the new inputs first.")
        if approved_once(key):
            print("[AUR protection] Explicit user override for these exact recipe inputs and this report, for this operation only.", file=sys.stderr)
            return
        print(f"[AUR protection] STOP: {safe_text(result.get('tier', 'Unknown'))}", file=sys.stderr)
        findings = result.get("signals", [])
        for finding in (findings if isinstance(findings, list) else [])[:12]:
            if isinstance(finding, dict) and finding.get("role") not in ("Fact", "Provenance"):
                print(f"  {safe_text(finding.get('id'))}: {safe_text(finding.get('description'))}", file=sys.stderr)
        if result.get("reason"):
            print(f"  {safe_text(result['reason'])}", file=sys.stderr)
        raise Blocked(f"Build stopped. Report: {report}\nReview this package before an explicit one-operation override.", approval_key=key)
    # Only report display is deduplicated; every local scan still runs.


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv == ["--package-menu-wrapper-version"]:
        print("package-menu wrapper " + (Path(__file__).resolve().parent / "VERSION").read_text().strip())
        return 0
    try:
        directory = request_directory(argv, Path.cwd())
        if directory is not None:
            gate(directory)
        # This is the original system binary, not this PATH wrapper again.
        environment = dict(os.environ)
        for name in list(environment):
            if name in {"BUILDFILE", "CHDIR", "BASH_ENV", "ENV", "LD_PRELOAD", "LD_LIBRARY_PATH"} or name.startswith("BASH_FUNC_"):
                environment.pop(name)
        os.execve(REAL_MAKEPKG, [REAL_MAKEPKG, *argv], environment)
    except KeyboardInterrupt:
        print("\n[AUR protection] Scan cancelled.", file=sys.stderr)
        return 130
    except (Blocked, OSError, ValueError, UnicodeError, subprocess.SubprocessError) as error:
        report_block(error)
        for index, line in enumerate(str(error).splitlines()[:8]):
            prefix = "[AUR protection] BLOCKED: " if index == 0 else "  "
            print(prefix + safe_text(line), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
