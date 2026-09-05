.PHONY: test test-python test-shell test-install test-ui test-corpus install uninstall

test: test-python test-shell test-install

test-python:
	python3 -m unittest discover -s tests -v

test-shell:
	sh -n install.sh uninstall.sh tests/test_install.sh tests/run-ui.sh

test-install:
	./tests/test_install.sh

test-ui:
	./tests/run-ui.sh python3 tests/smoke_app.py
	./tests/run-ui.sh python3 tests/smoke_renderer_contract.py
	./tests/run-ui.sh python3 tests/smoke_images.py
	./tests/run-ui.sh python3 tests/smoke_navigation.py
	./tests/run-ui.sh python3 tests/smoke_html.py
	./tests/run-ui.sh python3 tests/smoke_xlsx.py
	./tests/run-ui.sh python3 tests/smoke_pdf.py
	./tests/run-ui.sh python3 tests/smoke_media.py
	./tests/run-ui.sh python3 tests/smoke_text.py

test-corpus:
	test -n "$(CR2_SAMPLE_DIR)"
	CR2_SAMPLE_DIR="$(CR2_SAMPLE_DIR)" python3 -m unittest discover -s tests -v

install:
	./install.sh

uninstall:
	./uninstall.sh
