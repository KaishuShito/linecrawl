PREFIX ?= $(HOME)/.local
BIN_DIR := $(PREFIX)/bin

.PHONY: install-local test smoke

install-local:
	mkdir -p "$(BIN_DIR)"
	ln -sfn "$(CURDIR)/linecrawl.py" "$(BIN_DIR)/linecrawl"
	chmod +x "$(CURDIR)/linecrawl.py"

test:
	python3 -m unittest discover -s tests -p 'test_*.py'

smoke:
	command -v linecrawl
	linecrawl --help
	linecrawl --json doctor
