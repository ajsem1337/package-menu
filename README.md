# package-menu

![Made with AI](https://img.shields.io/badge/Made%20with-AI-76ddcf)

A TUI for managing Arch Linux system packages, inspired by [Omarchy](https://omarchy.org).

## Install

Requires Python 3.11+ and fzf 0.74.5+.
Optional: `downgrade`, `traur`, `xdg-open`.

```sh
git clone https://github.com/ajsem1337/package-menu.git
cd package-menu
make install PREFIX="$HOME/.local"
```

Add `~/.local/bin` to PATH.
For a pacman-managed system installation, build the included package instead:

```sh
makepkg -si
```

**Update package-menu** installs the latest GitHub `main` code after confirmation
and restarts the app. It supports `make install` installations, not pacman-owned
files. To update a locally built pacman package, pull the checkout and run
`makepkg -si` again. A plain `pacman -Syu` cannot update it without a package repository.

## Usage

```sh
package-menu
```

Pacman is the default. **Settings** provides:

- **Use AUR helper**, with a choice of paru, yay, Aura 4+ or custom commands.
- **Use Traur** to show trust scores before AUR installs/updates.
- **Use makepkg wrapper** enables the bundled build-time guard (Paru/yay syntax).
- **VirusTotal API key**, entered privately.
- **Package details:** expanded or compact.

Suspicious AUR targets are skipped while unrelated packages continue. Review them
under **Skipped packages**; installing anyway requires typing the package name.
This grants one-operation approval, not a permanent whitelist entry.
The wrapper runs only through package-menu when enabled; it does not replace
the system `makepkg` or change global helper configuration.

Settings are saved in `~/.config/package-menu/config.json` (`XDG_CONFIG_HOME`
is respected). Without an available helper, AUR is disabled.
`--aur-helper yay` selects a helper for one launch.

- **Find & install:** search and install repository/AUR packages.
- **Installed / uninstall:** browse and remove installed packages.
- **Remove orphans:** remove unused dependencies.
- **Downgrade:** restore versions from local caches.
- **Update system:** update using the selected helper or pacman.
- **VirusTotal:** look up an existing report by file hash.

| Key | Action |
| --- | --- |
| Enter / Esc | Confirm / back |
| Tab / Shift+Tab | Select / unselect packages |
| Ctrl+A | Update system in the main menu / select all matches in package lists |
| Ctrl+D | Clear package selection |
| Ctrl+O | Open Settings |
| Alt+P / Alt+D | Toggle preview / full details |
| Alt+B | View AUR PKGBUILD |
| Alt+T | Traur report |
| Shift+Left/Right | Resize the package split view |
| Shift+Up/Down | Scroll preview |

The right-hand pane holds shortcuts and status messages. Package lists also show
package details there.

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
