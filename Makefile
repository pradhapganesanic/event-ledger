# Event Ledger — common tasks.
# Docker targets need Docker Desktop; `test` runs without Docker.

.DEFAULT_GOAL := help
.PHONY: help run up smoke down logs test

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
		awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-8s\033[0m %s\n", $$1, $$2}'

run: ## One click: build, start, wait for health, and smoke test (needs Docker)
	./run.sh

up: ## Build and start the stack, detached (needs Docker)
	docker compose up --build -d

smoke: ## Run the smoke test against a running stack (needs Docker)
	./run.sh

down: ## Stop the stack and remove volumes
	docker compose down -v

logs: ## Tail service logs
	docker compose logs -f

test: ## Run all test suites with coverage (no Docker needed)
	cd account-service && python -m pytest -q --cov=app --cov-branch --cov-fail-under=100
	cd gateway         && python -m pytest -q --cov=app --cov-branch --cov-fail-under=100
	cd integration     && python -m pytest -q
