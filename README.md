# package-menu

![Made with AI](https://img.shields.io/badge/Made%20with-AI-76ddcf)

A standalone fzf TUI for Arch Linux packages. Search repositories and AUR,
install/uninstall, clean up orphans, downgrade from local caches and update the
system. Includes manual Traur reports and VirusTotal lookups of existing reports.

Extracted from [System Menu](https://github.com/ajsem1337/system-menu), retaining
the package frontend's Git history. No Noctalia dependency, daemon or widget is
needed. System Menu can launch it just like any other TUI; it is never required.

## Requirements

- Arch Linux or a compatible derivative with `pacman`, `pacman-conf` and `sudo`.
- Python 3.11+, `fzf` 0.74.5+, and a terminal.
- A compatible AUR helper: `paru` by default, or `yay` / a custom executable
  selected using `--aur-helper EXECUTABLE`.
- Optional: `downgrade` for cached versions, `traur` for manual AUR reports,
  `xdg-open` for opening VirusTotal links.
- `git`, `make` and `install` for the installation commands below.

Dependencies are installed by you. No services, makepkg wrappers or security
scanner settings are installed or changed. Existing helper/build gates remain
independent; a manual report is not an installation approval.

## Install

```sh
git clone https://github.com/ajsem1337/package-menu.git
cd package-menu
make install PREFIX="$HOME/.local"
```

Ensure `~/.local/bin` is on PATH, then run:

```sh
package-menu
package-menu --aur-helper yay
package-menu --version
```

For a system install or packaging, `PREFIX=/usr` and `DESTDIR` are supported.
The executable lives in `PREFIX/bin`; frontend/version in
`PREFIX/share/package-menu`; licenses in `PREFIX/share/licenses/package-menu`.
To update, pull this repository and run the same `make install` again. Updating
the Noctalia widget does not update this application.

Without installing, run `python3 -I app.py` from this repository in a terminal.

### Launch from System Menu (optional)

In System Menu 2.x settings, choose Packages mode **In configured terminal**,
executable `package-menu`, and optional argument items `--aur-helper`, `yay`.
Configure the terminal and its run-program arguments there. You can instead use
Pacseek, Pacsea, GNOME Software, Shelly or another application in that widget.

## Usage

- **Find & install:** repositories plus AUR, grouped alphabetically by source.
- **Installed / uninstall:** installed packages, with multi-selection removal.
- **Remove orphans:** immediately runs `sudo pacman -Rns --` with all results
  of `pacman -Qdtq`, without a selection submenu. Empty results skip removal.
- **Downgrade:** choose previous versions from local caches only.
- **Update system:** the selected helper's normal `-Syu`.
- **VirusTotal:** hash a local file or enter SHA-256, retrieve existing reports;
  files are never uploaded.

| Key | Action |
| --- | --- |
| Enter / Esc | Confirm / back |
| Tab / Shift+Tab | Select / unselect, at most 50 packages |
| Ctrl+A / Ctrl+D | Select matches / clear selection |
| Alt+P | Toggle preview |
| Alt+D | Toggle compact / full details |
| Alt+B | Current AUR PKGBUILD, read-only; press again to return |
| Alt+T | Manual Traur report, without installing |
| Shift+Left/Right | Resize preview |
| Shift+Up/Down | Scroll preview |

Search matches names only. AUR joins after two characters; AUR sources are last.
Finish selections before changing the query. Full details are the default in
both Find & install and Installed / uninstall, with a shared field formatter.
Installed metadata comes from `pacman -Qi`; current AUR statistics come from RPC.
Installed-source fallback uses local repositories then exact-name AUR metadata,
not proven installation provenance. Votes/popularity are not security ratings.

Successful operations return immediately to the same refreshed list/query,
without Enter. Update and orphan cleanup return to the main menu. Failures remain
visible until Enter, then return without retrying or switching package sources.
Only the package manager's own transaction confirmation and sudo authentication
remain; there is no duplicate app-level yes/no. Explicit reports wait to be read.

### AUR helper

The helper must support `-S --needed --aur -- PACKAGE`,
`-Ssa --color never -- QUERY`, and `-Syu`. Pass its executable separately from
arguments; paths containing spaces remain one argument. No aliases or shell
expansion. Existing helper/makepkg settings are preserved.

### VirusTotal API key

Open VirusTotal, enter a file/SHA-256 and answer **y** to adding a key for terminal
results. Paste your own [API key](https://www.virustotal.com/gui/my-apikey) into
the hidden prompt. It is stored at `~/.config/package-menu/virustotal.key` with
permissions `0600`. `VT_API_KEY` takes precedence. This path is unchanged from
the former bundled frontend; no key is migrated or copied into this repository.
Without a key, open the report in a browser. Empty input returns immediately.
Unknown reports, lookup failures and zero detections are not safety guarantees.

See [operations and privacy](OPERATIONS.md) for commands and data access.

## Development

```sh
make test
git diff --check
```

Tests mock transactions/network; some exercise real fzf filters. Installation
tests stage files in a temporary prefix, never remove/install system packages.

## Credits

MIT — [LICENSE](LICENSE). The fzf multi-selection and package-info workflow is
adapted from Omarchy; its [MIT notice](LICENSE-OMARCHY) is retained.

## AI assistance

**Made with AI.** Developed with AI assistance, including code, documentation and tests.
