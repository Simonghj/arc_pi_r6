<div align="center">

# arc_pi_r6

**English** · [简体中文](README_zh.md)

## Team Limitless' project for the 2026 OAIC Agentic Software Factory Hackathon finals

### Requirement tree in, full-stack web app out.

**Write what the app must do as GIVEN/WHEN/THEN scenarios. A coding agent builds it, tests it in a real browser,
and repairs it.**

[![MIT license](https://img.shields.io/badge/license-MIT-f4c430?style=flat-square)](LICENSE)
![Python 3.12](https://img.shields.io/badge/Python-3.12-3776AB?style=flat-square&logo=python&logoColor=white)
![Node.js >= 22.19](https://img.shields.io/badge/Node.js-%3E%3D22.19-339933?style=flat-square&logo=nodedotjs&logoColor=white)

[Quick start](#quick-start) · [How it works](#how-it-works) · [Writing requirements](#writing-requirements) ·
[Reference](#reference) · [Known limits](#known-limits) · [License](#license)

</div>

---

`arc_pi` is a Python harness. You give it a requirement tree: a YAML file of requirements with GIVEN/WHEN/THEN
scenarios. It drives a coding agent until a React + Vite frontend and Express backend implement that tree.

- **Python runs the loop:** ordering, batching, deadlines, builds, Playwright verification, repairs, snapshots,
  evidence.
- **The engine is the [Pi agent](https://github.com/earendil-works)** (`@earendil-works/pi-agent-core` 0.85.1). It
  runs in a Node child process from the prebuilt bundle `bridge/dist/agent.mjs`.
- **The model writes all app code during the run.** `template/` is only a generic starter: a React shell and an
  Express server with no app pages or logic.

This is the final tuned version of arc_pi, built for ARC-Bench.

## Quick start

### 1. Check what you need

| Tool | Version | Why |
|---|---|---|
| Python | 3.12 (the version we tested; older ones are untested) | The harness |
| Node.js + npm | >= 22.19.0 | Agent engine, app builds, Playwright |
| Git | any recent | **Required.** Each run creates a git repo in the output folder and commits after every batch. On Windows, Git for Windows also provides the `bash` the agent uses. |
| Model endpoint | OpenAI-compatible `POST {base}/chat/completions` with tool calling | The model that writes the code |
| Internet | npm registry, Chromium download, your model endpoint | Dependency installs and model calls |

On Linux x64 only, the harness downloads a pinned Node 24.19.0 by itself if no Node >= 22.19 is on `PATH`.

### 2. Install

```bash
git clone <this repo> arc_pi
cd arc_pi

# Windows (PowerShell)
python -m venv .venv
.\.venv\Scripts\Activate.ps1
# Linux / macOS
python3 -m venv .venv
source .venv/bin/activate

python -m pip install -r requirements.txt
npm --prefix grader ci
npx --prefix grader playwright install chromium
python -m arc_pi.cli doctor
```

- `doctor` works offline. It checks the config, Node, the bridge bundle and the template. It prints `doctor: OK`
  even without an API key; the key is only needed for a real run.
- Do **not** run `npm run build` in `bridge/`. This repo ships only the built bundle `bridge/dist/agent.mjs`,
  which already contains everything it needs.

### 3. Configure

Copy `.env.example` to `.env` (`copy .env.example .env` on Windows, `cp .env.example .env` on Linux / macOS) and
fill in your key. `.env` is git-ignored. Or create `.env` yourself and paste:

```dotenv
# Your model endpoint (any OpenAI-compatible /chat/completions API)
OPENAI_API_KEY=
OPENAI_BASE_URL=https://your-provider.example/v1
MODEL=your-model-name

# Vision model, only used when PI_FINAL_REVIEW=1 (key and URL fall back to the OPENAI_* values)
VISUAL_MODEL=
VISUAL_API_KEY=
VISUAL_BASE_URL=

# Recommended settings (our final tuned values)
PI_REASONING=high
PI_REVIEWER=self
PI_CONTEXT_TOKENS=120000
PI_TIME_BUDGET_SECONDS=18000
PI_FINAL_PHASE_SECONDS=3600
PI_PROMPT_SECONDS=900
PI_BATCH_REVIEW=0
PI_SECONDS_PER_BATCH=1300
PI_BLIND_TEST=0
PI_FINAL_REVIEW=0
PI_REPAIR_ROUNDS=3
```

- Fill in `OPENAI_API_KEY`, `OPENAI_BASE_URL` and `MODEL`. Our final runs used
  `MODEL=deepseek-v4-flash-vision-exp` on the ARC-Bench gateway (`https://api.arc-bench.com/v1`).
- **Keep the settings block.** Without it you get the code defaults, which behave differently (see
  [Settings](#settings)).
- Process environment variables take priority over `.env`. The harness hides the key in its logs. Never commit
  `.env`.

### 4. Run

```bash
python main.py example/requirements.yaml -o output --type web
```

`example/requirements.yaml` is a tiny counter app, good for a first run. Progress lines appear in the console and
in `output/.arc/debug.log`.

Then look at the app:

```powershell
python -m arc_pi.cli prepare-preview output
cd output/backend
$env:PORT = 43100; npm start        # bash: PORT=43100 npm start
```

Open `http://localhost:43100`. `prepare-preview` installs dependencies and builds the frontend; it calls no model
and starts no server. The backend serves the built frontend.

<details>
<summary><strong>Command-line options, run time and exit codes</strong></summary>

- The first argument is a YAML file, or a folder containing `requirements.yaml` / `requirements.yml`.
- `-o` / `--output-dir` is the app folder. Without it the app goes to `runs/<UTC time>-<random>/` in this project.
  Inside this project only `runs/` and `output/` are allowed as output folders.
- `--web-port` (alias `--port`) is the port the finished app is checked on. Default 3000.
- `--type` accepts only `web`.
- `python -m arc_pi.cli ...` accepts the same arguments as `main.py`. A leading `compile` is also accepted
  (`python main.py compile example/requirements.yaml -o output`).
- `PI_TIME_BUDGET_SECONDS` is an upper limit (18000 s = 5 h). A run stops earlier once all batches and repairs are
  done. The counter example needs only a small part of it.
- Exit code 0: every finished run, including partial ones; the result is in `outcome` (see
  [Output and evidence](#output-and-evidence)). With no API key, the run prints a note and exits 0 without
  generating anything.
- Exit code 1: a setup failure (invalid requirements, a bad output folder, missing harness files, no git) or a
  crash.

</details>

## How it works

![How arc_pi builds an app. Build loop: requirements go to the agent, which writes one batch; a build check runs after every change (fails → fix), then a contract check and the next batch. Test + repair loop: write tests per requirement, run Playwright in a real browser, repair failures for up to 3 rounds and re-test. When all pass, the best version ships.](assets/harness-loop.svg)

[Open the full-size figure](assets/harness-loop.svg).

Two loops: the **build loop** runs once for each batch of requirements; the **test + repair loop** then tests the
whole app in a real browser and fixes what fails.

With the final settings:

| Step | What happens |
|---|---|
| **Set up** | `template/frontend` and `template/backend` are copied into the output folder only if they are missing. A git repo is created there. |
| **Order and batch** | Requirements are sorted by dependency and grouped by top-level folder (at most 8 per batch). Neighbouring batches are merged until there are about `(time budget - final reserve) / PI_SECONDS_PER_BATCH` batches; 11 with the settings above. |
| **Project map** | Before each batch the harness lists every source file with its exports, API routes and page routes, so the agent does not search the files itself. No model call. |
| **Build after every change** | After each reply that changed files, the app is built and the backend started in a throwaway copy. Build failed → fix prompt with the error. Reply cut off → continuation prompt. Nothing changed → one nudge (once per run). Build passed → contract check. |
| **Contract check** | No model call. Every quoted name in the requirement text must appear in the source, and every backticked GIVEN value must be created by the backend. Gaps → one follow-up listing them. |
| **Commit** | A batch that changed files and builds is committed (`arc_pi batch N: ...`). |
| **Fresh sessions** | If the last request used more than 80% of `PI_CONTEXT_TOKENS` (96000 at 120000), the agent writes a handoff note before the next batch: implemented ids, file map, data model, seed data, API, and a **NEXT BATCH** section mapping the next requirements to existing files. A fresh session continues from that note. |
| **Scenario tests** | After the batches, one more handoff starts a fresh session that writes `OUTPUT/review-tests/<id>.spec.js` for every requirement. Specs that bypass the UI (direct URLs, API calls, CSS selectors, storage) are deleted. Needs more than 1500 s left. |
| **Verification** | The full suite runs against a fresh build: 1 worker, no retries, 10 s per test. The result is saved as the best snapshot. |
| **Repairs** | If anything fails (build, startup, tests, backend errors), the agent repairs the app code from the failure summary; test files are locked. Up to `PI_REPAIR_ROUNDS` (3) rounds, stopping as soon as all tests pass. Each round is **accepted** (build/startup fixed or more tests pass → new best snapshot), **kept** for the next round (no test lost, best not updated) or **reverted** to the best snapshot. |
| **Finish** | The best snapshot is restored if needed. Runtime data files are removed if the app still boots and seeds itself without them. Evidence is written. |

- **Retries.** Each model request gets 3 attempts inside the engine (waits of 2 s and 5 s, 300 s timeout per
  request). After that, network errors, HTTP 429 and 5xx are retried by the harness after 30, 60, 120 and 240 s,
  as long as enough time is left.
- **Time.** `PI_FINAL_PHASE_SECONDS` (3600 s) is kept for tests, verification and repairs. No new batch starts
  after that point; batches not started are recorded as incomplete.

## Writing requirements

Start from `example/requirements.yaml`:

```yaml
id: ROOT
name: Counter
type: FOLDER            # FOLDER groups requirements; ATOMIC is one implementable requirement
dependencies: []
description: A small counter application.
children:
  - id: REQ-1
    name: Counter controls
    type: ATOMIC
    dependencies: []    # ids that must be built first
    description: >-
      The home page shows the count, initially 0, and buttons named "Increment" and "Decrement".
    scenarios:
      - name: Increment and decrement the count
        steps:
          - keyword: GIVEN
            content: The visitor opens the home page and the displayed count is 0.
          - keyword: WHEN
            content: The visitor clicks Increment twice.
          - keyword: THEN
            content: The displayed count is 2.
```

How the agent reads the text:

| You write | The agent does |
|---|---|
| `"Quoted names"` | Uses them as exact accessible names. A button named "Save" has exactly the name `Save`. The word in front decides the element: button, link, tab, textbox, checkbox, combobox, ... |
| Any page or control | Makes it reachable from the home page `/`. Tests navigate only by clicking, never by URL. |
| ``GIVEN a user `alice` exists`` | Treats backticked GIVEN values as seed data. The backend creates them at startup when missing. |
| "new", "does not exist yet", "not yet used", "candidate" | Does not seed those values; the scenario creates them. |
| `reference/*.png` next to the requirement file (optional) | Compares pages with them in the vision review (only with `PI_FINAL_REVIEW=1` and a `VISUAL_MODEL`). |

<details>
<summary><strong>Evolving an existing app</strong></summary>

Point `-o` at a folder that already contains an app. The harness then works on that app:
- The template is copied only into a missing `frontend/` or `backend/`; existing files are kept.
- The agent keeps existing pages, routes and data and changes only what the new requirements need.
- Dependency ids that are not in the new requirement file are treated as already built.
- If a requirement's scenarios changed, its old spec is moved to `.arc/arc_pi/<run>/changed-requirement-specs/`,
  the agent updates the app, and a new spec is written.

</details>

## Reference

### Settings

"Code default" is what you get if the variable is not set at all. "Final" is the value from the `.env` block in
[Configure](#3-configure).

| Variable | Code default | Final | Meaning |
|---|---|---|---|
| `PI_REASONING` | `medium` | `high` | Thinking level: `off`, `low`, `medium`, `high`, `max`. Build fixes and repairs always use `high`. |
| `PI_TIME_BUDGET_SECONDS` | 2400 | 18000 | Upper limit for the whole run |
| `PI_FINAL_PHASE_SECONDS` | 0 (auto) | 3600 | Time kept at the end for tests, verification and repairs (at most half the budget) |
| `PI_SECONDS_PER_BATCH` | 600 | 1300 | Planning value for how many batches to make; not a time limit per batch |
| `PI_PROMPT_SECONDS` | 0 (no limit) | 900 | Time limit for one agent prompt; a prompt that hits it gets a continuation |
| `PI_CONTEXT_TOKENS` | 200000 | 120000 | Handoff to a fresh session when a request passes 80% of this (min 20000) |
| `PI_REPAIR_ROUNDS` | 2 | 3 | Repair rounds after verification (0 to 6); stops early when all tests pass |
| `PI_REVIEWER` | `1` | `self` | `self`: the same agent writes the scenario tests. `1`: a separate reviewer session checks each batch on a copy (experimental). `0`: no scenario tests, only build and startup are checked. |
| `PI_BATCH_REVIEW` | `1` | `0` | `1` with `self`: after each batch the agent tests and fixes its own work (screenshots, vision, accessibility tree, specs) |
| `PI_FINAL_REVIEW` | `1` | `0` | `1`: a whole-app review before verification, comparing pages with `reference/*.png` (needs `VISUAL_MODEL`) |
| `PI_BLIND_TEST` | `0` | `0` | `1`: experimental. A background session writes extra tests from the requirement text alone. |

<details>
<summary><strong>Model, cost and advanced settings</strong></summary>

| Variable | Code default | Final | Meaning |
|---|---|---|---|
| `OPENAI_API_KEY` | none | your key | Required for a run |
| `OPENAI_BASE_URL` | `https://api.arc-bench.com/v1` | your endpoint | OpenAI-compatible base URL (`/chat/completions` is appended) |
| `MODEL` | `deepseek-v4-flash` | your model | Main model |
| `VISUAL_MODEL` / `VISUAL_API_KEY` / `VISUAL_BASE_URL` | empty / `OPENAI_*` / `OPENAI_*` | empty | Vision model for screenshot reviews |
| `PI_MAX_RUN_TOKENS` | 0 (no cap) | not set | Stop sending new prompts after this many main-session tokens |
| `PI_MAX_RUN_COST_CNY` | 0 (no cap) | not set | Stop sending new prompts once the estimated cost reaches this (CNY) |
| `PI_PRICE_INPUT` / `PI_PRICE_OUTPUT` / `PI_PRICE_CACHE_HIT` | by model name | not set | CNY per 1M tokens for the cost estimate. Built in: `glm*` 0.8 / 2.8 / 0.23, `deepseek*` 3 / 9 / 0.1; other models use the deepseek prices. |
| `PI_SELF_REVIEW_SECONDS` | 0 | not set | Time limit for each per-batch self-review (only with `PI_BATCH_REVIEW=1`) |
| `PI_MAX_TURNS_PER_PROMPT` | 0 (no limit) | not set | Model steps per prompt |
| `PI_BATCH_TOOL` | `auto` | not set | Turns on the engine's `batch` tool (several file/shell calls in one step). `auto` = on for `glm*` models only. Idea from the "batch tool" workaround in [Anthropic's parallel tool use docs](https://platform.claude.com/docs/en/agents-and-tools/tool-use/parallel-tool-use); the code is our own. |
| `PI_CONTEXT_BYTES` | 8388608 | not set | Byte limit for the engine's conversation history; larger prompts are split |
| `PI_BASH_PATH` | auto | not set | Path to the bash the agent uses (Windows default: Git Bash) |
| `PI_NPM` | `npm` on PATH | not set | npm executable for builds |

- The token and cost caps only stop *new* prompts. A running prompt finishes, and the first prompt of each repair
  round checks only the clock.
- Reviewer, blind-test and vision calls are not counted in the token and cost caps.
- `ARCBENCH_*` variables (`ARCBENCH_TASK_DIR`, `ARCBENCH_OUTPUT_DIR`, `ARCBENCH_WEB_PORT`, `ARCBENCH_TASK_TYPE`, ...)
  are fallbacks for the command-line arguments, used inside the ARC-Bench runner. You do not need them locally.

</details>

### Output and evidence

`outcome` in the final console line and in `progress.json`:
- `completed`: every requirement implemented and all tests passed.
- `partial`: the app builds and starts, but something is incomplete or some tests fail.
- `failed`: anything else.

<details>
<summary><strong>What the output folder contains</strong></summary>

| Path | What |
|---|---|
| `frontend/`, `backend/` | The generated app. The backend serves the built frontend and the `/api/` routes. |
| `review-tests/` | The scenario specs (`<id>.spec.js`) and `helpers.js` |
| `.git/` | One commit per finished batch. Test writing and repairs are not committed, so the files on disk can be newer than the last commit. |
| `.arc/debug.log` | All progress lines with timestamps |
| `.arc/error-log.md` | Errors from this run, without duplicates |
| `.arc/runner-events.jsonl`, `.arc/traceability/` | Requirement and test events, plus the requirement-to-test mapping |
| `.arc/arc_pi/<run-id>/progress.json` | Per batch: status, changed files, build result, stop reason; plus incomplete requirement ids |
| `.arc/arc_pi/<run-id>/metrics.json` | Time, token use, settings, best test counts |
| `.arc/arc_pi/<run-id>/verification.json` | The best final test report |
| `.arc/arc_pi/<run-id>/project-map.md` | The project map the agent saw at the last batch |
| `.arc/arc_pi/<run-id>/bridge-*.jsonl`, `bridge-stderr.log` | Raw engine log |
| `.arc/arc_pi/<run-id>/verify-*/` | Each check (`verify-batch-*`, `verify-final`, `verify-repair-N`): `verification.json`, build/startup/acceptance logs, test results, screenshots |
| `.arc/arc_pi/<run-id>/best/` | Snapshot of the best version of the app |

Outside the output folder, in this project: `logs/errors.jsonl` (harness errors) and `.cache/npm/` (cached
`node_modules` for app builds, reused across runs).

</details>

### Local commands

| Command | What it does |
|---|---|
| `python -m arc_pi.cli doctor` | Offline check of config, Node, bridge bundle and template |
| `python -m arc_pi.cli grade OUTPUT --tests-dir DIR [--port N]` | Builds an existing app and runs a folder of Playwright specs against it. No model calls. Exit 0 only if all pass. Report: `OUTPUT/.arc/arc_pi/grade/<time>/verification.json`. |
| `python -m arc_pi.cli prepare-preview OUTPUT` | Installs the app's dependencies and builds the frontend so you can start it yourself |
| `python -m arc_pi.cli smoke` | Offline end-to-end check: a scripted fake model through the real engine, plus a real template build and browser run |
| `python -m pytest -q tests` | Offline unit tests (install once with `python -m pip install pytest`; it is not in `requirements.txt`) |

<details>
<summary><strong>Project layout</strong></summary>

| Path | What |
|---|---|
| `main.py`, `arc_pi/cli.py` | Command line and local commands |
| `arc_pi/config.py` | Reads `.env` and validates settings |
| `arc_pi/requirements.py` | Loads, validates and orders the requirement tree |
| `arc_pi/runner.py` | The main loop: batches, project map, handoffs, tests, verification, repairs |
| `arc_pi/prompts.py` | System prompt, batch, contract and repair prompts |
| `arc_pi/contract.py` | Model-free check of quoted names and seed values |
| `arc_pi/bridge.py` | Talks to the Node engine (one child process, line-based JSON messages) |
| `arc_pi/verify.py` | Throwaway build, startup and Playwright runs |
| `arc_pi/review.py` | Self-review, separate reviewer, vision review, scenario-test writing |
| `arc_pi/blindtest.py` | Experimental blind test writer |
| `arc_pi/artifacts.py`, `processes.py`, `preview.py` | Snapshots, subprocesses and Node lookup, preview preparation |
| `arc_pi/projectlog.py`, `errorlog.py` | Run error log and harness error log, with keys hidden |
| `arc_pi/smoke.py` | Offline smoke check |
| `bridge/dist/agent.mjs` | The prebuilt Pi agent engine |
| `grader/` | Playwright install used for verification |
| `template/` | Generic React + Vite frontend and Express backend starter |
| `skills/*/SKILL.md` | Rules added to the agent's prompts (see [Skills and credits](#skills-and-credits)) |
| `arcbench_agent_runtime/` | Runtime SDK from the ARC-Bench organisers (events, traceability, git), unchanged |
| `example/requirements.yaml` | Small example requirement tree |
| `tests/` | Unit tests |

</details>

### Skills and credits

| Skill | When it is used | Based on |
|---|---|---|
| `web-implementation` | Every run, in the system prompt | Ideas from `writing-plans` and `defense-in-depth` in [superpowers](https://github.com/obra/superpowers) (MIT). ARIA patterns distilled from the [W3C ARIA Authoring Practices Guide](https://www.w3.org/WAI/ARIA/apg/). |
| `auth-session-consistency` | Batches that mention auth, login, register, session or account | Our own |
| `playwright-e2e` | Writing and fixing scenario tests | Patterns distilled from the API reference of [playwright-skill](https://github.com/lackeyjb/playwright-skill) (MIT); `helpers.js` is our own |
| `review-and-debug` | Reviews and repair rounds | Ideas from `systematic-debugging`, `verification-before-completion` and `receiving-code-review` in [superpowers](https://github.com/obra/superpowers) (MIT) |

Before choosing selectors, the reviewer reads the page's rendered accessibility tree. The idea of looking at the
page first comes from [anthropics/skills `webapp-testing`](https://github.com/anthropics/skills/tree/main/skills/webapp-testing)
(Apache-2.0; no code copied). The tree itself comes from [Playwright](https://github.com/microsoft/playwright)'s
`locator.ariaSnapshot()`.

## Known limits

- Developed on Windows 11 (Python 3.12, Node 24). A fresh setup on Linux or macOS has not been tried yet.
- Live runs used only the ARC-Bench gateway. Other OpenAI-compatible endpoints should work, but nobody has tried
  one yet.
- Only `web` tasks (React + Vite + Express) are supported.
- The engine is shipped as a prebuilt bundle; its sources are not in this repo.
- Cost estimates are in CNY and use built-in prices for `glm*` and `deepseek*` models only. Set `PI_PRICE_*` for
  other models.

## Third-party components

- `bridge/dist/agent.mjs` bundles these npm packages: `@earendil-works/pi-agent-core`, `@earendil-works/pi-ai`,
  `@earendil-works/pi-telemetry`, `@earendil-works/chord` (0.85.1), `diff`, `ignore`, `partial-json`, `typebox`
  and `yaml`. Each is under its own license; see the package on npm.
- `grader/` installs `@playwright/test` 1.57.0 from npm.
- `arcbench_agent_runtime/` is the ARC-Bench runtime SDK (`arcbench-runtime` on PyPI), included unchanged.

## License

[MIT](LICENSE). This covers the code written for this project. The third-party parts listed above keep their own
licenses.
