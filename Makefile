# `make help` lists everything. With Docker: `make setup` once, then `make up` / `make down`.
# Without Docker: `make install`, then `make dev`.

# .env supplies WEB_PORT and friends when present.
-include .env

COMPOSE ?= docker compose
PYTHON ?= .venv/bin/python
WEB_PORT ?= 8000
NAME ?= postman

.PHONY: help setup build up down logs ps migrate load-stations api-key shell install dev dev-api-key geocode

## Show this help
help:
	@awk '/^## /{d=substr($$0,4)} /^[a-z-]+:/{if(d){printf "  %-14s %s\n",substr($$1,1,length($$1)-1),d; d=""}}' $(MAKEFILE_LIST)

## First run with Docker: check .env, build, start, create the tables, load the stations
setup:
	@test -f .env || { echo "No .env here. Copy .env.example to .env and fill it in first."; exit 1; }
	@command -v docker >/dev/null || { echo "Docker is not installed."; exit 1; }
	$(COMPOSE) build
	$(COMPOSE) up -d
	$(MAKE) migrate load-stations
	@echo
	@echo "Running on http://localhost:$(WEB_PORT). Create an API key: make api-key NAME=postman"

## Build the image
build:
	$(COMPOSE) build

## Start the API, Postgres and Redis
up:
	$(COMPOSE) up -d

## Stop everything (the database is kept in a volume)
down:
	$(COMPOSE) down

## Follow the API log
logs:
	$(COMPOSE) logs -f web

## Show running containers
ps:
	$(COMPOSE) ps

## Apply database migrations (after pulling new code)
migrate:
	$(COMPOSE) exec web python manage.py migrate

## Load or refresh the fuel stations from data/stations_geocoded.csv
load-stations:
	$(COMPOSE) exec web python manage.py load_stations

## Create an API key in Docker: make api-key NAME=postman
api-key:
	$(COMPOSE) exec web python manage.py create_api_key $(NAME)

## Django shell in Docker
shell:
	$(COMPOSE) exec web python manage.py shell

## Without Docker: create .venv, install, migrate, load stations (SQLite + in-memory cache)
install:
	@test -f .env || cp .env.example .env
	test -x $(PYTHON) || python3 -m venv .venv
	$(PYTHON) -m pip install -r requirements.txt
	$(PYTHON) manage.py migrate
	$(PYTHON) manage.py load_stations

## Without Docker: run the development server on http://127.0.0.1:8000
dev:
	$(PYTHON) manage.py runserver

## Without Docker: create an API key: make dev-api-key NAME=postman
dev-api-key:
	$(PYTHON) manage.py create_api_key $(NAME)

## Rebuild data/stations_geocoded.csv from research/geocoding (needs research/requirements.txt)
geocode:
	$(PYTHON) research/geocoding/run_all.py
