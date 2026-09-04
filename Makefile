.PHONY: test test-python test-shell test-js test-install test-ui test-corpus install uninstall

test: test-python test-shell test-js test-install

test-python:
	python3 -m unittest discover -s tests -v

test-shell:
	sh -n install.sh uninstall.sh tests/test_install.sh

test-js:
	gjs -c 'const GLib = imports.gi.GLib; const ByteArray = imports.byteArray; const [ok, contents] = GLib.file_get_contents(ARGV[0]); if (!ok) throw new Error("read failed"); new Function(ByteArray.toString(contents));' viewers/kukni.js

test-install:
	./tests/test_install.sh

test-ui:
	dbus-run-session -- xvfb-run -a -s '-screen 0 1280x800x24' python3 tests/smoke_app.py

test-corpus:
	test -n "$(CR2_SAMPLE_DIR)"
	CR2_SAMPLE_DIR="$(CR2_SAMPLE_DIR)" python3 -m unittest discover -s tests -v

install:
	./install.sh

uninstall:
	./uninstall.sh
