PREFIX ?= /usr/local
DESTDIR ?=
PYTHON ?= python3

.PHONY: install test run

install:
	install -Dm644 app.py "$(DESTDIR)$(PREFIX)/share/package-menu/app.py"
	install -Dm644 guard.py "$(DESTDIR)$(PREFIX)/share/package-menu/guard.py"
	install -Dm755 makepkg-wrapper "$(DESTDIR)$(PREFIX)/share/package-menu/makepkg-wrapper"
	install -Dm644 VERSION "$(DESTDIR)$(PREFIX)/share/package-menu/VERSION"
	install -Dm644 LICENSE "$(DESTDIR)$(PREFIX)/share/licenses/package-menu/LICENSE"
	install -Dm644 LICENSE-OMARCHY "$(DESTDIR)$(PREFIX)/share/licenses/package-menu/LICENSE-OMARCHY"
	install -Dm755 bin/package-menu "$(DESTDIR)$(PREFIX)/bin/package-menu"

test:
	$(PYTHON) -B -m unittest discover -s tests -v

run:
	$(PYTHON) -I app.py
