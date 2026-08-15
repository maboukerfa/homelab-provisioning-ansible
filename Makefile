.DEFAULT_GOAL := help
SHELL := /bin/bash

PLAYBOOK ?= playbooks/bootstrap.yml
LIMIT    ?= homelab

# Extra flags passed straight to ansible / ansible-playbook. The one you will
# actually reach for is -K, when the target's sudo wants a password:
#   make bootstrap ARGS=-K
# Also useful: ARGS="--tags docker", ARGS=-vv, ARGS="--start-at-task=..."
ARGS ?=

help: ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "} {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

deps: ## Install the collections pinned in requirements.yml
	ansible-galaxy collection install -r requirements.yml

ping: ## Verify SSH reachability and sudo on every host
	ansible $(LIMIT) -m ping --become $(ARGS)

check: ## Dry-run the bootstrap and show what would change
	ansible-playbook $(PLAYBOOK) --limit $(LIMIT) --check --diff $(ARGS)

bootstrap: ## Apply the bootstrap for real
	ansible-playbook $(PLAYBOOK) --limit $(LIMIT) --diff $(ARGS)

stack: ## Deploy one stack: make stack NAME=silverbullet
	@test -n "$(NAME)" || { echo "usage: make stack NAME=<stack>"; exit 2; }
	ansible-playbook playbooks/$(NAME).yml --limit $(LIMIT) --diff $(ARGS)

lint: ## Lint playbooks and roles
	ansible-lint

.PHONY: help deps ping check bootstrap stack lint
