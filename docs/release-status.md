# Release status

September 27, 2026: research/demo release in preparation; no final result claimed.

- Runtime and local MCP tools implemented; v6 common prompt tested in a completed 32-run development rehearsal ($2.445409 settled, 27 automatic passes).
- Fresh independent agent inspection finds baseline information access sufficient on the three inspected development tasks. One development task has a wording ambiguity; this is disclosed rather than silently regraded.
- Human reference-key review complete. Generated-answer grading is a separate requirement; dual-agent grading with human disagreements and a predeclared sample approved before freeze.
- Final grader rejects stale/incomplete approvals, and the report detects missing/extra final cells. The planned final matrix is 11 tasks × four configurations × three repetitions.
- Developer interviews deferred. Qualification thresholds are owner-chosen assumptions.
- Original job descriptions, private reference keys, raw run records and review journals are excluded from the repository.
- Execution moved to headless Claude Code on the owner subscription (claude-code-cli-v1); 32-attempt subscription rehearsal (27 automatic passes, $2.33 notional) preceded the freeze `final-freeze-v2-claude-code`.
- Final 132 attempts completed September 27 ($14.47 notional). Dual Claude reviewers graded all 119 completed answers; 40 items await human review, so results are provisional (`docs/final-results.md`).
- Static site rebuilt from the owner's design and published as a private claude.ai Artifact. Repository remains private; no license chosen.
