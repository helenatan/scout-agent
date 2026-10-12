# Reproducing the study

There are three levels. The first two need nothing beyond this repository. The third runs real models on your data.

The original snapshot (13,998 captured postings with full description text) and the answer keys are not distributed,
because they contain job posting text owned by employers and job boards. Everything else is here: the agent, all five
tool versions, the runners, the graders, the task prompts, the frozen grading records and one row per graded attempt.

## Setup

Python 3.13 was used for the study.

```bash
python3.13 -m venv .venv
.venv/bin/pip install -r requirements.lock
.venv/bin/python scripts/create_demo_data.py   # fictional 3-posting snapshot at m0/data/pai_dump_2026-09-06.sqlite
```

`create_demo_data.py` refuses to overwrite an existing database, so it never replaces a real snapshot.

## Level 1: run Scout

```bash
.venv/bin/python -m pytest tests -q
```

The suite runs against the demo snapshot. Tests that assert numbers from the original snapshot are listed in
`tests/real_data_tests.txt` and skipped automatically when the demo snapshot is installed.

Call any tool version directly:

```python
from scout.tools.pai_tools import PAIData, execute_tool
data = PAIData()
for design in ["baseline", "redesigned", "tuned", "dated", "v5"]:   # v1 .. v5
    print(design, execute_tool(data, design, "search_job_postings", {"snapshot_id": "pai-2026-08", "mode": "counts"}
                               if design != "baseline" else {"snapshot_id": "pai-2026-08"}))
```

Serve one version over MCP for any MCP client: `.venv/bin/python -m scout.tools.mcp_server --design v5`.

Run the service with the scripted model (no API key, no cost):

```bash
SCOUT_PROVIDER=fake .venv/bin/uvicorn scout.service.api:app --port 8000
curl -s localhost:8000/v1/runs -H 'content-type: application/json' -d '{
  "task": "How many PM postings were captured in pai-2026-08?", "snapshot_ids": ["pai-2026-08"],
  "model": "sonnet", "tool_design": "v5", "max_cost_usd": 0.5, "deadline_seconds": 120}'
```

The four run endpoints are `POST /v1/runs`, `GET /v1/runs/{id}`, `GET /v1/runs/{id}/events` and
`POST /v1/runs/{id}/cancel`; the web UI is at `/ui/index.html`.

| Variable | Meaning |
|---|---|
| `SCOUT_PROVIDER` | `fake` (scripted demo), `claude-code` (headless Claude Code; the study's setting), or `anthropic` (Messages API, needs `ANTHROPIC_API_KEY`; the default) |
| `SCOUT_CLAUDE_CLI` | Path to the `claude` binary for the Claude Code provider (default: `claude` on `PATH`, logged in) |
| `SCOUT_RUNS_DB` | Run store location (SQLite) |
| `SCOUT_SPEND_CAP_USD` | Service-wide spending cap (default $5) |
| `SCOUT_TOOL_TRANSPORT` | `mcp` (default) or `direct` |

Do not expose the service publicly: it has no user authentication or per-user billing.

## Level 2: verify the published results

```bash
.venv/bin/python scripts/verify_results.py
```

This recomputes every table in [results.md](results.md) from [`results/attempts.csv`](../results/attempts.csv) and
fails if any documented number differs. The CSV has one row per graded attempt (two rows when an attempt was graded by
two graders) with these columns:

| Column | Meaning |
|---|---|
| `experiment`, `label`, `suite`, `split` | Which experiment and result set; `original-11` or `fresh-v1`; `dev` or `holdout` |
| `task_id`, `model`, `tool_version`, `variant`, `rep` | The setup; `variant` names a Haiku tuning variant (C, V0 to V3) |
| `run_id`, `state`, `outcome_reason` | Scout's run id and terminal state (`completed`, `incomplete`, ...) with its machine reason |
| `grader`, `passed`, `failed_checks` | Grader version, 1/0 (empty while pending human review), and the names of failed checks |
| `cost_usd`, `duration_s`, token columns, `tool_calls`, `model_requests` | Usage as recorded by Scout; costs are notional |

[`results/SOURCES.md`](../results/SOURCES.md) records which saved result set each block of rows came from and how it was
graded. [`scripts/export_public_results.py`](../scripts/export_public_results.py) is the script that produced the CSV
from the private run records; it is published for transparency and cannot run without them.

## Level 3: re-run the study on your own data

### 1. Build a snapshot

Scout reads a read-only SQLite file at `m0/data/pai_dump_2026-09-06.sqlite` (path set by `DEFAULT_DB` in
`scout/tools/pai_tools.py`). `scripts/create_demo_data.py` is the minimal working example of the schema:

| Table | Columns Scout uses |
|---|---|
| `job_postings_raw` | `id`, `source`, `source_id`, `company`, `title`, `location`, `run_date` (first captured), `last_seen_date`, `is_us`, `posted_date`, `url`, `description_text` |
| `job_postings_enriched` | `raw_id`, `pipeline_version` (rows with `v1.0` are used), `company_normalized`, `normalized_title` |
| `fetch_log` | `source`, `run_date`, `status` (`ok`, `partial`, `failed`), `records_fetched`, `records_inserted`, `error_message` |

Set `DEFAULT_SOURCES`, `FIRST_RUN` and `LAST_RUN` in `scout/tools/pai_tools.py` to your sources and collection dates.
Windows are named `pai-YYYY-MM` (a calendar month) or `pai-w-YYYY-MM-DD` (seven days from that date; the study used
Mondays). For `partial` fetch days, `error_message` lists the failed boards as `board: reason; board: reason`.

### 2. Write tasks

A task is a JSON file with an `id` and a `request` object (`task`, `snapshot_ids`, `max_cost_usd`, `deadline_seconds`,
`require_complete_results`); see `eval/tasks/fresh-v1/F-R1.json`. Keep answer keys in files whose names start with `_`:
`run_matrix.py` never loads them as tasks, and git ignores them under `eval/tasks/`.

### 3. Run the setups

With Claude Code installed and logged in (the study's setting; costs are notional on a subscription):

```bash
SCOUT_PROVIDER=claude-code SCOUT_RUNS_DB=eval/results/my-store.sqlite SCOUT_SPEND_CAP_USD=40 \
  .venv/bin/uvicorn scout.service.api:app --port 8792 &
.venv/bin/python eval/run_matrix.py --api http://127.0.0.1:8792 --tasks path/to/your/tasks \
  --models sonnet --designs v5 --reps 3 --concurrency 2 --cap-usd 15 --wait-seconds 900 --label my-sonnet-v5
```

Use `SCOUT_PROVIDER=anthropic` with `ANTHROPIC_API_KEY` to bill the Messages API instead. Run records land in
`eval/results/<label>/runs/`. For the original 11-task layout, `eval/run_original_suite.py` wraps the same steps and
grades with grader v3 at the end.

### 4. Grade

Graders are task-specific, because each task has its own right answer:

- **Facts by code.** Follow `eval/tasks/fresh-v1/grader_fresh.py`: one function per task that checks counts, ids, labels
  and verbatim quotes against your key, using the helpers in `grader_common.py`.
- **Judgment by rubric.** Follow `eval/tasks/fresh-v1/judge.py`: yes/no rubric items, each answered by two judge models
  through the Claude Code CLI; both must say yes, and a disagreement goes to a person.
- **Entry point:** `.venv/bin/python eval/tasks/fresh-v1/grade_fresh.py eval/results/<label>` grades a results folder
  for the fresh suite; copy the folder as a template for your own suite.

Freeze tasks, keys and grader (hash them, as in `eval/tasks/fresh-v1/FREEZE.json`) before the first attempt, and hold
out tasks you never tune against.

## What an exact re-run would need

The original snapshot at `m0/data/pai_dump_2026-09-06.sqlite`, plus `eval/tasks/final/_keys.json` for the original
suite, and `eval/tasks/fresh-v1/_keys.json` and `rubric.json` for the fresh suite. With those in place,
`eval/run_original_suite.py` and `eval/run_matrix.py` re-run any setup, and the published graders score it. Model
outputs vary from attempt to attempt, so a re-run reproduces the distribution, not the exact pass counts.
