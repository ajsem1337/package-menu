"""Small fzf frontend. Package operations remain the package managers' responsibility."""

import colorsys
import datetime
import getpass
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import textwrap
import time
import urllib.error
import urllib.parse
import urllib.request

HOME = Path.home()
HERE = Path(__file__).resolve()
VT_KEY = HOME / ".config/package-menu/virustotal.key"
CONFIG_FILE = Path(os.environ.get("XDG_CONFIG_HOME") or HOME / ".config") / "package-menu/config.json"
NAME = re.compile(r"[A-Za-z0-9@._+-]+")
MAX_BATCH = 50
AUR_HELPER = ""
HELPER_STYLE = "pacman"
HELPER_INSTALL_ARGS = ["-S", "--needed", "--aur", "--"]
HELPER_UPDATE_ARGS = ["-Syu"]
TRAUR_ENABLED = False
WRAPPER_ENABLED = False
BUNDLED_WRAPPER = HERE.parent / "makepkg-wrapper"
PREVIEW_MODE = "details"
LAST_SKIPPED = []


class Error(Exception):
    pass


def helper_executable(value):
    if not isinstance(value, str):
        raise Error("Enter a helper executable or choose Pacman only.")
    value = value.strip()
    if not value or value.startswith("-") or any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise Error("Set a valid helper executable or choose Pacman only.")
    return value


def helper_arguments(value):
    if not isinstance(value, list) or any(not isinstance(arg, str) or any(ord(char) < 32 or ord(char) == 127 for char in arg) for arg in value):
        raise Error("Helper arguments must be a list of strings without control characters.")
    return value.copy()


def configure_helper(value, style="auto", install_args=None, update_args=None):
    global AUR_HELPER, HELPER_STYLE, HELPER_INSTALL_ARGS, HELPER_UPDATE_ARGS
    value = helper_executable(value)
    if style not in ("auto", "pacman", "aura", "custom"):
        raise Error("Unknown helper command style.")
    install_args = helper_arguments(install_args if install_args is not None else ["-S", "--needed", "--aur", "--"])
    update_args = helper_arguments(update_args if update_args is not None else ["-Syu"])
    if value == "pacman":
        value = ""
    if style == "auto":
        style = "aura" if Path(value).name == "aura" else "pacman"
    AUR_HELPER = value
    HELPER_STYLE = style
    HELPER_INSTALL_ARGS, HELPER_UPDATE_ARGS = install_args, update_args


def read_settings():
    settings = {"helper_enabled": False, "helper": "paru", "style": "auto",
                "install_args": ["-S", "--needed", "--aur", "--"], "update_args": ["-Syu"],
                "traur_enabled": False, "wrapper_enabled": False, "preview_mode": "details"}
    if CONFIG_FILE.exists():
        try:
            saved = json.loads(CONFIG_FILE.read_text())
        except (ValueError, UnicodeError):
            raise Error(f"Invalid settings file: {CONFIG_FILE}") from None
        if not isinstance(saved, dict):
            raise Error(f"Invalid settings file: {CONFIG_FILE}")
        settings.update(saved)
    settings.pop("wrapper_command", None)  # Legacy external-wrapper setting.
    validate_settings(settings)
    return settings


def validate_settings(settings):
    for key in ("helper_enabled", "traur_enabled", "wrapper_enabled"):
        if type(settings.get(key)) is not bool:
            raise Error(key + " must be true or false.")
    helper_executable(settings.get("helper"))
    if settings.get("style") not in ("auto", "pacman", "aura", "custom"):
        raise Error("Unknown helper command style.")
    helper_arguments(settings.get("install_args"))
    helper_arguments(settings.get("update_args"))
    if settings.get("preview_mode") not in ("details", "summary"):
        raise Error("Unknown package details mode.")


def activate_settings(settings):
    global TRAUR_ENABLED, WRAPPER_ENABLED, PREVIEW_MODE
    validate_settings(settings)
    helper = helper_executable(settings["helper"]) if settings["helper_enabled"] else "pacman"
    if helper == "auto":
        helper = next((name for name in ("paru", "yay", "aura") if shutil.which(name)), "pacman")
    elif helper != "pacman" and not shutil.which(helper):
        helper = "pacman"
    configure_helper(helper, settings["style"], settings["install_args"], settings["update_args"])
    TRAUR_ENABLED = settings["traur_enabled"]
    WRAPPER_ENABLED = settings["wrapper_enabled"]
    PREVIEW_MODE = settings["preview_mode"]


def save_settings(settings):
    validate_settings(settings)
    CONFIG_FILE.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", dir=CONFIG_FILE.parent, delete=False) as temporary:
            temporary_path = Path(temporary.name)
            json.dump(settings, temporary, indent=2)
            temporary.write("\n")
        temporary_path.replace(CONFIG_FILE)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def frontend_command(*args):
    # fzf callbacks run in separate processes; retain the chosen helper there too.
    command = [sys.executable, "-I", str(HERE), "--aur-helper", AUR_HELPER or "pacman",
               "--helper-style", HELPER_STYLE]
    if HELPER_STYLE == "custom":
        command += ["--helper-install-args", json.dumps(HELPER_INSTALL_ARGS),
                    "--helper-update-args", json.dumps(HELPER_UPDATE_ARGS)]
    command += ["--runtime-settings", json.dumps({"helper_enabled": bool(AUR_HELPER),
                "traur_enabled": TRAUR_ENABLED, "wrapper_enabled": WRAPPER_ENABLED,
                "preview_mode": PREVIEW_MODE})]
    return [*command, *args]


def with_wrapper(command):
    if not WRAPPER_ENABLED or not AUR_HELPER:
        return command
    if HELPER_STYLE != "pacman":
        raise Error("The makepkg wrapper requires Paru/yay command syntax; this helper is not supported.")
    if not BUNDLED_WRAPPER.is_file() or not os.access(BUNDLED_WRAPPER, os.X_OK) or not (HERE.parent / "guard.py").is_file():
        raise Error("The bundled makepkg wrapper is missing. Reinstall package-menu.")
    return [command[0], "--makepkg", str(BUNDLED_WRAPPER), *command[1:]]


def update_command():
    if not AUR_HELPER:
        return ["sudo", "pacman", "-Syu"]
    if HELPER_STYLE == "custom":
        return with_wrapper([AUR_HELPER, *HELPER_UPDATE_ARGS])
    return with_wrapper([AUR_HELPER, "-Syu"])


def update_commands():
    if AUR_HELPER and HELPER_STYLE == "pacman":
        return [[AUR_HELPER, "-Syu", "--repo"], with_wrapper([AUR_HELPER, "-Sua"])]
    commands = [update_command()]
    if AUR_HELPER and HELPER_STYLE == "aura":
        commands.append([AUR_HELPER, "-Au"])
    return commands


def text(value):
    return re.sub(r"[\x00-\x1f\x7f-\x9f]", " ", str(value)).strip()


def valid_name(value):
    if not NAME.fullmatch(value) or value.startswith(("-", ".")):
        raise Error("Invalid package/repository name.")
    return value


def capture(argv, timeout=20, empty_ok=False, accept_report=False, return_status=False):
    environment = dict(os.environ, LC_ALL="C")
    process = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               text=True, env=environment, start_new_session=True)
    try:
        output, errors = process.communicate(timeout=timeout)
    except (subprocess.TimeoutExpired, KeyboardInterrupt):
        os.killpg(process.pid, signal.SIGKILL)
        process.communicate()
        raise Error("Command cancelled or timed out; no result was accepted.") from None
    if process.returncode and not (accept_report and output) and not (empty_ok and process.returncode == 1 and not output and not errors):
        raise Error(text(errors) or f"{argv[0]} failed ({process.returncode}).")
    return (output, process.returncode) if return_status else output


def parse_search(output, kind):
    records = []
    for line in output.splitlines():
        match = re.match(r"^([^/\s]+)/([^\s]+)\s+(\S+)(.*)$", line)
        if match:
            source, name, version, extra = match.groups()
            valid_name(source)
            valid_name(name)
            records.append({"id": f"{source}/{name}", "name": name, "source": source,
                            "version": version, "state": "installed" if "[installed" in extra else "",
                            "description": "", "kind": kind, "extra": text(extra)})
        elif line[:1].isspace() and records:
            records[-1]["description"] += (" " if records[-1]["description"] else "") + text(line)
    return records


def parse_info(output):
    fields, current = {}, None
    for line in output.splitlines():
        match = re.match(r"^([A-Za-z][A-Za-z ]*?)\s+: (.*)$", line)
        if match:
            current, value = match.groups()
            fields[current] = value
        elif line[:1].isspace() and current:
            fields[current] += " " + line.strip()
    return fields


def parse_installed(output):
    records = []
    for block in re.split(r"\n\s*\n", output.strip()):
        fields = parse_info(block)
        if "Name" not in fields:
            continue
        name = valid_name(fields["Name"])
        source = fields.get("Installed From", "").strip()
        if source in ("", "None"):
            source = "local/unknown"
        records.append({"id": f"installed/{name}", "name": name, "source": text(source),
                        "version": fields.get("Version", "?"), "description": fields.get("Description", ""),
                        "state": "installed", "kind": "installed", "extra": ""})
    return records


def aur_info(names):
    found = {}
    # Exact-name lookup, including split packages; never infer AUR from -bin/-git.
    for start in range(0, len(names), 100):
        batch = names[start:start + 100]
        url = "https://aur.archlinux.org/rpc/v5/info?" + urllib.parse.urlencode({"arg[]": batch}, doseq=True)
        with urllib.request.urlopen(url, timeout=12) as response:
            data = json.load(response)
        if not isinstance(data, dict) or data.get("type") != "multiinfo" or not isinstance(data.get("results"), list):
            raise Error("Invalid AUR info response")
        for item in data["results"]:
            if not isinstance(item, dict) or not isinstance(item.get("Name"), str):
                raise Error("Invalid AUR package record")
            name = valid_name(item["Name"])
            if name not in batch:
                raise Error("Unexpected AUR package record")
            found[name] = item
    return found


def installed_catalog():
    records = parse_installed(capture(["pacman", "-Qi"]))
    unresolved = [record for record in records if record["source"] == "local/unknown"]
    if not unresolved:
        return records
    # Local sync databases, in pacman repository priority order. No refresh.
    repositories = {}
    try:
        for line in capture(["pacman", "-Sl"]).splitlines():
            fields = line.split()
            if len(fields) >= 3:
                source, name, version = fields[:3]
                repositories.setdefault(valid_name(name), []).append((valid_name(source), version))
    except (Error, OSError) as error:
        # Without the repo index we cannot safely distinguish foreign packages.
        for record in unresolved:
            record["source_detail"] = "Source lookup unavailable: " + text(error)
        print("Source lookup unavailable: " + text(error), file=sys.stderr)
        return records
    foreign = []
    for record in unresolved:
        candidates = repositories.get(record["name"], [])
        if candidates:
            exact = [candidate for candidate in candidates if candidate[1] == record["version"]]
            record["source"] = (exact or candidates)[0][0]
            record["source_detail"] = ("Matching installed version in local sync database." if exact else
                                       "Currently available in local sync database (version differs).")
            record["source_detail"] += " Installation origin was not recorded; repository priority breaks ties."
        else:
            foreign.append(record)
    if not AUR_HELPER:
        return records
    try:
        available = aur_info([record["name"] for record in foreign])
    except (Error, OSError, ValueError) as error:
        for record in foreign:
            record["source_detail"] = "Not in configured repositories; AUR lookup unavailable: " + text(error)
        print("AUR source lookup unavailable: " + text(error), file=sys.stderr)
    else:
        for record in foreign:
            if record["name"] in available:
                record["source"] = "aur"
                record["source_detail"] = "Exact package name found in AUR; installation origin was not recorded."
            else:
                record["source_detail"] = "No recorded origin or exact name in configured repositories/AUR."
    return records


def catalog(mode, query=""):
    if mode == "repos":
        return parse_search(capture(["pacman", "-Ss", "."]), "repo")
    if mode == "installed":
        return installed_catalog()
    if not query.strip():
        return []
    if not AUR_HELPER:
        return []
    if HELPER_STYLE in ("aura", "custom"):
        return aur_search(query, set())
    # Search is read-only. No fallback mirror or alternative package is substituted.
    output = capture([AUR_HELPER, "-Ssa", "--color", "never", "--", query], timeout=30, empty_ok=True)
    return parse_search(output, "aur")


def theme_options():
    # Read only generated color declarations, never source/evaluate a shell file.
    path = HOME / ".config/fzf/themes/noctalia.sh"
    if not path.is_file():
        return []
    return [line.rstrip('"') for line in path.read_text().splitlines()
            if re.fullmatch(r'--color=[a-z+_-]+:#[0-9a-fA-F]{6}"?', line)]


def search_options():
    # --nth applies AFTER --with-nth; field 2 there is the full package name.
    return ["--ansi", "--exact", "--no-extended", "--ignore-case", "--no-sort", "--highlight-line",
            "--delimiter=\t", "--with-nth={3}\t{2}\t{4}", "--nth=2", "--tabstop=1", "--no-hscroll"]


def source_order(record):
    source = record["source"].casefold()
    group = 2 if source == "aur" or record["kind"] == "aur" else (1 if source.startswith("cachyos") else 0)
    return group, source, record["name"].casefold()


def aur_search(query, installed):
    url = "https://aur.archlinux.org/rpc/v5/search/" + urllib.parse.quote(query, safe="") + "?by=name"
    with urllib.request.urlopen(url, timeout=12) as response:
        data = json.load(response)
    if not isinstance(data, dict):
        raise Error("Invalid AUR response")
    if data.get("type") != "search" or not isinstance(data.get("results"), list):
        raise Error(text(data.get("error", "Invalid AUR response")))
    records = []
    for item in data["results"]:
        if not isinstance(item, dict) or not isinstance(item.get("Name"), str) or not isinstance(item.get("Version"), str):
            raise Error("Invalid AUR package record")
        name = valid_name(item["Name"])
        if query.casefold() in name.casefold():
            records.append({"id": "aur/" + name, "name": name, "source": "aur",
                            "version": text(item["Version"]), "description": text(item.get("Description") or ""),
                            "state": "installed" if name in installed else "", "kind": "aur", "extra": ""})
    return records


def merged_search(database, query):
    # fzf cancels this process when another character is typed; no helper daemon.
    time.sleep(0.3)
    directory = Path(database).parent
    lookup = json.loads(Path(database).read_text())
    status = "AUR: type at least 2 characters"
    if not AUR_HELPER:
        lookup = {key: record for key, record in lookup.items() if record["kind"] != "aur"}
        status = "Pacman only · AUR disabled"
    elif len(query.strip()) >= 2:
        try:
            installed = set(json.loads((directory / "installed.json").read_text()))
            aur = aur_search(query, installed)
            lookup.update({record["id"]: record for record in aur})
            status = f"AUR: {len(aur)} results"
        except (Error, OSError, ValueError, KeyError) as error:
            status = "AUR unavailable (local/cached results only): " + text(error)
    # Keep prior entries available for previews and selections during reload.
    with tempfile.NamedTemporaryFile(mode="w", dir=directory, delete=False) as output:
        json.dump(lookup, output)
        temporary = Path(output.name)
    temporary.replace(database)
    base = (directory / "header.txt").read_text()
    (directory / "status.txt").write_text(status + "\n" + base)
    print(picker_rows(sorted(lookup.values(), key=source_order)), flush=True)


def colored(value, color, bold=False, dim=False):
    red, green, blue = (int(color[index:index + 2], 16) for index in (1, 3, 5))
    style = "1;" if bold else ("2;" if dim else "")
    return f"\033[{style}38;2;{red};{green};{blue}m{value}\033[0m"


def source_color(source, kind, palette):
    source = source.casefold()
    if source in ("", "local/unknown"):
        return palette.get("fg", "#cdd6f4")
    if source == "aur" or kind == "aur":
        return "#f9e2af"
    if source.startswith("cachyos"):
        return palette.get("info", "#cba6f7")
    if source.split("-", 1)[0] in ("core", "extra", "multilib"):
        return palette.get("marker", "#a6e3a1")
    # Stable across searches and Python restarts, including previously unseen repos.
    hue = int.from_bytes(hashlib.sha256(source.encode()).digest()[:4], "big") / 2**32
    return "#" + "".join(f"{round(channel * 255):02x}"
                         for channel in colorsys.hls_to_rgb(hue, 0.74, 0.72))


def picker_rows(records, show_state=True):
    palette = dict(option.removeprefix("--color=").split(":", 1) for option in theme_options())
    foreground = palette.get("fg", "#cdd6f4")
    green = palette.get("marker", "#a6e3a1")
    source_colors = {source: source_color(source, kind, palette)
                     for source, kind in {(record["source"], record["kind"]) for record in records}}
    lines = []
    for record in records:
        name = text(record["name"])
        description = text(record.get("description", ""))
        source = text(record["source"])
        label = "AUR" if source.casefold() == "aur" or record["kind"] == "aur" else source
        state = "[Installed]" if record.get("state") == "installed" else ""
        state_column = colored(f"{state:<11}", green) + " " if show_state else ""
        details = (colored(f"{record.get('version', ''):<14.14}", foreground, dim=True) + " "
                   + state_column + colored(description, foreground, dim=True))
        if record["kind"] == "menu":
            details = colored(description, foreground, dim=True)
        # Keep the complete name searchable, even when it exceeds the column width.
        lines.append(record["id"] + "\t" + colored(name, foreground, bold=True) + "\t"
                     + colored(f"{label:<16}", source_colors[record["source"]], bold=True) + "\t"
                     + " " * max(0, 26 - len(name)) + details)
    return "\n".join(lines)


def clear_console():
    if sys.stdout.isatty():
        print("\033[2J\033[3J\033[H", end="", flush=True)


def resize_preview(path, delta):
    if delta not in (-5, 5):
        raise Error("Invalid preview resize step.")
    path = Path(path)
    size = max(25, min(75, int(path.read_text()) + delta))
    path.write_text(str(size))
    return f"change-preview-window(right,{size}%,wrap)+refresh-preview"


def preview_mode(database, mode):
    if mode not in ("details", "recipe"):
        raise Error("Invalid preview mode.")
    path = Path(database).parent / "preview-mode"
    path.write_text("summary" if path.read_text() == mode else mode)
    return "refresh-preview"


def picker(records, title, query="", multi=True, keys="", preview=True, searchable=True, merged=False, show_state=True):
    clear_console()
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    with tempfile.TemporaryDirectory(prefix="package-menu-", dir=runtime) as directory:
        database = Path(directory) / "catalog.json"
        database.write_text(json.dumps({record["id"]: record for record in records}))
        header = "Tab multi · Enter confirm · Esc back · Shift+←/→ resize\nCtrl+A all · Ctrl+D clear · Alt+P preview · Alt+T Traur audit" if multi else "↑/↓ choose · Enter open · Esc exit"
        if multi and preview:
            header += "\nAlt+D compact / full details · Alt+B AUR PKGBUILD"
        if keys:
            header += "\n" + keys
        if multi:
            header += f"\n{'SOURCE':<16} {'PACKAGE':<26} {'VERSION':<14}"
            if show_state:
                header += " STATE"
        args = ["fzf", "--layout=reverse", "--border=rounded", "--info=inline", "--no-separator", *search_options(), "--print-query",
                "--expect=alt-t", "--query=" + query,
                "--border-label= " + title + " ", "--header=" + header, *theme_options()]
        if searchable:
            args += ["--prompt=Search packages > "]
        else:
            args += ["--no-input", "--disabled", "--info=hidden", "--with-nth=2,4", "--nth=1"]
        if merged:
            (Path(directory) / "installed.json").write_text(json.dumps(capture(["pacman", "-Qq"]).splitlines()))
            (Path(directory) / "header.txt").write_text(header)
            (Path(directory) / "status.txt").write_text("AUR: type at least 2 characters\n" + header)
            command = "exec " + shlex.join(frontend_command("--search", str(database))) + " {q}"
            status = shlex.join(["cat", str(Path(directory) / "status.txt")])
            args += ["--id-nth=1", "--bind=start:reload-sync(" + command + "),change:reload-sync(" + command + ")",
                     "--bind=load:transform-header(" + status + ")"]
        if multi:
            args += [f"--multi={MAX_BATCH}", "--bind=ctrl-a:select-all,ctrl-d:deselect-all"]
        if preview:
            (Path(directory) / "preview-mode").write_text(PREVIEW_MODE)
            command = shlex.join(frontend_command("--preview", str(database))) + " {1}"
            size_file = Path(directory) / "preview-size"
            size_file.write_text("48")
            resize = shlex.join(frontend_command("--resize-preview", str(size_file)))
            mode = shlex.join(frontend_command("--preview-mode", str(database)))
            args += ["--preview=" + command, "--preview-window=right,48%,wrap", "--bind=alt-p:toggle-preview",
                     "--bind=alt-d:transform(" + mode + " details),alt-b:transform(" + mode + " recipe)",
                     "--bind=shift-left:transform(" + resize + " 5),shift-right:transform(" + resize + " -5)"]
        else:
            args += ["--preview-window=hidden"]
        selected = subprocess.run(args, input=picker_rows(records, show_state=show_state), text=True, stdout=subprocess.PIPE)
        if selected.returncode in (1, 130):
            return None
        if selected.returncode:
            raise Error("fzf could not open the package selector.")
        output = selected.stdout.splitlines()
        lookup = json.loads(database.read_text())
        chosen = [lookup[line.split("\t", 1)[0]] for line in output[2:]]
        return output[0], output[1], chosen


def preview_fields(fields):
    for label, value in fields:
        value = "  ".join(map(text, value)) if isinstance(value, list) else text(value if value is not None else "")
        print(textwrap.fill(value or "None", width=max(24, int(os.environ.get("FZF_PREVIEW_COLUMNS", "80"))),
                            initial_indent=f"{label:<16}: ", subsequent_indent=" " * 18, break_on_hyphens=False))


def preview_package_fields(fields, mode):
    """One field order and formatter for repository, AUR and installed packages."""
    order = ["Version", "Description", "URL", "Architecture", "Licenses", "Groups",
             "Provides", "Depends On", "Optional Deps", "Required By", "Optional For",
             "Conflicts With", "Replaces", "Download Size", "Installed Size", "Packager",
             "Maintainer", "Votes", "Popularity", "AUR version", "Status", "Make Deps",
             "Check Deps", "Package Base", "Keywords", "Build Date", "Install Date",
             "Install Reason", "Install Script", "Validated By", "Submitted", "Modified",
             "Package URL"]
    if mode != "details":
        order = ["Version", "Description", "Licenses", "Depends On", "Installed Size",
                 "Packager", "Required By", "Maintainer", "Votes", "Popularity", "AUR version", "Status"]
    selected = [(key, fields[key]) for key in order if key in fields]
    if mode == "details":
        selected += [(key, value) for key, value in fields.items()
                     if key not in order and key not in ("Name", "Repository", "Installed From")]
    preview_fields(selected)


def preview_aur(directory, record, mode, local_fields=None):
    name = valid_name(record["name"])
    metadata = directory / ("aur-" + name + ".json")
    try:
        if not metadata.exists():
            metadata.write_text(json.dumps(aur_info([name]).get(name)))
        item = json.loads(metadata.read_text())
        if not item:
            raise Error("Package no longer listed in AUR.")
        if mode == "recipe":
            base = valid_name(item.get("PackageBase", name))
            recipe = directory / ("recipe-" + base)
            if not recipe.exists():
                url = "https://aur.archlinux.org/cgit/aur.git/plain/PKGBUILD?" + urllib.parse.urlencode({"h": base})
                with urllib.request.urlopen(url, timeout=12) as response:
                    content = response.read(256 * 1024 + 1)
                if len(content) > 256 * 1024:
                    raise Error("PKGBUILD exceeds preview limit.")
                if content.lstrip().lower().startswith((b"<!doctype", b"<html")):
                    raise Error("AUR returned a web page, not a PKGBUILD.")
                recipe.write_bytes(content)
            print("Current AUR PKGBUILD · read-only · Alt+B back\n")
            content = re.sub(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]", "", recipe.read_text(errors="replace"))
            print(content)
            return
        fields = dict(local_fields or {})
        if record["kind"] != "installed":
            fields.update({"Version": item.get("Version", record["version"]),
                           "Description": item.get("Description") or record.get("description", "")})
            fields.update((label, item.get(key, [])) for label, key in (
                ("Licenses", "License"), ("Depends On", "Depends"), ("URL", "URL"),
                ("Groups", "Groups"), ("Provides", "Provides"), ("Optional Deps", "OptDepends"),
                ("Conflicts With", "Conflicts"), ("Replaces", "Replaces")))
        fields.update({"Maintainer": item.get("Maintainer") or "Orphaned",
                       "Votes": item.get("NumVotes", "?"), "Popularity": item.get("Popularity", "?")})
        if record["kind"] == "installed" and item.get("Version") and item["Version"] != record["version"]:
            fields["AUR version"] = item["Version"]
        if item.get("OutOfDate"):
            fields["Status"] = "Flagged out-of-date"
        if mode == "details":
            fields.update((label, item.get(key, [])) for label, key in (
                ("Make Deps", "MakeDepends"), ("Check Deps", "CheckDepends"),
                ("Package Base", "PackageBase"), ("Keywords", "Keywords")))
            fields["Package URL"] = "https://aur.archlinux.org/packages/" + name
            for label, key in (("Submitted", "FirstSubmitted"), ("Modified", "LastModified")):
                if item.get(key):
                    fields[label] = datetime.datetime.fromtimestamp(item[key], datetime.timezone.utc).isoformat(timespec="minutes")
        preview_package_fields(fields, mode)
    except (Error, OSError, ValueError, TypeError, OverflowError) as error:
        if local_fields:
            preview_package_fields(local_fields, mode)
        elif record["kind"] != "installed":
            print(text(record.get("description", "")))
        print("\nAUR preview unavailable:", text(error))


def preview(database, identifier):
    record = json.loads(Path(database).read_text()).get(identifier)
    if not record:
        raise Error("The selected package is no longer in this list.")
    print(text(record["name"]), flush=True)
    directory = Path(database).parent
    mode_file = directory / "preview-mode"
    mode = mode_file.read_text() if mode_file.exists() else PREVIEW_MODE
    if mode == "details" and record.get("source_detail"):
        print(text(record["source_detail"]))
    is_aur = record["source"].casefold() == "aur" or record["kind"] == "aur"
    fields = {}
    if record["kind"] != "aur" and not (is_aur and mode == "recipe"):
        operation = "-Qi" if record["kind"] == "installed" else "-Si"
        target = record["name"] if operation == "-Qi" else record["id"]
        info = capture(["pacman", operation, "--", target])
        fields = parse_info(info)
        fields.setdefault("Version", record["version"])
        fields.setdefault("Description", record.get("description", ""))
        if is_aur and fields.get("Packager") == "Unknown Packager":
            fields.pop("Packager")
        if not is_aur:
            preview_package_fields(fields, mode)
            sys.stdout.flush()
    if is_aur:
        preview_aur(directory, record, mode, fields)
    print("\nAlt+D " + ("compact view" if mode == "details" else "full details")
          + (" · Alt+B " + ("back" if mode == "recipe" else "PKGBUILD") if is_aur else ""))


def validate_selection(records):
    if not records:
        raise Error("No packages selected.")
    if len(records) > MAX_BATCH:
        raise Error(f"Select at most {MAX_BATCH} packages at a time; narrow the search first.")
    names = [valid_name(record["name"]) for record in records]
    if len(set(names)) != len(names):
        raise Error("Select only one source/version of each package.")
    return names


def orphan_names():
    # A single -t excludes packages still required, including optional deps.
    # pacman returns 1 without stderr when no orphaned dependencies exist.
    return {valid_name(name) for name in capture(["pacman", "-Qdtq"], empty_ok=True).splitlines() if name}


def remove_orphans():
    names = sorted(orphan_names())
    if names:
        code = subprocess.call(["sudo", "pacman", "-Rns", "--", *names])
        if code:
            raise Error(f"Package operation stopped (exit {code}). No fallback was attempted.")
    clear_console()


def transaction_command(action, records):
    names = validate_selection(records)
    if action == "install":
        kinds = {record["kind"] for record in records}
        if kinds == {"repo"}:
            targets = [valid_name(record["source"]) + "/" + name for record, name in zip(records, names)]
            return ["sudo", "pacman", "-S", "--needed", "--", *targets]
        if kinds == {"aur"}:
            if not AUR_HELPER:
                raise Error("AUR is disabled. Choose a helper in Settings.")
            if HELPER_STYLE == "aura":
                return with_wrapper([AUR_HELPER, "-A", "--", *names])
            if HELPER_STYLE == "custom":
                return with_wrapper([AUR_HELPER, *HELPER_INSTALL_ARGS, *names])
            return with_wrapper([AUR_HELPER, "-S", "--needed", "--aur", "--", *names])
        raise Error("Install one source category at a time.")
    if any(record["kind"] != "installed" for record in records):
        raise Error("Choose installed packages for this action.")
    if action == "remove":
        return ["sudo", "pacman", "-Rns", "--", *names]
    if action == "downgrade":
        # No cross-repository substitution; includes helper caches explicitly.
        args = ["sudo", "downgrade", "--cached-only", "--ignore=never"]
        # Explicit --pacman-cache replaces the tool's defaults, so retain all
        # configured system caches before adding the user's helper caches.
        caches = [Path(line) for line in capture(["pacman-conf", "CacheDir"]).splitlines() if line]
        for cache in [*caches, HOME / ".cache/paru/clone", HOME / ".cache/yay"]:
            if cache.is_dir():
                args += ["--pacman-cache=" + str(cache)]
        return [*args, *names]
    raise Error("Unknown package operation.")


def transaction_commands(action, records):
    validate_selection(records)
    if action == "install" and {record["kind"] for record in records} == {"repo", "aur"}:
        return [transaction_command(action, [record for record in records if record["kind"] == kind])
                for kind in ("repo", "aur")]
    return [transaction_command(action, records)]


def confirm(message):
    return input(message + " [y/N] ").strip().lower() == "y"


def perform(action, records):
    LAST_SKIPPED.clear()
    validate_selection(records)
    print("\n" + action.upper())
    for record in records:
        print(f"  {record['name']}   [{record['source']}]   {record['version']}")
    aur = [record for record in records if record["kind"] == "aur"] if action == "install" else []
    if aur:
        # Separate targets so a denied recipe/dependency cannot prevent unrelated
        # selected packages from proceeding. Never fake a successful makepkg run.
        repositories = [record for record in records if record["kind"] != "aur"]
        if repositories:
            run_transaction(transaction_command(action, repositories))
        for record in aur:
            run_aur_target(record)
        return
    commands = transaction_commands(action, records)
    if action == "remove":
        print("Pacman will also show any unused dependencies selected for removal.")
    elif action == "downgrade":
        print("Local cache only. No Arch archive fallback and no automatic IgnorePkg changes.")
        print("A downgrade can break dependencies; review pacman's transaction.")
    for command in commands:
        run_transaction(command)


def run_transaction(command, system_update=False):
    code = subprocess.call(command)
    if code:
        label = "System update" if system_update else "Package operation"
        raise Error(f"{label} stopped (exit {code}). No fallback was attempted.")


def skip_package(record, reason, approval_keys=(), preflight=False, updating=False):
    entry = {"name": valid_name(record["name"]), "reason": text(reason),
             "approval_keys": list(approval_keys), "preflight": preflight, "updating": updating}
    LAST_SKIPPED[:] = [item for item in LAST_SKIPPED if item["name"] != entry["name"]]
    LAST_SKIPPED.append(entry)
    print(f"\nSKIPPED {entry['name']}: {entry['reason']}\nReview this package before retrying. Continuing with other packages.", flush=True)


def skipped_summary():
    if not LAST_SKIPPED:
        return ""
    names = ", ".join(item["name"] for item in LAST_SKIPPED[:4])
    extra = f" (+{len(LAST_SKIPPED) - 4})" if len(LAST_SKIPPED) > 4 else ""
    return "Skipped AUR: " + names + extra + " · See Skipped packages in the main menu"


def run_aur_target(record, updating=False, force=False, approval_keys=()):
    command = transaction_command("install", [record])
    if updating and HELPER_STYLE == "pacman":
        # Candidates were selected by the helper's upgrade query, including VCS.
        # Do not let --needed suppress a VCS rebuild with an unchanged version.
        command.remove("--needed")
    if TRAUR_ENABLED and not WRAPPER_ENABLED and not force:
        try:
            audit([record], strict=True)
        except Error as error:
            skip_package(record, error, preflight=True, updating=updating)
            return False
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    with tempfile.TemporaryDirectory(prefix="package-menu-gate-", dir=runtime) as directory:
        events = Path(directory) / "events.jsonl"
        events.touch(mode=0o600)
        environment = dict(os.environ, PACKAGE_MENU_GATE_EVENT=str(events))
        environment.pop("PACKAGE_MENU_GATE_APPROVAL", None)
        if force and approval_keys:
            approvals = Path(directory) / "approvals.json"
            with open(approvals, "x", opener=lambda name, flags: os.open(name, flags, 0o600)) as output:
                json.dump({"version": 1, "keys": list(approval_keys)}, output)
            environment["PACKAGE_MENU_GATE_APPROVAL"] = str(approvals)
        code = subprocess.call(command, env=environment)
        if code < 0 or code in (130, 143):
            raise KeyboardInterrupt
        if events.stat().st_size > 65536:
            raise Error("Invalid wrapper event data. Operation stopped.")
        reasons = []
        blocked_keys = []
        try:
            for line in events.read_text().splitlines():
                event = json.loads(line)
                if (not isinstance(event, dict) or event.get("version") != 1 or event.get("blocked") is not True
                        or not isinstance(event.get("reason"), str)):
                    raise ValueError("invalid wrapper event")
                reasons.append(text(event["reason"]))
                key = event.get("approval_key")
                if key is not None:
                    if not isinstance(key, str) or not re.fullmatch(r"[a-f0-9]{64}", key):
                        raise ValueError("invalid approval key")
                    blocked_keys.append(key)
        except (ValueError, UnicodeError):
            raise Error("Invalid wrapper event data. Operation stopped.") from None
    if reasons:
        skip_package(record, "; ".join(dict.fromkeys(reasons)), blocked_keys, updating=updating)
        return False
    if code:
        raise Error(f"Package operation stopped (exit {code}). No fallback was attempted.")
    LAST_SKIPPED[:] = [item for item in LAST_SKIPPED if item["name"] != record["name"]]
    return True


def audit(records, strict=False):
    for record in records:
        if record["kind"] != "aur" and record["source"].casefold() != "aur":
            print("\nSkipping non-AUR package:", record["name"])
            continue
        print("\nTraur:", record["name"])
        try:
            command = ["traur", "scan", valid_name(record["name"]), "--json"]
            if strict:
                output, code = capture(command, timeout=90, accept_report=True, return_status=True)
            else:
                output, code = capture(command, timeout=90, accept_report=True), 0
            report = json.loads(output)
            if not isinstance(report, dict) or report.get("status") != "complete" or not isinstance(report.get("tier"), str) or not isinstance(report.get("signals"), list):
                raise Error("Incomplete or invalid Traur report.")
            if strict and (report.get("schema_version") != 2 or report.get("package") != record["name"]
                           or type(report.get("score")) is not int or not 0 <= report["score"] <= 100
                           or report["tier"] not in ("Trusted", "Ok", "Sketchy", "Suspicious", "Malicious") or code not in (0, 1)):
                raise Error("Incomplete or invalid Traur score.")
            print("Result:", text(report["tier"]), "—", text(report["status"]))
            if type(report.get("score")) is int:
                print("Trust score:", report["score"], "/ 100")
            for finding in report.get("signals", []):
                if isinstance(finding, dict) and finding.get("role") not in ("Fact", "Provenance"):
                    print(" ", text(finding.get("description", "")))
        except (Error, OSError, ValueError, KeyError) as error:
            print("NO VALID AUDIT RESULT:", text(error))
            if strict:
                raise Error("Traur could not complete the scan. Operation stopped.") from error
            continue
        if strict and (code != 0 or report["tier"] not in ("Trusted", "Ok") or report.get("override_gate_fired") is not None):
            raise Error("Traur blocked the operation: " + text(report["tier"]))
    if records:
        print("\nAUR report only; exact build-recipe checks require a makepkg wrapper.")


def aur_update_names():
    if HELPER_STYLE == "pacman":
        # Paru/yay expose a read-only AUR upgrade query, including devel checks.
        # Keep versioned output so ignored packages remain identifiable.
        output = capture([AUR_HELPER, "-Qua", "--color", "never"], timeout=60, empty_ok=True)
        names = []
        for line in output.splitlines():
            fields = line.split()
            if not fields:
                continue
            if len(fields) not in (4, 5) or fields[2] != "->" or (len(fields) == 5 and fields[4] != "[ignored]"):
                raise Error("Could not read the helper's AUR update list.")
            name = valid_name(fields[0])
            if len(fields) == 4:
                names.append(name)
        return list(dict.fromkeys(names))

    # Aura/custom helpers do not share Paru/yay's query syntax. Compare AUR
    # versions without running their update command or treating every foreign
    # package as an upgrade. vercmp handles Arch epochs and package releases.
    installed = {}
    for line in capture(["pacman", "-Qm"], empty_ok=True).splitlines():
        fields = line.split()
        if len(fields) != 2:
            raise Error("Could not read installed foreign package versions.")
        installed[valid_name(fields[0])] = fields[1]
    metadata = aur_info(list(installed)) if installed else {}
    names = []
    for name, version in installed.items():
        if name not in metadata:
            continue
        remote = metadata[name].get("Version")
        if not isinstance(remote, str) or not remote or any(character.isspace() for character in remote):
            raise Error("Invalid AUR update version.")
        comparison = capture(["vercmp", remote, version]).strip()
        if not re.fullmatch(r"-?\d+", comparison):
            raise Error("Could not compare AUR package versions.")
        if int(comparison) > 0:
            names.append(name)
    return names


def audit_update():
    if not AUR_HELPER or not TRAUR_ENABLED:
        return
    names = aur_update_names()
    if names:
        audit([{"name": name, "kind": "aur", "source": "aur"} for name in names], strict=True)


def update_system():
    LAST_SKIPPED.clear()
    if not AUR_HELPER or HELPER_STYLE == "custom":
        # An arbitrary custom update command cannot be safely split or rewritten.
        # Its native behavior remains intact; compatible helpers can select the
        # Paru/yay or Aura command style in Settings for managed AUR updates.
        commands = update_commands()
        audit_update()
        for command in commands:
            run_transaction(command, system_update=True)
        return
    repository_command = [AUR_HELPER, "-Syu"]
    if HELPER_STYLE == "pacman":
        repository_command.append("--repo")
    run_transaction(repository_command, system_update=True)
    names = aur_update_names()
    pending = set(names)
    for index, name in enumerate(names):
        if name not in pending:
            continue
        record = {"name": name, "source": "aur", "kind": "aur", "version": ""}
        if run_aur_target(record, updating=True) and index + 1 < len(names):
            # A successful target may have upgraded other targets as dependencies.
            # Re-query before attempting the next one instead of reinstalling it.
            pending = set(aur_update_names())


def file_digest(value):
    value = value.strip()
    if re.fullmatch(r"[a-fA-F0-9]{64}", value):
        return value.lower()
    path = Path(value).expanduser()
    if not path.is_file():
        raise Error("Enter a regular file path or a SHA-256 digest.")
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def save_vt_key(key):
    key = key.strip()
    if not 20 <= len(key) <= 200 or any(character.isspace() for character in key):
        raise Error("Invalid API key format.")
    VT_KEY.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", dir=VT_KEY.parent, delete=False) as temporary:
        temporary.write(key)
        temporary_path = Path(temporary.name)
    temporary_path.replace(VT_KEY)  # tempfile is owner-only (0600).


def vt_lookup(digest, key):
    if not re.fullmatch(r"[a-f0-9]{64}", digest):
        raise Error("Invalid SHA-256.")
    request = urllib.request.Request("https://www.virustotal.com/api/v3/files/" + digest,
                                     headers={"x-apikey": key, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return json.load(response)["data"]["attributes"]
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return None
        if error.code == 429:
            raise Error("VirusTotal rate limit reached. Try later; no verdict is available.") from None
        raise Error(f"VirusTotal request failed (HTTP {error.code}); no verdict is available.") from None
    except (urllib.error.URLError, TimeoutError, ValueError, KeyError):
        raise Error("Could not read a valid VirusTotal report; no verdict is available.") from None


def virustotal():
    print("\nVIRUSTOTAL — existing reports only; files are never uploaded.")
    value = input("File path or SHA-256 (empty = back): ").strip()
    if not value:
        return False
    digest = file_digest(value)
    link = "https://www.virustotal.com/gui/file/" + digest
    print("SHA-256:", digest)
    key = os.environ.get("VT_API_KEY", "").strip()
    if not key and VT_KEY.is_file():
        if VT_KEY.stat().st_mode & 0o077:
            raise Error("VT key file is not private (expected permissions 0600).")
        key = VT_KEY.read_text().strip()
    if not key and confirm("Add a VT API key for results in this terminal? (otherwise use browser)"):
        key = getpass.getpass("VT API key (hidden, stored owner-only): ").strip()
        save_vt_key(key)
    if key:
        attributes = vt_lookup(digest, key)
        if attributes is None:
            print("UNKNOWN: no existing report. This does NOT mean the file is safe.")
        else:
            stats = attributes.get("last_analysis_stats", {})
            if not stats:
                print("UNKNOWN: report contains no engine results.")
            else:
                print("Malicious:", stats.get("malicious", 0), " Suspicious:", stats.get("suspicious", 0),
                      " Undetected:", stats.get("undetected", 0))
                stamp = attributes.get("last_analysis_date")
                if stamp:
                    print("Last analysis:", datetime.datetime.fromtimestamp(stamp).astimezone().isoformat(timespec="minutes"))
                print("No detections is NOT a safety guarantee. This is the existing report, not a new scan.")
    print(link)
    if confirm("Open the report in your browser?"):
        subprocess.Popen(["xdg-open", link], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return True  # A real report/link was displayed and should remain readable.


def pause(destination="Packages"):
    input(f"\nEnter to return to {destination}…")
    clear_console()


def show_error(error):
    print("\nSTOP:", text(error))
    print("No automatic retry, alternative package source or security bypass was used.")


def browse(action):
    mode, query = ("repos" if action == "install" else "installed"), ""
    while True:
        print("Loading " + mode + " packages…", flush=True)
        records = sorted(catalog(mode, query), key=source_order)
        title = {"install": "Find & install", "remove": "Installed / uninstall", "downgrade": "Downgrade (cache only)"}[action]
        merged = action == "install" and bool(AUR_HELPER)
        hints = "Finish selections before changing the search" if merged else ""
        if LAST_SKIPPED:
            hints += "\n" + skipped_summary()
        suffix = (" — repos + AUR" if merged else " — repositories") if action == "install" else " — installed"
        selected = picker(records, title + suffix, query, keys=hints, merged=merged, show_state=action == "install")
        if selected is None:
            return
        query, key, chosen = selected
        if not chosen:
            continue
        try:
            if key == "alt-t":
                audit(chosen)
                pause(title)  # Keep explicit reports readable, then restore this list.
            elif key == "":
                perform(action, chosen)
                clear_console()
                # Reload this catalog, keeping its query and mode. Only Esc
                # leaves the list and returns to the main Packages menu.
        except (Error, OSError) as error:
            show_error(error)
            pause(title)


def menu_records(choices):
    return [{"id": key, "name": name, "source": "", "description": description, "kind": "menu"}
            for key, name, description in choices]


def helper_menu():
    settings = read_settings()
    choices = menu_records([
        ("auto", "Automatic", "Use installed paru, yay or Aura; otherwise pacman"),
        ("pacman", "Pacman only", "Repository packages; AUR disabled"),
        ("paru", "Paru", ""),
        ("yay", "Yay", ""),
        ("aura", "Aura 4+", ""),
        ("custom", "Custom helper", "Executable and command syntax"),
    ])
    selected = picker(choices, "Settings — AUR helper", multi=False, preview=False, searchable=False,
                      keys="Selected: " + text(settings["helper"]) + " · Active: " + text(AUR_HELPER or "pacman") + " · Esc back")
    if selected is None or not selected[2]:
        return
    helper = selected[2][0]["id"]
    settings["style"] = "auto"
    if helper == "custom":
        helper = input("Helper executable (empty = back): ").strip()
        if not helper:
            return
        helper = helper_executable(helper)
        styles = menu_records([
            ("pacman", "Paru / yay syntax", "-S --needed --aur; -Syu"),
            ("aura", "Aura 4+ syntax", "-A; -Syu then -Au"),
            ("custom", "Custom arguments", "Configure install and update arguments"),
        ])
        selected = picker(styles, "Helper command syntax", multi=False, preview=False, searchable=False)
        if selected is None or not selected[2]:
            return
        settings["style"] = selected[2][0]["id"]
        if settings["style"] == "custom":
            try:
                settings["install_args"] = shlex.split(input("AUR install arguments (targets appended): "))
                settings["update_args"] = shlex.split(input("Full system update arguments: "))
            except ValueError as error:
                raise Error("Invalid helper arguments: " + str(error)) from None
    settings["helper"] = helper
    settings["helper_enabled"] = helper != "pacman"
    save_settings(settings)
    activate_settings(settings)
    clear_console()


def settings_records(settings):
    def toggle(key, label):
        return (key, ("[x] " if settings[key] else "[ ] ") + label, "")

    choices = [toggle("helper_enabled", "Use AUR helper")]
    if settings["helper_enabled"]:
        choices.append(("helper", "AUR helper settings", text(settings["helper"])))
    choices += [toggle("traur_enabled", "Use Traur"), toggle("wrapper_enabled", "Use makepkg wrapper")]
    key_status = "Environment" if os.environ.get("VT_API_KEY", "").strip() else "Configured" if VT_KEY.is_file() else "Not set"
    choices += [("vt_key", "VirusTotal API key", key_status),
                ("preview_mode", "Package details", "Expanded" if settings["preview_mode"] == "details" else "Compact"),
                ("back", "Back", "")]
    return menu_records(choices)


def settings_menu():
    while True:
        settings = read_settings()
        selected = picker(settings_records(settings), "Settings", multi=False, preview=False, searchable=False,
                          keys="Active: " + text(AUR_HELPER or "pacman") + " · Enter change · Esc back")
        if selected is None or not selected[2] or selected[2][0]["id"] == "back":
            return
        action = selected[2][0]["id"]
        if action == "helper":
            helper_menu()
            continue
        if action in ("helper_enabled", "traur_enabled", "wrapper_enabled"):
            settings[action] = not settings[action]
        elif action == "vt_key":
            key = getpass.getpass("VirusTotal API key (hidden; empty = back): ").strip()
            if key:
                save_vt_key(key)
            continue
        elif action == "preview_mode":
            settings["preview_mode"] = "summary" if settings["preview_mode"] == "details" else "details"
        else:
            continue
        save_settings(settings)
        activate_settings(settings)


def main_records():
    choices = [
        ("install", "Find & install", "Repositories + AUR" if AUR_HELPER else "Repository packages"),
        ("remove", "Installed / uninstall", "List installed packages and select several to remove"),
        ("orphans", "Remove orphans", "Remove all unused dependencies; pacman confirms the transaction"),
        ("downgrade", "Downgrade", "Select previous versions from local caches"),
        ("vt", "VirusTotal", "Check a file/SHA-256; no upload"),
        ("update", "Update system", text(AUR_HELPER or "pacman")),
        ("settings", "Settings", "Helper, Traur, wrapper, VirusTotal and package details"),
        ("exit", "Exit", ""),
    ]
    if LAST_SKIPPED:
        choices.insert(-2, ("skipped", "Skipped packages", f"Review or install anyway · {len(LAST_SKIPPED)} skipped"))
    return menu_records(choices)


def skipped_menu():
    while LAST_SKIPPED:
        records = menu_records([(str(index), item["name"], item["reason"]) for index, item in enumerate(LAST_SKIPPED)])
        selected = picker(records, "Skipped AUR packages", multi=False, preview=False, searchable=False)
        if selected is None or not selected[2]:
            return
        item = LAST_SKIPPED[int(selected[2][0]["id"])]
        print("\nSKIPPED:", item["name"], "\n", item["reason"], flush=True)
        if not item["preflight"] and not item["approval_keys"]:
            print("This wrapper cannot authorize a scoped override. Fix the reported failure first.")
            pause()
            continue
        print("Installing anyway may execute malicious code. This does not add a permanent whitelist entry.")
        answer = input("Type " + item["name"] + " to install anyway once (empty = back): ").strip()
        if answer != item["name"]:
            continue
        record = {"name": item["name"], "source": "aur", "kind": "aur", "version": ""}
        run_aur_target(record, updating=item["updating"], force=True, approval_keys=item["approval_keys"])
        clear_console()


def main():
    while True:
        try:
            selected = picker(main_records(), "Packages", multi=False, preview=False, searchable=False,
                              keys=("Helper: " + text(AUR_HELPER) if AUR_HELPER else "Pacman only · AUR disabled")
                                   + ("\n" + skipped_summary() if LAST_SKIPPED else ""))
            if selected is None or not selected[2] or selected[2][0]["id"] == "exit":
                return
            action = selected[2][0]["id"]
            if action in ("install", "remove", "downgrade"):
                browse(action)
            elif action == "orphans":
                remove_orphans()
            elif action == "vt":
                if virustotal():
                    pause()
            elif action == "settings":
                settings_menu()
            elif action == "skipped":
                skipped_menu()
            elif action == "update":
                update_system()
                clear_console()
        except (Error, OSError) as error:
            show_error(error)
            pause()
        except (KeyboardInterrupt, EOFError):
            return


def run(argv):
    if argv == ["--version"]:
        print("package-menu " + (HERE.parent / "VERSION").read_text().strip())
        return
    if argv in (["--help"], ["-h"]):
        print("Usage: package-menu [--aur-helper EXECUTABLE|pacman] [--settings | --list repos|installed|aur [query]]")
        print("Choose a helper in Settings. Without a helper, repository actions use pacman.")
        print("--help, -h  Show this help\n--version   Show the installed version")
        return
    settings = read_settings()
    options = {"--aur-helper": "helper", "--helper-style": "style",
               "--helper-install-args": "install_args", "--helper-update-args": "update_args",
               "--runtime-settings": "runtime"}
    while argv and argv[0] in options:
        option = argv[0]
        if len(argv) < 2:
            raise Error(option + " requires a value.")
        key, value = options[option], argv[1]
        if key.endswith("_args") or key == "runtime":
            try:
                value = json.loads(value)
            except ValueError:
                raise Error(option + " requires a JSON argument list.") from None
        if key == "helper":
            settings["style"] = "auto"
            settings["helper_enabled"] = value != "pacman"
        if key == "runtime":
            if not isinstance(value, dict):
                raise Error("Invalid runtime settings.")
            settings.update(value)
        else:
            settings[key] = value
        argv = argv[2:]
    activate_settings(settings)
    if argv == ["--settings"]:
        settings_menu()
    elif len(argv) == 3 and argv[0] == "--preview":
        preview(argv[1], argv[2])
    elif len(argv) == 3 and argv[0] == "--preview-mode":
        print(preview_mode(argv[1], argv[2]))
    elif len(argv) == 3 and argv[0] == "--search":
        merged_search(argv[1], argv[2])
    elif len(argv) == 3 and argv[0] == "--resize-preview":
        print(resize_preview(argv[1], int(argv[2])))
    elif len(argv) >= 2 and argv[0] == "--list":
        print(json.dumps(catalog(argv[1], " ".join(argv[2:])), ensure_ascii=True))
    elif not argv:
        main()
    else:
        raise Error("Usage: package-menu [--aur-helper EXECUTABLE|pacman] [--settings | --list repos|installed|aur [query]]")


def cli():
    try:
        run(sys.argv[1:])
    except (Error, OSError) as error:
        print(text(error), file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    cli()
