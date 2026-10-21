# =============================================================================
# TG-Desk CRM — developer shortcuts (Linux / macOS / WSL / Git-Bash)
# =============================================================================
# Windows users: use `.\make.ps1 <target>` instead (same targets, no make needed).
#
# Quick start:
#   make up            # start PostgreSQL + Redis (Docker)
#   make backend-install
#   make seed          # create tables + demo data
#   make dev           # prints the 4 terminals to run
#
# Everything runs from the repo root:  make -C . up
# =============================================================================

SHELL := /bin/bash
.DEFAULT_GOAL := help
.ONESHELL:
.SHELLFLAGS := -eu -o pipefail -c

COMPOSE_FILE := docker-compose.dev.yml
COMPOSE      := docker compose -f $(COMPOSE_FILE)

BACKEND_DIR      := backend
FRONTEND_DIR     := frontend
WORKER_DIR       := telethon_worker
VENV_DIR         := $(BACKEND_DIR)/.venv
PYTHON           := $(VENV_DIR)/bin/python
PIP              := $(VENV_DIR)/bin/pip
REQUIREMENTS     := $(BACKEND_DIR)/requirements.txt
API_PORT         ?= 8000

.PHONY: help up down logs ps backend-install seed api worker fe-install fe dev build \
        init-env clean-backend clean-check

# --------------------------------------------------------------------------- #
# Help — auto-generated from the `## ` comments below
# --------------------------------------------------------------------------- #
help: ## Show this help
	@echo ""
	@echo "  TG-Desk CRM — make targets"
	@echo "  ------------------------------------------------------------------"
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| sort \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'
	@echo ""

# --------------------------------------------------------------------------- #
# Infrastructure (Docker Compose v2)
# --------------------------------------------------------------------------- #
up: ## Start PostgreSQL 16 + Redis 7 in the background
	@test -f .env || { echo "[make] .env missing -> creating it from .env.example"; cp .env.example .env; }
	$(COMPOSE) up -d
	@echo "[make] waiting for containers to become healthy..."
	$(COMPOSE) ps

down: ## Stop and remove containers (named volumes / data are kept)
	$(COMPOSE) down

logs: ## Follow logs of all services (Ctrl+C stops following only)
	$(COMPOSE) logs -f --tail=200

ps: ## Show container status
	$(COMPOSE) ps

init-env: ## Create .env from .env.example if it does not exist yet
	@test -f .env || cp .env.example .env
	@echo "[make] .env ready"

# --------------------------------------------------------------------------- #
# Backend
# --------------------------------------------------------------------------- #
backend-install: ## Create backend/.venv and install requirements
	@test -f $(REQUIREMENTS) || { echo "[make] ERROR: $(REQUIREMENTS) not found"; exit 1; }
	python3 -m venv $(VENV_DIR)
	$(PIP) install --upgrade pip
	$(PIP) install -r $(REQUIREMENTS)
	@test -x $(PYTHON) || python3 -m venv $(VENV_DIR)   # defensive: system may lack python3-venv
	@echo "[make] backend ready -> next: 'make seed' then 'make api'"

seed: ## Seed the database (idempotent; add RESET=1 to drop and recreate)
	@test -x $(PYTHON) || { echo "[make] ERROR: run 'make backend-install' first"; exit 1; }
	@if [ "$${RESET:-0}" = "1" ]; then \
		cd $(BACKEND_DIR) && ../$(PYTHON) -m seed --reset; \
	else \
		cd $(BACKEND_DIR) && ../$(PYTHON) -m seed; \
	fi

api: ## Run the FastAPI backend with autoreload on :$(API_PORT)
	@test -x $(PYTHON) || { echo "[make] ERROR: run 'make backend-install' first"; exit 1; }
	cd $(BACKEND_DIR) && ../$(PYTHON) -m uvicorn app.main:app --reload --port $(API_PORT)

# --------------------------------------------------------------------------- #
# Telethon worker
# --------------------------------------------------------------------------- #
worker: ## Run the Telethon worker (python worker.py)
	@test -x $(PYTHON) || { echo "[make] ERROR: run 'make backend-install' first"; exit 1; }
	cd $(WORKER_DIR) && ../$(PYTHON) worker.py

# --------------------------------------------------------------------------- #
# Frontend
# --------------------------------------------------------------------------- #
fe-install: ## npm install in frontend/
	cd $(FRONTEND_DIR) && npm install

fe: ## Run the Vite dev server on :5173
	cd $(FRONTEND_DIR) && npm run dev

# --------------------------------------------------------------------------- #
# Everything at once
# --------------------------------------------------------------------------- #
dev: up ## Print the four terminals to run (starts infra first)
	@echo ""
	@echo "  ============================================================"
	@echo "   TG-Desk CRM — run these 4 terminals IN THIS ORDER"
	@echo "  ============================================================"
	@echo "   1)  make api       -> FastAPI   http://localhost:8000/health"
	@echo "   2)  make seed      -> tables + demo data (once; RESET=1 to wipe)"
	@echo "   3)  make worker    -> Telethon worker (needs real TG_API_*)"
	@echo "   4)  make fe        -> Vite      http://localhost:5173"
	@echo "  ------------------------------------------------------------"
	@echo "   Infra is up:  make ps / make logs / make down"
	@echo "  ============================================================"
	@echo ""

# --------------------------------------------------------------------------- #
# Build / verification
# --------------------------------------------------------------------------- #
build: ## Build the frontend and byte-compile the Python sources
	cd $(FRONTEND_DIR) && npm run build
	@test -f $(REQUIREMENTS) || { echo "[make] ERROR: $(REQUIREMENTS) not found"; exit 1; }
	python3 -m compileall -q $(BACKEND_DIR) $(WORKER_DIR)
	@echo "[make] build OK"

clean-backend: ## Remove __pycache__ / .pytest_cache from the repo
	find . -type d -name '__pycache__' -prune -exec rm -rf {} + 2>/dev/null || true
	rm -rf $(BACKEND_DIR)/.pytest_cache $(BACKEND_DIR)/.mypy_cache $(BACKEND_DIR)/.ruff_cache

# Chained commands above use `&&` legitimately: SHELL is bash with `-eu -o pipefail`.
# (Only PowerShell forbids `&&` — see make.ps1.)
