.PHONY: install dev run test lint typecheck verify-db worker

install:
	pip install -e .[dev]

dev:
	PORT=$${PORT:-8000} uvicorn app.main:app --reload --host 0.0.0.0 --port $${PORT:-8000}

run:
	gunicorn app.main:app -w 4 -k uvicorn.workers.UvicornWorker -b 0.0.0.0:$${PORT:-8000} --timeout 120

test:
	pytest -v

lint:
	ruff check app tests

typecheck:
	mypy app

verify-db:
	python scripts/verify_db_match.py

worker:
	celery -A app.tasks.celery_app worker --concurrency=4 --loglevel=info

beat:
	celery -A app.tasks.celery_app beat --loglevel=info
