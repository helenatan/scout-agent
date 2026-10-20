"""The shared system prompt (one version across A–D) and the service-side submit_answer tool."""
from __future__ import annotations

PROMPT_VERSION = "v6"  # 2026-09-27: concise research guidance; field rules in the submission schema.
# v5: claim-scoped evidence and faithful quotes; matrix 3 used this version.
# v3 (2026-09-20): tool names only (search_job_postings, get_job_posting, get_dataset_context); text otherwise as v2.
# v2 (2026-09-13): compact working text, quotes only from posting text, figures convention, stable pagination filters

SYSTEM_PROMPT = """Answer the user's question about product-management job postings using the available tools. Give a clear conclusion supported by the evidence you actually retrieved.

Gather evidence
- Use the requested dates, postings, and filters. Reuse results you already have.
- Get missing collection background from get_dataset_context. A snapshot contains postings first captured during its date window, not all jobs available then.
- For distinct-opening counts, use the published identity rule. Keep raw record counts separate from distinct openings.
- When complete results are required, read every page supporting your answer; keep filters unchanged when following a cursor. A complete tool-provided count can support a count without reading the records. Claims about individual postings still need their records or text.

Answer carefully
- Distinguish changes in captured postings from changes in the job market. Explain relevant collection gaps.
- Base interpretations on the posting text. Include supporting quotes when requested. If the evidence is insufficient, say what remains unknown.
- Be concise. Avoid repeating evidence or narrating every step.

Submit
- Use submit_answer, following its field descriptions and any answer format requested in the task. Cite evidence from this run for every finding and state material limitations.
- If the service requests a correction, address its feedback and resubmit."""


SUBMIT_ANSWER_TOOL = {
    "name": "submit_answer",
    "description": "Return the evidence-backed answer instead of a plain-text final message. "
                   "Submit when the question is answered or the available evidence cannot resolve it. "
                   "If the service requests a correction, fix the reported issues and resubmit.",
    "input_schema": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "summary": {"type": ["string", "null"], "description": "Brief conclusion supported by the findings. For a completed answer, provide non-empty text, including when the conclusion is that evidence is insufficient."},
            "findings": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "statement": {"type": "string", "description": "One supported claim. If the task specifies named figures, use its exact name = value format."},
                        "evidence_scope": {"type": "string", "enum": ["count", "records", "posting_text", "context"], "description": "Choose count for quantities, records for record facts or lists, posting_text for interpretations of posting text, or context for collection metadata. Only count may rely on a complete aggregate instead of all supporting pages. Defaults to records."},
                        "evidence_ids": {"type": "array", "items": {"type": "string"}, "minItems": 1, "description": "IDs of tool results received in this run that support this finding. For a tool-provided count, cite the result that supplied it."},
                        "calculation": {
                            "type": "object",
                            "description": "For a reported numeric change, subtract earlier from later: operands [later, earlier], result later - earlier. For a percentage-point change, express both shares as percentages first.",
                            "additionalProperties": False,
                            "properties": {
                                "operation": {"type": "string", "enum": ["subtract"]},
                                "operands": {"type": "array", "items": {"type": "number"}, "minItems": 2, "maxItems": 2},
                                "result": {"type": "number"},
                            },
                            "required": ["operation", "operands", "result"],
                        },
                    },
                    "required": ["statement", "evidence_ids"],
                },
            },
            "limitations": {"type": "array", "items": {"type": "string"}, "description": "Material caveats or unfinished work that affect this answer; use an empty list if none."},
            "quotes": {
                "type": "array",
                "description": "Include when the task requests supporting quotes. Copy a contiguous passage from a get_job_posting result and cite its evidence_id. Do not quote search results or collection metadata. For an absence claim, cite inspected evidence without inventing a quote.",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {"evidence_id": {"type": "string"}, "quote": {"type": "string"}},
                    "required": ["evidence_id", "quote"],
                },
            },
        },
        "required": ["summary", "findings", "limitations"],
    },
}


def task_message(task: str, snapshot_ids: list[str], require_complete: bool, max_cost_usd: float, deadline_seconds: int) -> str:
    comp = ("Complete results are required: page all supporting record queries, except a count finding may cite a complete aggregate for its query. Text and record-list claims still require their records."
            if require_complete else "Complete results are not required; fetch what the question needs.")
    return (f"{task}\n\nSupported snapshots: {', '.join(snapshot_ids)}.\n{comp}\n"
            f"Budget: ${max_cost_usd:.2f}. Deadline: {deadline_seconds} seconds from acceptance.")
