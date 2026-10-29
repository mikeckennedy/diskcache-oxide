PY ?= venv/bin/python

.PHONY: docs docs-serve check test

docs:
	$(PY) scripts/build_docs.py

docs-serve:
	$(PY) scripts/serve_docs.py

test:
	$(PY) -m pytest

check:
	./scripts/check.sh
