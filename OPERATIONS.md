# Operations and privacy

## Commands

| Action | Command |
| --- | --- |
| Repository install | `sudo pacman -S --needed -- repo/package ...` |
| AUR install (paru/yay) | `HELPER -S --needed --aur -- package ...` |
| AUR install (Aura 4+) | `aura -A -- package ...` |
| Remove | `sudo pacman -Rns -- package ...` |
| Orphan cleanup | `pacman -Qdtq`, then `sudo pacman -Rns --` with the results |
| Update (paru/yay) | `HELPER -Syu` |
| Update (Aura 4+) | `aura -Syu`, then `aura -Au` |
| Update (no helper) | `sudo pacman -Syu` |
| Downgrade | `sudo downgrade --cached-only --ignore=never ...` |
| AUR report | `traur scan NAME --json` |

Mixed installs run repository packages before AUR. Downgrades use pacman and
paru/yay caches. Package managers handle transaction confirmation and sudo
authentication. Enabling Traur scans selected/installed AUR packages and displays
their scores before installs/updates (Traur JSON schema 2). Incomplete or high-risk reports stop the
operation. It does not inspect every build dependency or replace a build-time gate.
Custom helpers use the configured install/update argument lists. Missing helpers
select pacman-only mode before an operation; failed operations are not retried.

The optional wrapper passes the configured executable through Paru/yay's
`--makepkg` option. Aura/custom command syntax is rejected when that option is
enabled, rather than silently running unprotected. The wrapper must be installed
separately. Existing system/helper gates are never modified or disabled by the TUI.

## Network and files

- AUR RPC supplies search results and metadata. PKGBUILD previews use AUR cgit;
  previews are displayed as text, not executed.
- Package managers and Traur use their normal upstreams.
- VirusTotal receives a SHA-256 and API key to retrieve an existing report.
  Files are never uploaded. Missing reports or zero detections do not prove safety.
- The optional key is stored in `~/.config/package-menu/virustotal.key` (0600).
- Helper settings are stored in `$XDG_CONFIG_HOME/package-menu/config.json`,
  defaulting to `~/.config/package-menu/config.json`.
- Preview/selector caches use private temporary files, removed on exit.
- Fzf theme settings are read as data. Returning from an action clears terminal
  output/scrollback; external tools keep their own logs.

Python starts fzf, its preview/search callbacks, `cat`, package queries and the
commands above. Transactions use argument arrays; fzf callbacks use shell-escaped
arguments. `pacman-conf` supplies cache directories.
