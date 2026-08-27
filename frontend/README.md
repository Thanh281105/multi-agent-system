# Thương Trí frontend

React/TypeScript workspace for the Evidence Atlas interface. Vite emits the
production bundle into `app/frontend/dist`; FastAPI serves that directory from
the same origin as the API.

## Local workflow

```powershell
npm ci
npm run dev
```

The development server proxies `/api` to `http://127.0.0.1:8000`.

## Required checks

```powershell
npm run lint
npm test
npm run build
```

Do not hand-edit files under `app/frontend/dist`. Keep UI decisions aligned
with the root `DESIGN.md`. API keys are runtime input and must never be stored
in source, browser storage, logs, screenshots, or generated artifacts.
