PYTHON := .venv/bin/python

.PHONY: test test-gui build

test:
	$(PYTHON) -m pytest

test-gui:
	QT_QPA_PLATFORM=offscreen $(PYTHON) -m pytest -m gui

build:
	$(PYTHON) -m PyInstaller -y "EnergyFlow Studio.spec"
