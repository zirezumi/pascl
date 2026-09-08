UV ?= uv
PY := .venv/bin/python

.PHONY: setup lint format typecheck test check docker clean

setup:
	$(UV) venv .venv --python 3.12
	$(UV) pip install --python $(PY) -e ".[dev]"

lint:
	$(PY) -m ruff check .
	$(PY) -m ruff format --check .

format:
	$(PY) -m ruff format .
	$(PY) -m ruff check --fix .

typecheck:
	$(PY) -m mypy

test:
	$(PY) -m pytest

check: lint typecheck test

docker:
	docker build -t pascl:dev .
	docker run --rm pascl:dev --version

clean:
	rm -rf .venv .mypy_cache .pytest_cache .ruff_cache build dist src/*.egg-info
