.PHONY: help up down build logs ps gpu-up monitor-up stop clean

help: ## Show available targets
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-12s\033[0m %s\n", $$1, $$2}'

up: ## Start the full dev stack
	docker compose up -d --build

down: ## Stop all containers (keep volumes)
	docker compose down

stop: ## Stop containers without removing them
	docker compose stop

ps: ## List running services
	docker compose ps

build: ## Build all images
	docker compose build

logs: ## Tail logs of all services
	docker compose logs -f --tail=100

gpu-up: ## Start the stack with GPU transcription (needs nvidia-container-toolkit)
	docker compose -f docker-compose.yml -f docker-compose.gpu.yml up -d --build

monitor-up: ## Start monitoring profile (Prometheus, Grafana, Loki, Tempo)
	docker compose --profile monitoring up -d

clean: ## Stop everything and wipe volumes (destroys local data)
	docker compose down -v --remove-orphans
