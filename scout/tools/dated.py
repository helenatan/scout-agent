"""Tool design "dated" (v4, dated summary): the redesigned tools with a complete collection_context.

Diagnostic experiment design, not a product default. Same tools, modes and schemas as "redesigned"; only the
collection_context attached to each search response changes: it carries the dated issue days per source, the window's
nominal length and the window notes, so no second get_dataset_context call is needed. Structured facts only: no
finished sentences and no instruction to copy anything (that is the "tuned" design's lever).
"""
from __future__ import annotations

from typing import Any, Callable

from scout.tools.v5 import CONTEXT_NOTE, _REDESIGNED_CONTEXT, full_context


def tool_definitions(redesigned: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for d in redesigned:
        d = dict(d)
        if d["name"] == "search_job_postings":
            assert _REDESIGNED_CONTEXT in d["description"], "redesigned search description changed; update dated"
            d["description"] = d["description"].replace(_REDESIGNED_CONTEXT, CONTEXT_NOTE)
        out.append(d)
    return out


def execute_tool(data, name: str, args: dict[str, Any], redesigned: Callable[[dict[str, Any]], dict[str, Any]]) -> dict[str, Any]:
    out = redesigned(args)
    if name == "search_job_postings" and "error" not in out and "collection_context" in out:
        out["collection_context"] = full_context(data, args["snapshot_id"], args.get("source"))
    return out
