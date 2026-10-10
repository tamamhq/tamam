# Dashboard (optional)

The CLI is all you need to run tests. The dashboard adds projects, uploaded builds, saved tests,
live run views and history. It is a FastAPI server plus a React app.

## Run it locally

Three terminals, from the repository root:

```bash
# 1. API on http://localhost:8000 (SQLite, runs execute in-process)
cd backend && TAMAM_AUTH_DEV_MODE=true uv run uvicorn tamam.main:app

# 2. Appium (if it isn't already running)
appium

# 3. Dashboard on http://localhost:5173
cd frontend && npm install && npm run dev
```

`TAMAM_AUTH_DEV_MODE=true` turns on **local mode**: no sign-in, everything in one local
workspace, and the API only answers requests from this computer. It's the normal way to run
the dashboard for yourself.

In the dashboard: create a project, upload your simulator build as a zipped `.app`,
then add a test in plain English and run it.

## Run it for a team

- **Sign-in**: set the `TAMAM_CLERK_*` settings and `VITE_CLERK_PUBLISHABLE_KEY` for the
  frontend ([Clerk](https://clerk.com)); leave `TAMAM_AUTH_DEV_MODE` off.
- **Queue and database**: `docker compose up` starts Postgres and Redis; set
  `TAMAM_DATABASE_URL` and `TAMAM_REDIS_URL`, and run a worker next to the API:
  `cd backend && uv run arq tamam.worker.WorkerSettings`.
- **Devices**: the API and worker can run anywhere, but simulator runs need a Mac. A Linux
  server works with a cloud device farm; see [devices](guide/devices.md).

All settings: [configuration](guide/configuration.md).
