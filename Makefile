# Copyright (c) 2026 Trustedwear Tech Private Limited (https://citra-ai.com)
# Author: Rohit Kumar Chandan
# SPDX-License-Identifier: Apache-2.0
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may not
# use this file except in compliance with the License. You may obtain a copy of
# the License at http://www.apache.org/licenses/LICENSE-2.0

# Citra Flows — local quickstart. Run `make help` for the list.
#
# Everything here wraps docker-compose.quickstart.yml. If you would rather not
# use make, every target is one docker compose command; see INSTALL.md.
.DEFAULT_GOAL := help
.PHONY: help install up build down destroy logs ps smoke test restart shell-api

COMPOSE := docker compose -f docker-compose.quickstart.yml

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

.env:
	@bash scripts/quickstart/gen-env.sh

wizard: ## Guided first run: generate .env with fresh secrets, then start everything
	@bash scripts/quickstart/wizard.sh $(ARGS)

install: .env ## First run: create .env, build images, start everything, wait for healthy
	@$(MAKE) up
	@echo ""
	@echo "  UI       http://localhost:$$(grep -E '^FLOWS_UI_PORT=' .env | cut -d= -f2 | tr -d '\r')"
	@echo "  API      http://localhost:$$(grep -E '^FLOWS_API_PORT=' .env | cut -d= -f2 | tr -d '\r')/docs"
	@echo ""
	@echo "  Sign in  $$(grep -E '^ADMIN_EMAIL=' .env | cut -d= -f2 | tr -d '\r')  /  $$(grep -E '^ADMIN_PASSWORD=' .env | cut -d= -f2- | tr -d '\r' | awk '{ m=""; for(i=0;i<length($$0);i++) m=m"*"; printf "%s (%d characters)", m, length($$0) }')"
	@echo "           the password is the one you chose, never printed — read it with: grep ^ADMIN_ .env"
	@echo "           seeded as super_admin in org '$$(grep -E '^ADMIN_ORG_ID=' .env | cut -d= -f2 | tr -d '\r')' — everything you create is scoped to it."
	@echo "           No public sign-up; add teammates from this account (INSTALL.md, 'Sign in')."
	@echo ""
	@echo "  Verify the install end to end with:  make smoke"

up: .env ## Build if needed and start the stack in the background
	# The three long-running services are named explicitly: `--wait` across the
	# whole project counts the one-shot minio-init container's clean exit as a
	# failure and returns 1 on a healthy stack. Their depends_on still pulls in
	# every data store, and minio-init with it.
	$(COMPOSE) up -d --build --wait citra-workflow citra-worker citra-flows-ui

build: .env ## Rebuild images without starting (use after changing FLOWS_API_PORT)
	$(COMPOSE) build

restart: ## Restart the app services, leaving the data stores alone
	$(COMPOSE) restart citra-workflow citra-worker citra-flows-ui

down: ## Stop everything, keep the data
	$(COMPOSE) down

destroy: ## Stop everything and DELETE all data (Mongo, Redis, MinIO volumes)
	$(COMPOSE) down -v

ps: ## Show service status
	$(COMPOSE) ps

logs: ## Tail the application logs (SERVICE=citra-worker to narrow)
	$(COMPOSE) logs -f $(or $(SERVICE),citra-workflow citra-worker)

smoke: .env ## Verify a running stack: sign in, build a workflow, run it, assert it completed
	@python scripts/smoke_test.py || python3 scripts/smoke_test.py

test: ## Run the Python unit suite (no stack required)
	cd citra-workflow && python -m pytest tests/ -m "not integration" -q

shell-api: ## Open a shell inside the API container
	$(COMPOSE) exec citra-workflow bash
