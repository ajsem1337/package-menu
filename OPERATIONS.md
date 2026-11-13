# Operations and privacy

## Commands

| Action | Command |
| --- | --- |
| Repository install | `sudo pacman -S --needed -- repo/package ...` |
| AUR install (paru/yay) | `HELPER -S --needed --aur -- package ...` |
| AUR install (Aura 4+) | `aura -A -- package ...` |
| Remove | `sudo pacman -Rns -- package ...` |
| Orphan cleanup | `pacman -Qdtq`, then `sudo pacman -Rns --` with the results |
| Repository update (paru/yay) | `HELPER -Syu --repo` |
| Repository update (Aura 4+) | `aura -Syu` |
| AUR update (paru/yay) | Query upgrades, then `HELPER -S --aur -- package` per target |
| AUR update (Aura 4+) | Compare versions, then `aura -A -- package` per target |
| Update (no helper) | `sudo pacman -Syu` |
| Downgrade | `sudo downgrade --cached-only --ignore=never ...` |
| AUR report | `traur scan NAME --json` |

Mixed installs run repository packages before AUR. Downgrades use pacman and
paru/yay caches. Package managers handle transaction confirmation and sudo
authentication. Repository updates finish independently before the AUR phase.
AUR targets run separately: denied recipes/dependencies skip the affected target,
not unrelated targets. Packages already upgraded as dependencies are not reinstalled.

Enabling Traur scans selected AUR installs and AUR update candidates
(Traur JSON schema 2), not every installed AUR package. With an explicit wrapper,
the wrapper performs the checks instead of a duplicate remote preflight. Paru/yay candidates come
from `HELPER -Qua --color never`; Aura/custom use AUR version comparisons with
`vercmp`. No AUR upgrades means no Traur scans. Incomplete or flagged reports skip
the target by default. Remote preflights do not inspect every build dependency or replace a build-time gate.
Aura/custom version checks do not predict VCS rebuilds or custom helper plans.
Custom helpers use the configured install/update argument lists. Missing helpers
select pacman-only mode before an operation. Generic custom full-update commands
retain their native behavior; select Paru/yay or Aura syntax for managed per-target updates.
Ordinary helper failures and cancellations stop the operation, without retries.

The optional wrapper passes the configured executable through Paru/yay's
`--makepkg` option. Aura/custom command syntax is rejected when that option is
enabled, rather than silently running unprotected. The wrapper must be installed
separately. Existing system/helper gates are never modified or disabled by the TUI.

Compatible wrappers append JSON-lines deny events to `PACKAGE_MENU_GATE_EVENT`:
`{"version":1,"blocked":true,"reason":"...","approval_key":"optional SHA-256"}`.
Unknown failures without a valid event are not treated as safely skipped packages.
The local guard emits approval keys bound to the recipe directory, input fingerprint
and report. **Skipped packages** requires typing the target name before passing
selected keys in a private `PACKAGE_MENU_GATE_APPROVAL` JSON file for one helper call.
Changed inputs/reports are denied again. Neither this file nor the deny-event file
survives the call; no whitelist or persistent IgnorePkg entry is written. Other
wrappers need the same protocol to support this scoped override.

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
