.PHONY: up down logs migrate seed test lint fmt eval backup

up:       ## Container starten
	docker compose up -d --build

down:
	docker compose down

logs:
	docker compose logs -f api

migrate:  ## Schema auf den neuesten Stand
	docker compose exec api alembic -c db/alembic.ini upgrade head

seed:     ## Testdaten für den Pilotbetrieb
	docker compose exec api python -m scripts.seed

test:
	docker compose exec api pytest -q

lint:
	docker compose exec api ruff check api scripts evals

fmt:
	docker compose exec api ruff format api scripts evals

eval:     ## Eval-Suite, optional TAGS=menu,noise
	docker compose exec api python -m evals.runner $(if $(TAGS),--tags $(TAGS),) $(if $(MODEL),--model $(MODEL),)

backup:
	bash scripts/backup.sh
