# Local development (no Docker needed). Run `make setup` once, then `make dev` in two terminals or `make api` / `make web`.
UV ?= $(HOME)/.local/bin/uv
PY := backend/.venv/bin/python

.PHONY: setup db api api-demo api-nocam sample-photos web test typecheck build seed migrate e2e

setup: db
	cd backend && ( [ -d .venv ] || $(UV) venv .venv --python 3.12 ) && $(UV) pip install --python .venv/bin/python -e ".[dev]"
	cd backend && [ -f .env ] || cp .env.example .env
	cd frontend && npm install --no-audit --no-fund
	$(MAKE) migrate seed

db:
	scripts/dev-db.sh start

migrate:
	cd backend && .venv/bin/alembic upgrade head

seed:
	cd backend && .venv/bin/python -m app.seed --demo-users

api: db
	cd backend && .venv/bin/uvicorn app.main:app --reload --host 127.0.0.1 --port 8000

# Demo/e2e mode: scripted agent, face check off (works with synthetic cameras)
api-demo: db
	cd backend && PHOTO_FACE_CHECK=false LLM_PROVIDER=scripted .venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000

# Real LLM (Groq), face check off: test with `make sample-photos` uploads when there is no webcam
api-nocam: db
	cd backend && PHOTO_FACE_CHECK=false .venv/bin/uvicorn app.main:app --reload --host 127.0.0.1 --port 8000

sample-photos:
	cd backend && .venv/bin/python -m scripts.sample_photos ../samples

web:
	cd frontend && npm run dev

test: db
	cd backend && .venv/bin/python -m pytest -q

typecheck:
	cd frontend && npm run typecheck

build:
	cd frontend && npm run build

e2e:
	cd e2e && npm install --no-audit --no-fund && ../backend/.venv/bin/python make_fake_cam.py fake-cam.y4m && node run.mjs
