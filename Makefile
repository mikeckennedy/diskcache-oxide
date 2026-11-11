PY ?= venv/bin/python

.PHONY: docs docs-serve check test wheels wheels-mac wheels-linux wheels-local

docs:
	$(PY) scripts/build_docs.py

docs-serve:
	$(PY) scripts/serve_docs.py

test:
	$(PY) -m pytest

check:
	./scripts/check.sh

# PGO release wheels into dist/, niced so the machine stays usable (profile counts do not depend on timing).
wheels: wheels-mac wheels-linux

wheels-mac:
	nice -n 20 ./scripts/build_pgo.sh 3.14 3.14t 3.15 3.15t

# manylinux_2_28 wheels for aarch64 (native) and x86_64 (Rosetta), in Docker; the script nices inside the container.
wheels-linux:
	./scripts/build_linux.sh

wheels-local:
	nice -n 20 ./scripts/build_pgo.sh 3.15
