PYTHON ?= python3
# Apple's Command Line Tools python is the 3.9 floor; test against it too.
FLOOR_PYTHON ?= /usr/bin/python3
BINDIR ?= $(HOME)/.local/bin

.PHONY: test install uninstall

test:
	$(PYTHON) -m unittest discover -s tests -t .
	@if [ -x "$(FLOOR_PYTHON)" ]; then \
		echo "$(FLOOR_PYTHON) -m unittest discover -s tests -t ."; \
		"$(FLOOR_PYTHON)" -m unittest discover -s tests -t .; \
	else \
		echo "skipping $(FLOOR_PYTHON): not present"; \
	fi

install:
	mkdir -p "$(BINDIR)"
	ln -sfn "$(CURDIR)/bin/tandem" "$(BINDIR)/tandem"

# Only removes the link if it is ours, so a different tandem on PATH survives.
uninstall:
	@if [ "$$(readlink "$(BINDIR)/tandem")" = "$(CURDIR)/bin/tandem" ]; then \
		rm "$(BINDIR)/tandem" && echo "removed $(BINDIR)/tandem"; \
	else \
		echo "$(BINDIR)/tandem is not a link to this checkout; left alone"; \
	fi
