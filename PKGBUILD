pkgname=package-menu-git
pkgver=1.3.3
pkgrel=1
pkgdesc='TUI for managing Arch Linux system packages'
arch=('any')
url='https://github.com/ajsem1337/package-menu'
license=('MIT')
depends=('python>=3.11' 'fzf>=0.74.5' 'pacman')
makedepends=('git' 'make')
optdepends=('downgrade: restore package versions from local caches'
            'traur: scan AUR packages and build recipes'
            'xdg-utils: open VirusTotal reports')
provides=('package-menu')
conflicts=('package-menu')
source=('package-menu::git+https://github.com/ajsem1337/package-menu.git#branch=main')
sha256sums=('SKIP')

pkgver() {
  cd "$srcdir/package-menu"
  printf '%s.r%s.g%s' "$(cat VERSION)" "$(git rev-list --count HEAD)" "$(git rev-parse --short HEAD)"
}

package() {
  cd "$srcdir/package-menu"
  make DESTDIR="$pkgdir" PREFIX=/usr install
}
