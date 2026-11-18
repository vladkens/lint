.PHONY: prepare check test

prepare:
	uvx ruff check --fix .
	uvx ruff format .
	npx --yes prettier --write --log-level warn .

check:
	uvx ruff format --check .
	uvx ruff check .
	uvx ty check
	npx --yes prettier --check --log-level warn .

test:
	uv run --script test_rust.py
