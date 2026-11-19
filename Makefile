PYTHON ?= python3
VENV := .venv/bin/python
LANGUAGE ?= node
DATABASE_URL ?= postgresql://uberpay:uberpay@127.0.0.1:5432/uberpay
.PHONY: setup keys up down demo build lint test test-python test-node test-go test-java contract migration-check conformance verify
setup:
	$(PYTHON) -m venv .venv
	.venv/bin/pip install --index-url https://pypi.org/simple -r tools/requirements-dev.txt
	$(MAKE) keys
keys:
	$(VENV) -m conformance.dev_keys
up:
	docker compose --profile $(LANGUAGE) up --build -d $(LANGUAGE)
down:
	docker compose --profile node --profile java --profile go down
demo:
	docker compose --profile $(LANGUAGE) exec mock-uber python -m conformance.demo --url http://$(LANGUAGE):8080/uberpay/v1 --keys /keys
build:
	npm --prefix implementations/node ci --registry=https://registry.npmjs.org
	npm --prefix implementations/node run build
	cd implementations/go && go build ./cmd/psp
	implementations/java/mvnw -q package
contract:
	$(VENV) tools/check_contract.py
migration-check:
	$(VENV) tools/check_migrations.py --database-url '$(DATABASE_URL)'
lint:
	.venv/bin/ruff check conformance tools --select F
	.venv/bin/ruff format --check conformance tools

test-python:
	$(VENV) -m unittest discover -s conformance/tests -v
test-node:
	npm --prefix implementations/node test
test-go:
	cd implementations/go && go test -race ./...
test-java:
	implementations/java/mvnw -q test
test: test-python test-node test-go test-java
conformance:
	$(VENV) -m conformance.run --languages $(if $(filter all,$(LANGUAGE)),node java go,$(LANGUAGE)) --database-url '$(DATABASE_URL)'
verify: contract lint migration-check test
	$(MAKE) conformance LANGUAGE=all
