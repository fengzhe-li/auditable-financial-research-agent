# Research Assurance Console (frontend)

React + TypeScript + Vite frontend for the Auditable Financial Research Agent. It reads tasks, claims, validation results, security events and evaluation results from the backend's FastAPI layer and submits the review actions; it is not a chat UI. See the [project README](../README.md) for the workflow it exposes and [docs/PHASE_7_REPORT.md](../docs/PHASE_7_REPORT.md) for the full write-up.

```bash
npm install
npm run dev      # http://localhost:5173, proxies /api to the backend on 127.0.0.1:8000
npm test         # vitest
npm run lint     # oxlint
npm run build    # type-check and production build
```

Start the backend first (`cd ../backend && python3 scripts/run_api.py`, after `python3 scripts/seed_demo_data.py` for demo data).

Source layout: `src/api/` (typed API client), `src/pages/` (task list, task detail, evaluation), `src/components/`; tests sit next to the code they cover, with shared setup and fixtures in `src/test/`.
