"""Write the M4 freeze for subscription execution (final-v2-claude-code).

    python eval/prepare_freeze_v2.py --api http://127.0.0.1:8021 --rehearsal dev-2026-09-27-cc-v1 --tests-passed N

Fingerprints every file that can change final behaviour or grading, binds the final service's
reported settings, and records gates, disclosures and the notional cost method. It refuses to
write if the final service store is not empty. The v1 file (prepared for the API runner, never
frozen) is left unchanged and referenced as superseded.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
import httpx

from eval.final_analysis import FINAL_TASKS

OUT = ROOT / "eval/readiness/final-freeze-v2-claude-code.json"
FILES = ["scout/service/__init__.py", "scout/service/api.py", "scout/service/budget.py", "scout/service/claude_code.py", "scout/service/engine.py",
         "scout/service/evidence.py", "scout/service/prompt.py", "scout/service/providers.py", "scout/service/schemas.py", "scout/service/store.py",
         "scout/service/validators.py", "scout/tools/__init__.py", "scout/tools/cc_bridge.py", "scout/tools/mcp_client.py", "scout/tools/mcp_server.py",
         "scout/tools/pai_tools.py", "eval/agent_grading.py", "eval/evaluation-plan-v2.md", "eval/export_agent_packets.py", "eval/final_analysis.py",
         "eval/final_grade.py", "eval/final_review_ui.html", "eval/final_review_ui.py", "eval/grade.py", "eval/report.py", "eval/run_final.py",
         "eval/run_matrix.py", "eval/stage_agent_review.py", "eval/grading/agent-policy-v1.json", "eval/grading/agent-policy-v2.json", "eval/grading/reviewer-instructions-v1.md",
         "eval/readiness/agent-grading-calibration-v1.json", "eval/readiness/agent-grading-calibration-claude-v1.json",
         "eval/readiness/aggregate-exposure-policy-v3.json", "eval/readiness/final-verification-post-v6-v1.json",
         "eval/readiness/independent-baseline-v6-review.md", "eval/tasks/final/_keys.json", "m0/data/pai_dump_2026-09-06.sqlite",
         "release/requirements.lock"] + [f"eval/tasks/final/{t}.json" for t in sorted(FINAL_TASKS)]


def sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--api", default="http://127.0.0.1:8021")
    ap.add_argument("--rehearsal", required=True)
    ap.add_argument("--tests-passed", type=int, required=True)
    ap.add_argument("--cap-usd", type=float, default=30.0)
    a = ap.parse_args()
    health = httpx.get(a.api + "/healthz", timeout=10).json()
    if health.get("provider") != "claude-code-cli" or health.get("tool_transport") != "mcp":
        raise SystemExit("final service must run SCOUT_PROVIDER=claude-code with MCP tools")
    if any(health["spend"].get(k) for k in ("confirmed_usd", "committed_usd", "runs")):
        raise SystemExit("final service store must be empty")
    if health["spend"]["cap_usd"] != a.cap_usd:
        raise SystemExit("service notional cap differs from --cap-usd")
    reh = ROOT / "eval/results" / a.rehearsal
    manifest = json.loads((reh / "manifest.json").read_text())
    report = json.loads((reh / "report.json").read_text()) if (reh / "report.json").exists() else {}
    freeze = {
        "status": "frozen",
        "version": "final-v2-claude-code",
        "supersedes": "eval/readiness/final-freeze-v1-prepared.json (API runner; prepared, never frozen or launched)",
        "provider": "claude-code-cli",
        "execution_version": health["settings"]["execution"]["haiku"]["execution_version"],
        "claude_code_version": health["settings"]["execution"]["haiku"]["claude_code_version"],
        "models": {"haiku": "claude-haiku-4-5-20251001", "opus": "claude-opus-5"},
        "design": "11 final tasks x A/B/C/D x 3 repetitions = 132 isolated attempts; interleaved rep -> task -> design -> model; sequential",
        "gates": {"verified_tasks": True, "runtime_tests": True, "development_rehearsal": True, "independent_baseline_review": True,
                  "holdout_policy": True, "grading_method": True, "grader_validation": True, "budget_and_models": True, "qualification_rules": True},
        "gate_notes": {
            "verified_tasks": "final-verification-post-v6-v1.json: SQL/count/group/coverage/quote checks pass for all 11 tasks; reviewed package final-v3-reviewed.",
            "runtime_tests": f"{a.tests_passed} engineering tests passed, including subscription-executor tests with a fake CLI and the real MCP bridge.",
            "development_rehearsal": f"{a.rehearsal}: {manifest.get('launched')} attempts through claude-code-cli-v1 on the 8 development tasks; notional ${manifest.get('spent_usd', 0):.4f}.",
            "independent_baseline_review": "independent-baseline-v6-review.md (agent inspection, not an interview).",
            "holdout_policy": "aggregate-exposure-policy-v3.json: R3/E1/E3 aggregate-only exposure retained with disclosure.",
            "grading_method": "dual-agent-human-audit-v1 with agent-policy-v2.json (Claude reviewers, predeclared per-task batching and sample).",
            "grader_validation": "agent-grading-calibration-claude-v1.json: both Claude reviewers matched all four fictional calibration cases.",
            "budget_and_models": f"Subscription execution; notional cap ${a.cap_usd:.0f} (rate card 2026-09-13 on reported tokens). No API credit is used.",
            "qualification_rules": "Owner-chosen assumptions: 80% observed success, 300 s median time, 15-point non-inferiority margin, 20% cost-saving target.",
        },
        "final_cap_usd": a.cap_usd,
        "cost_method": "Notional list-price equivalent: rate card 2026-09-13 (Haiku 4.5 $1/$5, Opus 5 $5/$25 per MTok; cache write 1.25x, read 0.1x input) applied to per-request usage streamed by Claude Code. Not billed; not comparable to the API development ledger as measured spend.",
        "prior_development_api_usd": 12.179053,
        "qualification_source": "Owner-chosen research/demo assumptions, September 27; interviews deferred",
        "grading_method": "dual-agent-human-audit-v1",
        "grading_policy": "eval/grading/agent-policy-v2.json",
        "human_sample_rule": "lowest SHA256 of sample-seed-0|task_id|config|rep among completed answers per task, selected after all attempts terminate",
        "analysis": "paired whole-task bootstrap, 5,000 resamples, seed 0; thresholds fixed as above; no prompt/key/threshold changes in response to outcomes",
        "service_settings": health["settings"],
        "sha256": {f: sha(ROOT / f) for f in FILES},
        "development_rehearsal_summary": {"label": a.rehearsal, "launched": manifest.get("launched"), "notional_usd": manifest.get("spent_usd"),
                                          "configurations": {k: {x: v.get(x) for x in ("passes", "launched", "success_rate")} for k, v in (report.get("configurations") or {}).items()}},
        "disclosures": [
            "Execution moved from the Anthropic API runner to headless Claude Code on the owner's subscription (claude-code-cli-v1). Final results are not pooled with, and not directly comparable to, the API development rehearsals.",
            "Costs are notional token-based estimates, not billed charges. Budgets are enforced on streamed usage after each request, not by pre-request reservation.",
            "Claude Code harness differences: MCP tool names carry an mcp__scout__ prefix; Claude Code adds a small amount of harness context; it controls prompt caching, and thinking for Opus (Haiku thinking disabled via MAX_THINKING_TOKENS=0). Claude Code startup time counts toward each attempt's duration.",
            "Graders are two Claude models from one vendor (Opus 5.5, Fable 5.1); errors may be correlated. Human sample and disagreement review is not full human grading.",
            "One development task wording ambiguity (DEV-E2) disclosed; development key unchanged.",
            "R3/E1/E3 aggregate-only exposure retained under existing owner policy; exact repeated payload verified.",
            "Final task outcomes are not fully independent: some records/recaptures are shared within the suite.",
            "Identity rule is stored company normalization + lower(trim(title)); it can merge distinct real vacancies.",
            "Private source data is not redistributed; public code cannot reproduce data-derived findings.",
        ],
    }
    OUT.write_text(json.dumps(freeze, indent=1, ensure_ascii=False) + "\n")
    print(f"Wrote {OUT} with {len(freeze['sha256'])} fingerprints")


if __name__ == "__main__":
    main()
