.DEFAULT_GOAL := help
SHELL := /bin/bash

PLAYBOOK ?= playbooks/bootstrap.yml
LIMIT    ?= homelab

help: ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "} {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

deps: ## Install the collections pinned in requirements.yml
	ansible-galaxy collection install -r requirements.yml

ping: ## Verify SSH reachability and passwordless sudo on every host
	ansible $(LIMIT) -m ping --become

check: ## Dry-run the bootstrap and show what would change
	ansible-playbook $(PLAYBOOK) --limit $(LIMIT) --check --diff

bootstrap: ## Apply the bootstrap for real
	ansible-playbook $(PLAYBOOK) --limit $(LIMIT) --diff

lint: ## Lint playbooks and roles
	ansible-lint

.PHONY: help deps ping check bootstrap lint
