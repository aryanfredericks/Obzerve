# Getting started

This guide takes you from a fresh clone to the complete Obzerve workflow:
1. Run an agent.
2. Watch it fail.
3. Find the root cause.
4. Replay the failure.
5. Turn it into a regression test.
6. Verify a fix with a release gate.

Everything works offline with the built-in mock LLM. A real local model (Ollama) and GPU retrieval are optional extras, covered at the end.

**Time:** about 15 minutes, most of it the first build and download.

---

## 1. Prerequisites

| Tool | Needed for | Check |
| --- | --- | --- |
| Git | getting the code | `git --version` |
| Docker *or* Podman, with Compose | running the platform | `docker compose version` or `podman compose version` |
| [uv](https://docs.astral.sh/uv/getting-started/installation/) | running the demo agent and the `obzerve` CLI | `uv --version` |
| `make` | the shortcuts used below (optional: every target is a plain command in the `Makefile`) | `make --version` |
| Node.js 20+ | **development setup only** (step 3B) | `node --version` |

Disk space: the demo agent installs PyTorch (about 4 GB). It's the ROCm build, which uses an AMD GPU when one is present and otherwise falls back to the CPU.

Ports used: 8080 (dashboard) and 8000 (API) for the containers; 5173 (dashboard) and 8000 (API) for the development setup; 5432 and 6379 (PostgreSQL and Redis) for both. Free them first if something else is using them.

## 2. Get the code

```bash
git clone https://github.com/aryanfredericks/Obzerve.git
cd Obzerve
```

## 3. Start the platform

Choose **A** (containers, recommended for a first run) **or** B (development, with hot reload). Don't run both at once: they use the same ports.

### A. In containers

```bash
make stack-demo
```

The first run builds two images and takes a few minutes. It starts PostgreSQL, Redis, the API, a queue worker and the dashboard. It also creates a `demo` project with the API key `dev_project_key`.

Check that everything is healthy:

```bash
make stack-ps                         # every service "Up", and the api "(healthy)"
curl http://localhost:8000/readyz     # {"status":"ok","database":"ok","queue":"ok"}
```

Open the dashboard at **http://localhost:8080**. It's empty until you send a trace.

Then tell the demo agent and CLI where things are. Set these in every terminal you use for steps 4–6:

```bash
export OBZERVE_ENDPOINT=http://localhost:8000
export OBZERVE_API_KEY=dev_project_key
export OBZERVE_DASHBOARD_URL=http://localhost:8080
```

### B. Development setup (alternative to A)

```bash
make setup        # Python packages (uv) and dashboard packages (npm)
make db-up        # PostgreSQL + Redis in containers
make bootstrap    # database migrations, the "demo" project, the API key dev_project_key
```

Then start each long-running process in its own terminal:

```bash
make server       # terminal 1: API on http://localhost:8000 (restarts on code changes)
make web          # terminal 2: dashboard on http://localhost:5173
```

Set the same variables as in A, but with the development dashboard URL:

```bash
export OBZERVE_ENDPOINT=http://localhost:8000
export OBZERVE_API_KEY=dev_project_key
export OBZERVE_DASHBOARD_URL=http://localhost:5173
```

## 4. Send your first traces

Install the demo agent's dependencies (skip this if you ran `make setup`). The first time, this downloads PyTorch:

```bash
uv sync
```

Run the research agent once normally, and once with every LLM call failing:

```bash
uv run research-agent "Compare recent approaches to RAG evaluation"

TRACE_OK=$(uv run research-agent --quiet | grep -oP 'traces/\K[0-9a-f]{32}')
TRACE_FAIL=$(uv run research-agent --fault llm-outage --quiet | grep -oP 'traces/\K[0-9a-f]{32}')
echo "$OBZERVE_DASHBOARD_URL/traces/$TRACE_FAIL"
```

The first command prints a report and a link to its trace. The other two save their trace IDs for later steps. The failing run prints `run failed: … (simulated timeout, injected fault)`; that is expected.

Open the printed link. On the trace page you'll see:
- **Waterfall:** search → retrieval → three `llm.generate` attempts in red, each one a timeout, with retries counted.
- **Root cause candidate:** a red callout pointing at `llm.generate` and its `LLMTimeoutError`.
- **Replay & regression testing:** a panel that marks this trace as **Replayable**.

Other faults to try with `--fault`: `llm-timeout` (recovers after retries), `bad-retrieval` (every step "succeeds" but the evidence is irrelevant), and `search-error` (a tool fails and the agent continues without it). The **Metrics** page shows latency percentiles, error rates and estimated cost.

## 5. Replay the failure

Replay the failed trace without calling anything external. Every step is served from the recording, so the failure happens again exactly:

```bash
uv run research-agent replay "$TRACE_FAIL" --mode recorded --quiet
```

Expected output: `recorded calls: 5, live calls: 0, diverged: none`, then `replay failed: LLMTimeoutError …`.

Now replay the *same* recording with a fix: a fallback that degrades gracefully when the model is down.

```bash
uv run research-agent replay "$TRACE_FAIL" --mode recorded --fallback
```

This time the run completes with a report saying the model is unavailable, listed with the sources it found. Each replay prints a `compare:` link, which shows the original run and the replay side by side, step by step.

## 6. Turn the failure into a regression test and gate a release

**a. Create regression cases** from both traces. You can also do this with **Turn into a regression case…** on a trace page.

```bash
uv run obzerve case add "$TRACE_FAIL" --dataset getting-started \
    --name "LLM outage degrades gracefully" --mode recorded \
    --no-error --contains-any unavailable

uv run obzerve case add "$TRACE_OK" --dataset getting-started \
    --name "Answers the question" --no-error --contains-any evaluation
```

`--mode recorded` makes the outage case replay the recorded failure every time. The second case uses the default `llm-live` mode: the model runs live against the recorded tool results.

**b. Run the dataset** against the current agent (the baseline) and against the fixed agent (the candidate):

```bash
BASE=$(uv run research-agent eval getting-started --label baseline | tee /dev/stderr | grep -oP '^run: \K\S+')
FIX=$(uv run research-agent eval getting-started --label "fallback fix" --fallback | tee /dev/stderr | grep -oP '^run: \K\S+')
```

Expected: the baseline passes 1 of 2 cases (the outage case fails) and the fix passes 2 of 2.

**c. Gate the release.** This is the check a CI pipeline would run; it exits with 1 when the gate fails:

```bash
uv run obzerve eval gate "$BASE" "$FIX" --min-pass-rate 1.0     # GATE PASSED: 1 fix, 0 regressions
uv run obzerve eval gate "$FIX" "$BASE"                         # GATE FAILED: undoing the fix is a regression
```

The same comparison is in the dashboard under **Evals**: tick two runs and choose **Compare selected**, then use **Evaluate gate**.

Running this section again adds the same cases to the dataset a second time. Use a new dataset name, or archive old cases on the dataset's page.

## 7. Optional: a real local model (Ollama)

```bash
ollama serve &                 # if Ollama isn't already running
ollama pull qwen3              # once; any Ollama chat model works
uv run obzerve-server pricing set qwen3:latest --provider ollama --input 0 --output 0   # local: $0
uv run research-agent --llm ollama --fault bad-retrieval
```

In the containerized stack, run the pricing command inside the API container instead:

```bash
podman compose --profile stack exec api obzerve-server pricing set qwen3:latest --provider ollama --input 0 --output 0
```

Add `--llm ollama` to any `research-agent` command, including `replay` and `eval`. With a real model the same question can produce a different answer on each run, which is what `llm-live` replays and eval runs are built to handle. Try `--prompt concise` with `eval` to watch a "cheaper" prompt get blocked by the gate because it makes things up when the context is irrelevant.

## 8. Optional: GPU retrieval

```bash
make gpu-check    # prints the PyTorch build and whether a GPU is visible
```

On an AMD GPU with ROCm, retrieval embeddings run on the GPU. Otherwise they run on the CPU, which is slower to start but works the same.

## 9. Run the checks

These are the same checks as GitHub CI. Run them with the development databases up (`make db-up`):

```bash
make ci           # ruff, migrations check, all tests, dashboard build
```

## 10. Stop everything

| You started | Stop it with |
| --- | --- |
| The container stack (3A) | `make stack-down` (stops every Obzerve container, including scaled-out workers) |
| `make server` / `make web` (3B) | `Ctrl+C` in their terminals |
| `make db-up` (3B) | `make db-down` |
| `ollama serve` | `pkill -x ollama`, or `Ctrl+C` in its terminal |

Your data stays in the `obzerve-pgdata` and `obzerve-redisdata` volumes. To delete it for a completely fresh start, run `podman volume rm obzerve_obzerve-pgdata obzerve_obzerve-redisdata` (or the `docker volume rm` equivalent) after stopping.

Check that nothing is left running:

```bash
podman ps --filter label=com.docker.compose.project=obzerve    # or: docker ps ...
```

## Troubleshooting

| Symptom | Fix |
| --- | --- |
| `address already in use` on 8000, 8080, 5173, 5432 or 6379 | Something else is using the port; often the other setup (3A vs 3B). Stop it (step 10), or move the stack with `OBZERVE_API_PORT=… OBZERVE_WEB_PORT=… make stack-demo` |
| Dashboard says **Not authorized** | Use **API key** in the top bar and enter `dev_project_key`. (`make stack` builds the dashboard without a key; `make stack-demo` includes it) |
| `make stack-demo` hangs at the dashboard step | The API never became healthy. Look at `podman compose --profile stack logs api`; usually it can't reach PostgreSQL |
| `obzerve` CLI: `HTTP 401` | `export OBZERVE_API_KEY=dev_project_key` (the CLI has no default key) |
| `obzerve case add`: `This trace cannot be replayed` | The trace was recorded without replayable operations, or before Phase 3. Record a new one |
| Trace links point to the wrong port | Set `OBZERVE_DASHBOARD_URL` (step 3) |
| Server tests are skipped | PostgreSQL or Redis isn't running: `make db-up` |
| `--llm ollama`: `Ollama unreachable` | Run `ollama serve`, and `ollama pull qwen3` once |

Next: [deployment.md](deployment.md) covers running Obzerve for a team (configuration, TLS, scaling, upgrades), and [architecture.md](architecture.md) covers how it works.
