# package-menu

![Made with AI](https://img.shields.io/badge/Made%20with-AI-76ddcf)

A TUI for managing Arch Linux system packages, inspired by [Omarchy](https://omarchy.org).

## Install

Requires Python 3.11+, fzf 0.74.5+, pacman, sudo and an AUR helper (`paru` or `yay`).
Optional: `downgrade`, `traur`, `xdg-open`.

```sh
git clone https://github.com/ajsem1337/package-menu.git
cd package-menu
make install PREFIX="$HOME/.local"
```

Add `~/.local/bin` to PATH. To update, pull the repository and run `make install` again.

## Usage

```sh
package-menu
package-menu --aur-helper yay
```

- **Find & install:** search and install repository/AUR packages.
- **Installed / uninstall:** browse and remove installed packages.
- **Remove orphans:** remove unused dependencies.
- **Downgrade:** restore versions from local caches.
- **Update system:** update using the selected AUR helper.
- **VirusTotal:** look up an existing report by file hash.

| Key | Action |
| --- | --- |
| Enter / Esc | Confirm / back |
| Tab / Shift+Tab | Select / unselect packages |
| Ctrl+A / Ctrl+D | Select matches / clear selection |
| Alt+P / Alt+D | Toggle preview / full details |
| Alt+B | View AUR PKGBUILD |
| Alt+T | Traur report |
| Shift+Left/Right | Resize preview |
| Shift+Up/Down | Scroll preview |

VirusTotal can prompt for an API key, saved in `~/.config/package-menu/virustotal.key`
with permissions `0600`. `VT_API_KEY` overrides it. Files are never uploaded.

See [operations and privacy](OPERATIONS.md) for commands and data access.

## Development

```sh
make test
python3 -I app.py
```

Installation supports `PREFIX` and `DESTDIR`.

MIT — [LICENSE](LICENSE) · [Omarchy attribution](LICENSE-OMARCHY).
