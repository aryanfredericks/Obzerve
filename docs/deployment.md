# Deploying Obzerve

Obzerve ships as two container images and a compose file that runs the whole platform: PostgreSQL, Redis, the API, queue workers, and the dashboard. It works the same with Docker Compose and with Podman Compose.

```
                     ┌────────────── :8080 ──────────────┐
 browser ───────────▶│ web (nginx: dashboard + /api proxy)│──┐
                     └────────────────────────────────────┘  │
 agents (SDK/OTLP) ──────────── :8000 ──────▶ api ◀──────────┘
                                               │  validate + enqueue
                                               ▼
                                        redis (stream) ──▶ worker × N ──▶ postgres
```

## Quick start

For a guided first run (including the failure → replay → regression test → gate walkthrough), see [getting-started.md](getting-started.md).

```bash
make stack-demo      # build and start everything, plus a demo project and key
# or, without make:
OBZERVE_DASHBOARD_KEY=dev_project_key podman compose --profile stack up -d --build
```

Open http://localhost:8080. Agents send traces to http://localhost:8000 with an API key:

```bash
uv run research-agent --fault llm-outage --dashboard-url http://localhost:8080
```

`make stack` starts the same platform without the demo project and without building a key into the dashboard. Create a project and key with:

```bash
podman compose --profile stack exec api obzerve-server create-project my-app
```

Then enter the printed key in the dashboard's **API key** control.

## What runs

| Service | Image | Role |
| --- | --- | --- |
| `postgres` | `postgres:17` | Traces, spans, prices, eval datasets and runs |
| `redis` | `redis:7-alpine` | Ingest queue (append-only file, so queued telemetry survives a restart) |
| `migrate` | `obzerve-server` | One-shot: applies database migrations, then exits. The API and workers wait for it |
| `api` | `obzerve-server` | OTLP ingestion in queue mode, plus the dashboard and metrics API |
| `worker` | `obzerve-server` | Persists queued telemetry; scale out with `--scale worker=N` |
| `web` | `obzerve-web` | nginx serving the dashboard and proxying `/api` to the API, so the browser uses one origin |

Services start in dependency order using health checks: PostgreSQL and Redis become healthy, then migrations finish, then the API becomes healthy, then the dashboard starts.

## Configuration

Set these in the environment or in a `.env` file next to `compose.yaml`:

| Variable | Default | Meaning |
| --- | --- | --- |
| `OBZERVE_DB_PASSWORD` | `obzerve` | PostgreSQL password, used by both the database and the services. **Change it** |
| `OBZERVE_API_PORT` / `OBZERVE_API_BIND` | `8000` / `127.0.0.1` | Host port and interface for the API (agents connect here) |
| `OBZERVE_WEB_PORT` / `OBZERVE_WEB_BIND` | `8080` / `127.0.0.1` | Host port and interface for the dashboard |
| `OBZERVE_DASHBOARD_KEY` | empty | Build an API key into the dashboard. Local demos only |

Any server setting from `server/src/obzerve_server/config.py` can be added to the `environment` of the `x-server` block in `compose.yaml` as `OBZERVE_<NAME>`. Examples: `OBZERVE_QUEUE_MAX_LENGTH`, `OBZERVE_API_KEY_CACHE_TTL_S`, `OBZERVE_MAX_INGEST_BYTES`, `OBZERVE_WORKER_MAX_DELIVERIES`.

Both host ports bind to `127.0.0.1` by default. To reach Obzerve from other machines, set `OBZERVE_*_BIND=0.0.0.0` only behind TLS (see below).

## Before exposing it to a network

- Put a TLS-terminating reverse proxy in front of `web` and `api`, for example Caddy, Traefik, or a cloud load balancer. Neither service terminates TLS itself.
- Change `OBZERVE_DB_PASSWORD`. Keep PostgreSQL's and Redis's ports bound to localhost, or remove those port mappings entirely; the services reach them over the compose network.
- Don't set `OBZERVE_DASHBOARD_KEY`. Create per-project keys with only the scopes each client needs: `ingest` for agents, `read` for viewers, `write` for CI that registers prompts and submits eval runs.
- `/metrics` on the API and port 9101 on each worker are unauthenticated Prometheus endpoints. Don't route them through the public proxy.
- User accounts and login are planned for a later Phase 4 step. Until then the dashboard authenticates with a project API key held in the browser.

## Operating it

```bash
make stack-ps                    # containers and health
make stack-logs                  # follow API + worker logs
podman compose --profile stack exec api obzerve-server queue status   # backlog / in progress / dead letters
podman compose --profile stack up -d --scale worker=3                 # more workers
make stack-down                  # stop (data stays in the obzerve-pgdata / obzerve-redisdata volumes)
```

- **Scaling:** workers scale linearly. On one machine, a single worker drained a 35k-trace backlog in 93 s and three workers drained it in 31 s. The API accepted 880 requests/s (44k spans/s) in its container. If the backlog reaches `OBZERVE_QUEUE_MAX_LENGTH`, the API answers 503 with `Retry-After`, and OTLP exporters slow down instead of losing data.
- **Upgrades:** pull or build the new images and run `podman compose --profile stack up -d --build`. The `migrate` service applies new migrations before the API and workers start.
- **Backups:** back up the `obzerve-pgdata` volume, for example with `pg_dump` via `podman compose exec postgres pg_dump -U obzerve obzerve`. Redis holds only in-flight telemetry.

## Images

```bash
podman build -f server/Dockerfile -t obzerve-server .   # API and worker (224 MB, runs as a non-root user)
podman build -f web/Dockerfile    -t obzerve-web .      # dashboard (50 MB)
```

The server image installs only the server's locked dependencies with uv. It does not include PyTorch, which only the demo agent's GPU retrieval needs. Health checks are declared in `compose.yaml` as well as in the Dockerfile, because Podman's default (OCI) image format drops Dockerfile `HEALTHCHECK` instructions.

## Continuous integration

`.github/workflows/ci.yml` runs on every push to `main` and on every pull request:
- **Python:** ruff, migrations applied to a fresh database and checked against the models (`obzerve-server check-migrations`), and the full test suite against PostgreSQL and Redis service containers. `OBZERVE_REQUIRE_SERVICES=1` makes the run fail, rather than skip the server tests, if those services are unreachable.
- **Dashboard:** typecheck and production build.
- **Images:** both images build, and `compose.yaml` renders.

CI installs everything except the ROCm PyTorch build. The GPU retrieval test and the Ollama test skip themselves there. `make ci` runs the same checks locally.
