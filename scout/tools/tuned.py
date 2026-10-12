"""Tool design "tuned" (v3, explicit caveats): the redesigned tools plus caveat sentences.

Experiment design, not a product default. Every search_job_postings and get_dataset_context response starts with
limitations_to_report: the window's collection caveats written as finished sentences, and the tool descriptions tell
the model to copy each one that applies. Built during the Haiku tuning experiment and reused for Sonnet 5.5 in the
model-pairs experiment. The caveat sentences are the same ones the v5 design uses (scout/tools/v5.py).
"""
from __future__ import annotations

from typing import Any, Callable

from scout.tools.v5 import CAVEATS_NOTE, collection_caveats


def tool_definitions(redesigned: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for d in redesigned:
        d = dict(d)
        if d["name"] in ("search_job_postings", "get_dataset_context"):
            d["description"] = d["description"] + CAVEATS_NOTE
        out.append(d)
    return out


def execute_tool(data, name: str, args: dict[str, Any], redesigned: Callable[[dict[str, Any]], dict[str, Any]]) -> dict[str, Any]:
    out = redesigned(args)
    if name in ("search_job_postings", "get_dataset_context") and "error" not in out:
        out = {"limitations_to_report": collection_caveats(data, args["snapshot_id"]), **out}
    return out
