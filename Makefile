.PHONY: help up down logs test test-integration eval seeds lint fmt

help:  ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-18s\033[0m %s\n",$$1,$$2}'

up:  ## Bring up the full demo stack (Postgres + dbt build + MCP server)
	docker compose up --build

down:  ## Tear down the stack and volumes
	docker compose down -v

logs:  ## Follow the server logs
	docker compose logs -f querygate

test:  ## Run unit tests (no warehouse needed)
	uv run pytest -q

test-integration:  ## Spin up Postgres, load seeds+marts, run integration tests
	./scripts/run_integration_tests.sh

eval:  ## Offline retrieval hit@k over the golden question set
	uv run python evals/eval_retrieval.py

seeds:  ## Regenerate the deterministic demo seed CSVs
	uv run python scripts/gen_seeds.py

lint:  ## Ruff lint
	uv run ruff check src tests

fmt:  ## Ruff format
	uv run ruff format src tests evals scripts
