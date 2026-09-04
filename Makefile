.PHONY: ci lint format typecheck test backend frontend install-hooks

VENV := .venv/bin
BACKEND := backend

ci: lint typecheck test frontend

lint:
	cd $(BACKEND) && ../$(VENV)/ruff check . && ../$(VENV)/ruff format --check .

format:
	cd $(BACKEND) && ../$(VENV)/ruff check --fix . && ../$(VENV)/ruff format .

typecheck:
	cd $(BACKEND) && ../$(VENV)/mypy

test:
	cd $(BACKEND) && ../$(VENV)/pytest -q

frontend:
	cd frontend && npm run lint && npm run build

install-hooks:
	$(VENV)/pip install pre-commit
	$(VENV)/pre-commit install
