# Final evaluation results (provisional)

> **Context.** This is the September 27 final evaluation of Haiku 4.5 and Opus 5 with basic and summary tools, under its
> official dual-reviewer grading. It is one part of the study; the same attempts graded with grader v3, and the later
> Sonnet and fresh-task experiments, are in [results.md](results.md).

Label `final-2026-09-27-cc-v1`, generated 2026-09-28. **Provisional: 40 of 119 completed answers await human review.** No recommendation is made until the queue is complete; pending answers are neither passes nor failures.

## Design

11 final tasks × A/B/C/D × 3 repetitions = 132 isolated attempts, interleaved and sequential, frozen in `final-freeze-v2-claude-code` before any outcome existed.
Execution ran through headless Claude Code on the owner's subscription (`claude-code-cli-v1`): the shared prompt, Scout's three data tools plus `submit_answer` over an MCP bridge, with runtime validation, one repair and deadlines owned by Scout's engine.

## Configurations

| | Model + tools | Confirmed passes | Awaiting review | Notional cost / success | Median time |
|---|---|---|---|---|---|
| A | Haiku 4.5 + baseline | 0 / 33 | 18 | — | 20.8 s |
| B | Opus 5 + baseline | 29 / 33 | 4 | $0.209 | 44.5 s |
| C | Haiku 4.5 + redesigned | 2 / 33 | 14 | $0.544 | 24.4 s |
| D | Opus 5 + redesigned | 29 / 33 | 4 | $0.220 | 43.8 s |

Total notional cost: $14.47 (rate card 2026-09-13 × tokens reported by Claude Code; not billed). 13 attempts (6 A, 7 C) ended incomplete because runtime checks still failed after the one repair and count as failures. No deadline misses.

Confirmed pass counts are lower bounds while answers are pending; cost per success is an upper bound.

## Grading status

132 attempts: 13 runtime failures; 79 completed answers graded by two-reviewer consensus; 0 of 40 human-review items decided; 40 still pending (not counted as passes or failures).

Human queue: 40 items (27 × Independent reviewers disagree on at least one criterion, 11 × Predeclared sample: one completed answer per task, 11 × At least one reviewer flagged uncertainty; categories overlap).

## Disclosures

- Execution moved from the Anthropic API runner to headless Claude Code on the owner's subscription (claude-code-cli-v1). Final results are not pooled with, and not directly comparable to, the API development rehearsals.
- Costs are notional token-based estimates, not billed charges. Budgets are enforced on streamed usage after each request, not by pre-request reservation.
- Claude Code harness differences: MCP tool names carry an mcp__scout__ prefix; Claude Code adds a small amount of harness context; it controls prompt caching, and thinking for Opus (Haiku thinking disabled via MAX_THINKING_TOKENS=0). Claude Code startup time counts toward each attempt's duration.
- Graders are two Claude models from one vendor (Opus 5.5, Fable 5.1); errors may be correlated. Human sample and disagreement review is not full human grading.
- One development task wording ambiguity (DEV-E2) disclosed; development key unchanged.
- R3/E1/E3 aggregate-only exposure retained under existing owner policy; exact repeated payload verified.
- Final task outcomes are not fully independent: some records/recaptures are shared within the suite.
- Identity rule is stored company normalization + lower(trim(title)); it can merge distinct real vacancies.
- Private source data is not redistributed; public code cannot reproduce data-derived findings.
- Instruction-enforced isolation had slips: several reviewer-a sessions briefly wrote helper scripts one level above their task folder (reviewer-a/ or the scratchpad root), mostly deleted before finishing; two reviewer-b sessions had an attempt to read a persisted tool-output file outside their folder denied by permissions. No reviewer reported reading the other reviewer's judgments. Disclosed; not independently verifiable.
