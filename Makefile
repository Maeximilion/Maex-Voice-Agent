.PHONY: up down logs migrate seed test lint fmt eval eval-targets eval-nummern backup lock

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
	docker compose exec api ruff check api scripts evals printbridge sim db

fmt:
	docker compose exec api ruff format api scripts evals printbridge sim db

eval:     ## Eval-Suite, optional TAGS=menu,noise
	docker compose exec api python -m evals.runner $(if $(TAGS),--tags $(TAGS),) $(if $(MODEL),--model $(MODEL),)

eval-targets: ## Target cases, level 3-5 and delivery, not in CI, red allowed (docs/08 §1)
	docker compose exec api python -m evals.runner --cases evals/targets $(if $(TAGS),--tags $(TAGS),) $(if $(MODEL),--model $(MODEL),)

eval-nummern: ## Nummernerkennung, ohne DB und ohne Modell
	python -m evals.number_eval

backup:
	bash scripts/backup.sh

# Rebuilds api/requirements.txt (exact versions with hashes) from api/requirements.in,
# in the base image of api/Dockerfile: the pins are for Linux and Python 3.12.
# Existing pins stay; ARGS="--upgrade-package jinja2" moves one, ARGS=--upgrade all.
lock:     ## Lock file from api/requirements.in, optional ARGS=--upgrade
	MSYS_NO_PATHCONV=1 docker run --rm --user "$$(id -u):$$(id -g)" -e HOME=/tmp \
		-v "$(CURDIR)/api:/work" -w /work python:3.12-slim sh -c \
		"pip install --quiet --user --no-warn-script-location pip-tools==7.6.2 && python -m piptools compile --quiet --generate-hashes --strip-extras --output-file=requirements.txt $(ARGS) requirements.in"
