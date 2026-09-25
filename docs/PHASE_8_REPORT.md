# Phase 8 Report

Status: **Complete.** 224/224 backend tests pass, 27/27 frontend tests pass, frontend production build succeeds, both Docker images build, `docker compose up --build` reaches a healthy, populated stack, and the seed/reset cycle was verified against real running containers. No real external LLM was called anywhere in this phase. Git was initialized with a single honest initial commit and was **not** pushed anywhere.

## The Phase 8 goal

Engineering polish and portfolio release: make the existing system (Phases 1-7, unchanged) easy to run, test, understand, and publish. No new product features, no architecture changes, no real model call.

## 1. Docker / Compose

Two services, one named volume, no other infrastructure - PostgreSQL/Redis/Qdrant/Kubernetes were deliberately not introduced (see `docs/ROADMAP.md`'s Phase 8 deviations).

| File | Purpose |
|---|---|
| `backend/Dockerfile` | `python:3.12-slim`, `pip install -e ".[api]"`, serves `afra.api.app` |
| `backend/docker/entrypoint.sh` | Seeds demo data only if `AFRA_DB_PATH` doesn't exist yet, then starts the API |
| `backend/.dockerignore` | Excludes `tests/`, `afra_demo.db`, caches |
| `frontend/Dockerfile` | Multi-stage: `node:20-slim` build → `nginx:1.27-alpine` serving the static build |
| `frontend/nginx.conf` | Proxies `/api/*` to the `backend` service by Compose DNS name - the same relative path the Vite dev proxy uses, so no frontend code differs between dev and container |
| `docker-compose.yml` | `backend` + `frontend`, a named volume (`afra_demo_db`) for the SQLite file, `HEALTHCHECK`s on both, `frontend` waits on `backend`'s healthcheck |

`scripts/run_api.py` gained `AFRA_API_HOST`/`AFRA_API_PORT` env-var overrides (default unchanged: `127.0.0.1:8000`) - the container sets `AFRA_API_HOST=0.0.0.0`; nothing else differs between local and containerized runs.

**Verified live** (not just "should work"): `docker compose build` (both images build cleanly), `docker compose up -d` (backend reaches `healthy` before frontend starts, per `depends_on: condition: service_healthy`), `GET /api/health` on both the backend's own port and through the frontend's nginx proxy, `GET /api/tasks` returns 8 auto-seeded tasks on first run, and `docker compose down -v && docker compose up --build` produces a fresh reseed (8 new tasks, new `task_id`s).

## 2. Health / readiness

`GET /api/health` (already built in Phase 7) is reused as both liveness and readiness for both containers' `HEALTHCHECK`s. No separate readiness endpoint was added - this system has no external dependency (connection pool, downstream service) that would make "up" and "ready" meaningfully different states; adding one would be a rule with nothing real behind it. Revisit if a future phase adds a genuine external dependency.

## 3. CI

`.github/workflows/ci.yml`, three jobs, all offline, no secrets:

- **backend**: `pytest -v` (224 tests), the deterministic Phase 6 benchmark (`build_benchmark_v1.py` + `run_benchmark.py`), and the Phase 6.5 harness's dry-run mode (`run_real_model_benchmark.py --configurations A D F --repeat 3`, zero external calls, no credentials).
- **frontend**: `npm test` (27 tests), `npm run build`.
- **docker**: builds both images (no push).

Every constituent command was run directly on the host during this phase and passed - but the workflow file itself has not been executed by an actual GitHub Actions runner, since this repository has not been pushed to GitHub. Disclosed, not claimed as "CI verified green" on a real runner.

## 4. Reproducible demo

```bash
# Local (no Docker)
cd backend && python3 -m pip install -e ".[api]" && python3 scripts/seed_demo_data.py && python3 scripts/run_api.py
cd frontend && npm install && npm run dev

# Docker (one command)
docker compose up --build

# Reset to a fresh demo dataset (either path)
docker compose down -v && docker compose up --build     # Docker
rm backend/afra_demo.db && python3 scripts/seed_demo_data.py   # local
```

## 5. Documentation

`README.md` was fully rewritten into a concise, portfolio-quality version covering: the problem, why this isn't a generic chatbot, architecture (with diagram), the assurance workflow (with diagram), a key-safety-boundaries table, Phase 6 deterministic evaluation numbers, Phase 6.5's explicit pending status, Phase 7 console screenshots, a dedicated Limitations section, local/Docker run instructions, and tests. The previous README's single giant phase-by-phase paragraph was moved to `docs/ROADMAP.md`/`docs/PHASE_*_REPORT.md`, which already carried that detail. One stale figure caught while rewriting (D's unsupported-claim rate still said "13%" in prose, missed by the Phase 6.5 grep sweep which was scoped to `docs/`/`backend/` and didn't cover the repository root) was corrected to the reconciled `16%`.

`docs/ROADMAP.md`'s Phase 8 row and deviations entry were added; `docs/ARCHITECTURE.md` needed no changes - its Phase 0 "Tech stack" table already specified `Docker / Docker Compose` and `GitHub Actions` for exactly this role, so this phase fulfilled an existing design rather than deviating from it.

## 6. Visual assets

- `docs/assets/architecture-diagram.svg` - component map, hand-authored, rendered in a browser and visually verified.
- `docs/assets/workflow-diagram.svg` - state-machine diagram; the first draft had overlapping boxes/labels, caught by rendering it and fixed before being committed.
- `docs/assets/screenshots/01-task-list.jpg` through `05-evaluation.jpg` - captured from the real Docker Compose stack against real seeded demo data (Task List, Task Detail overview, Claims & Validation, Security & Review, Evaluation), immediately after a fresh `docker compose down -v && docker compose up --build` so they reflect the documented one-command path, not leftover manual-testing state.
- No GIF was produced - optional ("if practical") in this phase's instructions; skipped to stay in scope, not silently dropped.

## 7. Git / GitHub readiness

The repository was not a git repository before this phase. `git init` was run, the existing root `.gitignore` (already covering `node_modules/`, `dist/`, `*.db`, Python caches, `.env*`) was reused as-is, and a single initial commit was created. **History was not fabricated to look incremental** - every phase's code already existed in final form when this phase began, so presenting it as commit-by-commit development would misrepresent how it was actually produced. **Not pushed anywhere**, per instruction.

**Ready to publish**: yes, as a single-commit snapshot with an accurate README and full phase-report history in `docs/`. Not yet done: choosing and adding a LICENSE (explicitly flagged, not silently assumed), and an actual push to a remote (requires the user's explicit go-ahead and a target repository).

## How to run it

See the README's "How to run locally" / "How to run with Docker" / "Tests" sections - reproduced exactly, not duplicated with different commands here.

## Overengineering flags

None identified specific to this phase - scope stayed to exactly the eight numbered priorities given.

None of the above blocks anything; there is no Phase 9 defined.
