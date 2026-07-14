PYTHONPATH := src
PYTHON := python3
PYTEST := $(PYTHON) -m pytest

.PHONY: test lint clean help

help:
	@echo "sievebox development tasks:"
	@echo "  make test    Run the test suite"
	@echo "  make lint    Syntax-check all Python files"
	@echo "  make clean   Remove caches and temp files"

test:
	PYTHONPATH=$(PYTHONPATH) $(PYTEST) tests/ -v

lint:
	@$(PYTHON) -c "import ast,sys; [ast.parse(open(f).read()) for f in sys.argv[1:]] and print('python syntax OK')" \
	  src/sievebox/*.py

clean:
	rm -rf .pytest_cache __pycache__ src/**/__pycache__ tests/__pycache__
