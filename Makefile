PY   ?= python3
VENV := .venv
BIN  := $(VENV)/bin

.PHONY: install api web-dev web-build simulate mock-llm test up clean distclean

install:
	$(PY) -m venv $(VENV)
	$(BIN)/pip install --upgrade pip -q
	$(BIN)/pip install -r requirements.txt

api:  ## FastAPI + background simulator + triage worker
	$(BIN)/uvicorn backend.main:app --reload --port 8000

web-dev:  ## Vite dev server (proxies /api to :8000)
	cd frontend && npm run dev

web-build:  ## production build; also served by the API at /
	cd frontend && npm install && npm run build

simulate:  ## baseline traffic + chaos into traceforge.db, local ML only
	$(BIN)/python -m backend.simulator --ticks 240 --seed 11 \
		--inject db_pool_exhaustion@80 --inject latency_spike@150

mock-llm:  ## OpenRouter-compatible mock + webhook receiver for offline testing
	$(BIN)/uvicorn backend.mock_llm:app --port 9938

test:  ## run the backend test suite
	$(BIN)/python -m pytest -q

up:  ## full stack in Docker (api :8000, web :5173)
	docker compose up --build

clean:
	rm -f traceforge.db traceforge.db-wal traceforge.db-shm
	find . -name '__pycache__' -type d -prune -exec rm -rf {} +

distclean: clean
	rm -rf $(VENV) frontend/node_modules frontend/dist
