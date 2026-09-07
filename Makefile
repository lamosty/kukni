.DEFAULT_GOAL := help

.PHONY: help dev check package test test-python test-shell test-install test-ui test-installed test-corpus install uninstall

# @security GNU Make normally exports command-line variables and expands their
# contents while constructing a recipe environment. Keep raw FILE out of that
# path, escape Make's `$` trigger plus `%`, and let the Python launcher decode
# it without a shell. Quotes, spaces and Make/shell syntax remain literal.
unexport FILE
dev: export KUKNI_MAKE_DEVELOPMENT_FILE := $(subst $$,%24,$(subst %,%25,$(value FILE)))
dev:
	./bin/kukni --development

help:
	@printf '%s\n' \
		'Kukni developer commands:' \
		'  make dev                    Run this checkout without installing' \
		'  make dev FILE=/path/image    Preview one file from this checkout' \
		'  make check                  Check the installed Kukni runtime' \
		'  make package                Build a traceable .deb in dist/' \
		'  make test                   Run the headless test suites' \
		'  make test-ui                Run isolated display/session UI smoke tests' \
		'  make test-installed         Test packaged activation in an isolated session'

check:
	/usr/bin/kukni --check

package:
	python3 packaging/build-deb.py

test: test-python test-shell test-install

test-python:
	python3 -m unittest discover -s tests -v

test-shell:
	sh -n install.sh uninstall.sh tests/test_install.sh tests/run-ui.sh

test-install:
	./tests/test_install.sh

test-ui:
	./tests/run-ui.sh python3 tests/smoke_development.py
	./tests/run-ui.sh python3 tests/smoke_app.py
	./tests/run-ui.sh python3 tests/smoke_renderer_contract.py
	./tests/run-ui.sh python3 tests/smoke_images.py
	./tests/run-ui.sh python3 tests/smoke_navigation.py
	./tests/run-ui.sh python3 tests/smoke_html.py
	./tests/run-ui.sh python3 tests/smoke_xlsx.py
	./tests/run-ui.sh python3 tests/smoke_pdf.py
	./tests/run-ui.sh python3 tests/smoke_media.py
	./tests/run-ui.sh python3 tests/smoke_text.py

# @constraint This separate gate requires the Ubuntu package to be installed.
# Its synthetic activation session never uses the user's D-Bus registrations.
test-installed:
	./tests/run-ui.sh python3 tests/smoke_installed_activation.py
	./tests/run-ui.sh timeout --kill-after=2s 20s /usr/bin/kukni --check-html

test-corpus:
	test -n "$(CR2_SAMPLE_DIR)"
	CR2_SAMPLE_DIR="$(CR2_SAMPLE_DIR)" python3 -m unittest discover -s tests -v

install:
	./install.sh

uninstall:
	./uninstall.sh
