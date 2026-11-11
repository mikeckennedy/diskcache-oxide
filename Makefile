PY ?= venv/bin/python

.PHONY: docs docs-serve check test wheels wheels-local

docs:
	$(PY) scripts/build_docs.py

docs-serve:
	$(PY) scripts/serve_docs.py

test:
	$(PY) -m pytest

check:
	./scripts/check.sh

# PGO release wheels into dist/, niced so the machine stays usable (profile counts do not depend on timing).
wheels:
	nice -n 20 ./scripts/build_pgo.sh 3.14 3.14t 3.15 3.15t

wheels-local:
	nice -n 20 ./scripts/build_pgo.sh 3.15
