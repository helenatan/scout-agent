# fresh-v1 decisions (Helena, 2026-10-01)

Principle: where a reasonable reader could go either way, accept either answer if the answer states the choice it made;
a silent alternative fails. Applied in draft 2 (rubric `fresh-v1-rubric-draft-2`,
sha256 `0ccdd97f7536776effcb2e4575004620c673f30576346bc6163b7bda01dcd1ea`).

| # | Decision | Applied in |
|---|---|---|
| 1 | F-R1: distinct 5, or 4 only when the answer says it merged the two-space record 89d7b687 with the single-space Embedded records | Part 1 `distinct_count_5_or_4` (literal "4" fail removed); key `accepted_distinct`; rubric F-R1.double_space rewritten (silent 4 fails), F-R1.groups accepts grouping consistent with the stated count (rewritten a second time after calibration to judge grouping only) |
| 2 | F-R2: jsearch 0-record day 2026-07-17 is a required limitation | key `required_limitations`; new rubric item F-R2.zero_day |
| 3 | F-E1: Pika = required only | already so; no change |
| 4 | F-E2: Cook accepts mentioned or preferred with the "only reduces the experience requirement" explanation (F-E2.cook_substitute); Partnerverse kept, the Business-Analyst / Antier remark is an accepted qualification and never penalised | key `accepted_qualifications`; F-E2.sections key facts; calibration E2-c3-antier passes |
| 5 | F-C1/F-C3 shared record 6a4e5934 accepted | no change |
| 6 | F-I1: default counts (distinct 5->9, qualifying 3->5), or a consistent stated variant: Walmart Connect merged (5->8, 3->4) or stored 'walmart' only (raw 4->11, distinct 4->8, qualifying 3->4); verdict "not supported" on both halves | key `accepted_variants`; Part 1 `counts_from_one_accepted_variant` (mixed variants fail); new rubric item F-I1.scope_choice; part1/part2 key facts list variants |
| 7 | F-I2: "1 of 3 assessable, 1 not assessable (5944d124)"; Part 1 accepts qualifying 1 with denominator 3 or 4; "1 of 4" without disclosure fails | key `qualifying_assessment` (also records the distinct basis, 1 of 2 assessable, accepted); new rubric item F-I2.not_assessable; required limitation added |
| 8 | F-I1 and F-I2 deadline 400 s, budget 1.5 | `build_tasks.py`, F-I1.json, F-I2.json |
| 9 | Judge prompt-size guard | `judge.py` MAX_PROMPT_CHARS = 60,000; over-long prompt -> needs_human, reason `prompt_too_long`, no truncation, no model call; pytest `test_judge_prompt_size_guard`; longest calibration prompt 4,226 chars |
| 10 | Calibration for every new branch | 48 answers / 150 judgments per judge: 150/150 intended, 150/150 judge-vs-judge; 154 judge calls this round (388 total) |
| 11 | Housekeeping | `_keys.json`, tasks and rubric rebuilt from build scripts; Part 1 pytest 25/25; REVIEW.md marked; `_manifest.json` updated |

Clarification on 7: "1 of 3 assessable" holds on a record basis (4 later records, 3 assessable). On a distinct basis the
later week has 3 openings (Core Platform captured twice), of which 2 are assessable, so "1 of 2 assessable" is accepted too.

## Remaining notes, decided 2026-10-01 (recorded by the operator session)

| # | Decision | Basis |
|---|---|---|
| 12 | Weekly windows only; monthly windows not reused | Helena agreed: monthly windows are the ones the tools were tuned on |
| 13 | eval/tasks/dev overlap checked: `audit_overlap.py --extra ../dev` finds no posting-id overlap (shared weeks pai-w-2026-07-20 and pai-w-2026-08-24, different questions). A scan of all 2,675 stored run files found 1 of 105 fresh posting ids (697037b8, in F-C3's 06-29 cohort), seen only as an incidental row of a broad 'Cognition' search in old C3 runs; accepted | operator check, 2026-10-01 |
| 14 | Use of `_overlap_audit.json` (exposure ids only) accepted | Helena agreed; operator verified 0 tool calls touching forbidden paths |
