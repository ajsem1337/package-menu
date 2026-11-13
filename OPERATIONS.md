# Operations and privacy

## Commands

| Action | Command |
| --- | --- |
| Repository install | `sudo pacman -S --needed -- repo/package ...` |
| AUR install | `HELPER -S --needed --aur -- package ...` |
| Remove | `sudo pacman -Rns -- package ...` |
| Orphan cleanup | `pacman -Qdtq`, then `sudo pacman -Rns --` with the results |
| Update | `HELPER -Syu` |
| Downgrade | `sudo downgrade --cached-only --ignore=never ...` |
| AUR report | `traur scan NAME --json` |

Mixed installs run repository packages before AUR. Downgrades use pacman and
paru/yay caches. Package managers handle transaction confirmation and sudo
authentication. Traur reports are manual; existing helper/makepkg gates are unchanged.

## Network and files

- AUR RPC supplies search results and metadata. PKGBUILD previews use AUR cgit;
  previews are displayed as text, not executed.
- Package managers and Traur use their normal upstreams.
- VirusTotal receives a SHA-256 and API key to retrieve an existing report.
  Files are never uploaded. Missing reports or zero detections do not prove safety.
- The optional key is stored in `~/.config/package-menu/virustotal.key` (0600).
- Preview/selector caches use private temporary files, removed on exit.
- Fzf theme settings are read as data. Returning from an action clears terminal
  output/scrollback; external tools keep their own logs.

Python starts fzf, its preview/search callbacks, `cat`, package queries and the
commands above. Transactions use argument arrays; fzf callbacks use shell-escaped
arguments. `pacman-conf` supplies cache directories.
