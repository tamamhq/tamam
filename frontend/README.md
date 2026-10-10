# Tamam dashboard

The optional web dashboard (React + Vite). See [docs/dashboard.md](../docs/dashboard.md) to run
it with the API.

```bash
npm install
npm run dev      # http://localhost:5173, proxies /api to http://localhost:8000
npm run lint
npm run build
```

Set `VITE_CLERK_PUBLISHABLE_KEY` to enable sign-in; without it the dashboard expects the API in
local mode (no sign-in; see docs/dashboard.md).
