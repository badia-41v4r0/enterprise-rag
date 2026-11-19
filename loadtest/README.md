# Load tests

What they measure: EnterpriseRAG itself (app replicas, Postgres, sessions, rate limits, budget reservations, the fair generation queue) under many concurrent users. The model server is replaced by `fake_model_server.py` (deterministic embeddings, a streaming chat with a fixed latency), because the point is to see whether **our** code scales with replicas and keeps its limits, not how fast a GPU is. Run the same scenario against your real model servers before sizing hardware ([`docs/operations/capacity.md`](../docs/operations/capacity.md)).

```bash
DB=postgresql+asyncpg://erag:erag@localhost:55432/erag_load
docker compose -f deploy/docker-compose.yml exec postgres createdb -U erag erag_load
ERAG_DATABASE_URL=$DB uv run alembic upgrade head

# 1. the fake model server (about 1 s per answer: 25 tokens x 40 ms)
uv run python loadtest/fake_model_server.py --port 18080 &

# 2. data: ACME + 300 imported users, the local provider limited to 8 concurrent generations
ERAG_DATABASE_URL=$DB ERAG_OLLAMA_BASE_URL=http://127.0.0.1:18080 \
  uv run python loadtest/setup.py --users 300 --max-concurrency 8 --out /tmp/load-users.csv

# 3. one or two app replicas (same database)
export ERAG_DATABASE_URL=$DB ERAG_OLLAMA_BASE_URL=http://127.0.0.1:18080 ERAG_METRICS_PORT=0   # default limits
uv run uvicorn app.main:app --port 18001 --workers 1 &
uv run uvicorn app.main:app --port 18002 --workers 1 &      # for the two-replica run

# 4. Locust, headless
LOAD_USERS_CSV=/tmp/load-users.csv LOAD_HOSTS=http://127.0.0.1:18001 \
  uv run locust -f loadtest/locustfile.py --headless -u 200 -r 20 -t 3m --csv /tmp/one
LOAD_USERS_CSV=/tmp/load-users.csv LOAD_HOSTS=http://127.0.0.1:18001,http://127.0.0.1:18002 \
  uv run locust -f loadtest/locustfile.py --headless -u 200 -r 20 -t 3m --csv /tmp/two
curl -s http://127.0.0.1:18080/stats    # peak concurrent generations: must not exceed --max-concurrency
```

Results are written to [`docs/benchmarks/r20-load.md`](../docs/benchmarks/r20-load.md) with the hardware used.
