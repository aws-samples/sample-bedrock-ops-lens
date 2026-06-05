.PHONY: up down reset psql redis-cli logs ps schema seed validate

DB_URL := postgresql://bedrock_lens:bedrock_lens_dev@localhost:5432/bedrock_lens

up:
	docker compose up -d
	@echo "waiting for postgres..."
	@until docker exec bedrock-lens-pg pg_isready -U bedrock_lens -d bedrock_lens >/dev/null 2>&1; do sleep 1; done
	@echo "postgres ready at $(DB_URL)"
	@echo "redis ready at redis://localhost:6379"

down:
	docker compose down

reset:
	docker compose down -v
	$(MAKE) up

psql:
	docker exec -it bedrock-lens-pg psql -U bedrock_lens -d bedrock_lens

redis-cli:
	docker exec -it bedrock-lens-redis redis-cli

logs:
	docker compose logs -f

ps:
	docker compose ps

schema:
	docker exec -i bedrock-lens-pg psql -U bedrock_lens -d bedrock_lens < db/schema.sql

seed:
	python db/seed.py --db-url "$(DB_URL)"

validate:
	python db/validate.py --db-url "$(DB_URL)"
