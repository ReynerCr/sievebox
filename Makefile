PYTHONPATH := src
PYTHON := python3
PYTEST := $(PYTHON) -m pytest

# Hermetic environment: tests must set every variable they read, so the
# suite passes identically on any machine. Filesystem state is NOT isolated.
# Tests touching /dev, /tmp/.X11-unix, etc. must patch existence themselves.
HERMETIC := env -i HOME=/tmp/sievebox-test-home TMPDIR=/tmp \
            PATH=/usr/local/bin:/usr/bin:/bin LANG=C.UTF-8

.PHONY: test lint clean help

help:
	@echo "sievebox development tasks:"
	@echo "  make test    Run the test suite (hermetic environment)"
	@echo "  make lint    Syntax-check all Python files"
	@echo "  make clean   Remove caches and temp files"

test:
	mkdir -p /tmp/sievebox-test-home
	PYTHONPATH=$(PYTHONPATH) $(HERMETIC) $(PYTEST) tests/

lint:
	@$(PYTHON) -c "import ast,sys; [ast.parse(open(f).read()) for f in sys.argv[1:]] and print('python syntax OK')" \
	  src/sievebox/*.py

clean:
	rm -rf .pytest_cache __pycache__ src/**/__pycache__ tests/__pycache__
