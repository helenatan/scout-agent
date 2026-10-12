# Tool versions

All five versions expose the same three tools with the same names and input schemas, over the same frozen data.
They differ only in what each result carries. Choose a version with `--design` (runners) or `tool_design` (HTTP API).

| Version | `tool_design` | Module | What it changes | Why it was built |
|---|---|---|---|---|
| Basic tools (v1) | `baseline` | `scout/tools/pai_tools.py` | Search returns 25 records per page; `get_job_posting` returns full text; the collection record is a separate `get_dataset_context` call per time window | The control |
| Summary tools (v2) | `redesigned` | `scout/tools/pai_tools.py` | Adds `mode="counts"` (raw and distinct counts over the whole search in one call), `mode="duplicate_groups"`, posting excerpts and sections. Every search result carries a compact `collection_context` that points to `get_dataset_context` for dates | Opus and Haiku spent many calls paging and counting by hand |
| Explicit caveats (v3) | `tuned` | `scout/tools/tuned.py` | v2, plus every search and dataset-context result starts with `limitations_to_report`: the window's data gaps as finished sentences, with a tool description telling the model to copy each one that applies | To stop missed caveats (experiment design) |
| Dated summary (v4) | `dated` | `scout/tools/dated.py` | v2, plus `collection_context` carries the dated issue days per source, the window length and window notes; no sentences | To test whether Sonnet's misses came from dates sitting one call away (experiment design) |
| Final tools (v5) | `v5` | `scout/tools/v5.py` | v4's dated context and v3's caveat sentences together; counts mode also returns counts per source and names up to 10 duplicate groups | To close every measured gap in one design with fewer calls |

v3 and v4 were first run from an isolated experiment copy of this code. The modules here were ported from it and
produce byte-identical tool definitions and results (checked on 30 calls across six time windows of the original
snapshot). Everything they return is computed from the dataset; nothing is task-specific.

Try any version directly, without a model:

```python
from scout.tools.pai_tools import PAIData, execute_tool
data = PAIData()  # m0/data/pai_dump_2026-09-06.sqlite (the demo snapshot after scripts/create_demo_data.py)
print(execute_tool(data, "v5", "search_job_postings", {"snapshot_id": "pai-2026-08", "mode": "counts"}))
```

Or serve one version over MCP: `python -m scout.tools.mcp_server --design v5`.

## One request, five responses

The same request in every version: count the PM postings that mention "AI agent" in the week of July 13, 2026 (`snapshot_id="pai-w-2026-07-13"`, `text="AI agent"`; v1 has no counts mode, so it gets the first page of records). That week includes a silent gap: jsearch reported success on July 17 but fetched zero records.

These are real responses from the study's snapshot, generated with `execute_tool`. Fields every response repeats (the request filters, a query fingerprint, the dataset version, the identity rule and the aggregation status) are left out, and long lists are shortened.

### v1: Basic tools (`baseline`)

The first page of matching postings, with no counts and nothing about collection. To count, the model pages through every result; to learn about data gaps, it must call `get_dataset_context` for the week.

```json
{
  "total_count": 8,
  "returned": 8,
  "offset": 0,
  "next_cursor": null,
  "postings": [
    {
      "posting_id": "5fb33638-3c2d-4b63-b057-d2de92e868e2",
      "source": "ashby",
      "company": "OpenAI",
      "title": "Product Manager, Codex Security Controls & Partner Interfaces",
      "company_normalized": "openai",
      "location": "Remote - US",
      "first_seen": "2026-07-13",
      "last_seen": "2026-09-05",
      "is_us": true,
      "description_chars": 9416
    },
    {
      "posting_id": "8595a4ab-ec3d-4295-80f9-219368cd787a",
      "source": "greenhouse",
      "company": "Scale AI",
      "title": "Forward Deployed Product Manager, Public Sector",
      "company_normalized": "scale ai",
      "location": "New York, NY; Washington, DC",
      "first_seen": "2026-07-14",
      "last_seen": "2026-09-05",
      "is_us": true,
      "description_chars": 11261
    },
    "... 6 more postings in the same shape"
  ]
}
```

### v2: Summary tools (`redesigned`)

One call returns the counts (`mode="counts"`). The `collection_context` says jsearch had one day with zero records, but not which day, and points to `get_dataset_context` for the details. Note that `overall_status` still says `complete`.

```json
{
  "mode": "counts",
  "counts": {
    "raw": 8,
    "distinct": 7
  },
  "duplicate_groups": {
    "count": 1,
    "records_in_groups": 2,
    "cursor": "<cursor for mode=duplicate_groups>"
  },
  "collection_context": {
    "snapshot_id": "pai-w-2026-07-13",
    "window": {
      "start": "2026-07-13",
      "end": "2026-07-19"
    },
    "overall_status": "complete",
    "relevance": "unknown",
    "sources_in_scope": [
      "jsearch",
      "greenhouse",
      "ashby"
    ],
    "issues": [
      {
        "source": "jsearch",
        "status": "complete",
        "days_expected": 7,
        "days_ok_zero_records": 1,
        "days_partial": 0,
        "days_failed": 0,
        "days_missing": 0,
        "failed_boards": []
      }
    ],
    "sources_without_issues": [
      "greenhouse",
      "ashby"
    ],
    "details": "get_dataset_context"
  }
}
```

### v3: Explicit caveats (`tuned`)

The v2 response with `limitations_to_report` added at the top: each data gap written as a sentence the model can copy into its answer.

```json
{
  "limitations_to_report": [
    "pai-w-2026-07-13 covers 2026-07-13 to 2026-07-19 (7 days).",
    "Possible silent gap: jsearch reported status ok but fetched 0 records on 2026-07-17 of pai-w-2026-07-13. Do not treat 'ok' as complete collection; the cause is not recorded, so do not state one as fact.",
    "Counts are postings first captured in this window from the configured sources: they are not live vacancies, all jobs open at the time, or a measure of hiring demand."
  ],
  "mode": "counts",
  "counts": {
    "raw": 8,
    "distinct": 7
  },
  "duplicate_groups": {
    "count": 1,
    "records_in_groups": 2,
    "cursor": "<cursor for mode=duplicate_groups>"
  },
  "collection_context": {
    "snapshot_id": "pai-w-2026-07-13",
    "window": {
      "start": "2026-07-13",
      "end": "2026-07-19"
    },
    "overall_status": "complete",
    "relevance": "unknown",
    "sources_in_scope": [
      "jsearch",
      "greenhouse",
      "ashby"
    ],
    "issues": [
      {
        "source": "jsearch",
        "status": "complete",
        "days_expected": 7,
        "days_ok_zero_records": 1,
        "days_partial": 0,
        "days_failed": 0,
        "days_missing": 0,
        "failed_boards": []
      }
    ],
    "sources_without_issues": [
      "greenhouse",
      "ashby"
    ],
    "details": "get_dataset_context"
  }
}
```

### v4: Dated summary (`dated`)

The v2 response, but `collection_context` now lists each issue day with its date (`2026-07-17`), the window length and any window notes. No sentences.

```json
{
  "mode": "counts",
  "counts": {
    "raw": 8,
    "distinct": 7
  },
  "duplicate_groups": {
    "count": 1,
    "records_in_groups": 2,
    "cursor": "<cursor for mode=duplicate_groups>"
  },
  "collection_context": {
    "snapshot_id": "pai-w-2026-07-13",
    "window": {
      "start": "2026-07-13",
      "end": "2026-07-19",
      "days": 7,
      "nominal_start": "2026-07-13",
      "nominal_end": "2026-07-19",
      "nominal_days": 7
    },
    "overall_status": "complete",
    "sources": {
      "jsearch": {
        "status": "complete",
        "days_expected": 7,
        "days_ok_zero_records": 1,
        "days_partial": 0,
        "days_failed": 0,
        "days_missing": 0,
        "failed_boards": [],
        "issue_days": [
          {
            "date": "2026-07-17",
            "issue": "ok_zero_records",
            "detail": "status ok but 0 records fetched"
          }
        ]
      },
      "greenhouse": "... same shape, no issue days",
      "ashby": "... same shape, no issue days"
    },
    "window_notes": []
  }
}
```

### v5: Final tools (`v5`)

v3's sentences and v4's dated context together, plus counts per source (`by_source`) and the name of each duplicate group.

```json
{
  "limitations_to_report": [
    "pai-w-2026-07-13 covers 2026-07-13 to 2026-07-19 (7 days).",
    "Possible silent gap: jsearch reported status ok but fetched 0 records on 2026-07-17 of pai-w-2026-07-13. Do not treat 'ok' as complete collection; the cause is not recorded, so do not state one as fact.",
    "Counts are postings first captured in this window from the configured sources: they are not live vacancies, all jobs open at the time, or a measure of hiring demand."
  ],
  "mode": "counts",
  "counts": {
    "raw": 8,
    "distinct": 7
  },
  "duplicate_groups": {
    "count": 1,
    "records_in_groups": 2,
    "cursor": "<cursor for mode=duplicate_groups>",
    "groups": [
      {
        "company": "elastic",
        "title": "principal product manager agents and context - elasticsearch",
        "records": 2,
        "sources": [
          "greenhouse"
        ]
      }
    ],
    "groups_truncated": false
  },
  "collection_context": {
    "snapshot_id": "pai-w-2026-07-13",
    "window": {
      "start": "2026-07-13",
      "end": "2026-07-19",
      "days": 7,
      "nominal_start": "2026-07-13",
      "nominal_end": "2026-07-19",
      "nominal_days": 7
    },
    "overall_status": "complete",
    "sources": {
      "jsearch": {
        "status": "complete",
        "days_expected": 7,
        "days_ok_zero_records": 1,
        "days_partial": 0,
        "days_failed": 0,
        "days_missing": 0,
        "failed_boards": [],
        "issue_days": [
          {
            "date": "2026-07-17",
            "issue": "ok_zero_records",
            "detail": "status ok but 0 records fetched"
          }
        ]
      },
      "greenhouse": "... same shape, no issue days",
      "ashby": "... same shape, no issue days"
    },
    "window_notes": []
  },
  "by_source": {
    "jsearch": {
      "raw": 3,
      "distinct": 3
    },
    "greenhouse": {
      "raw": 4,
      "distinct": 3
    },
    "ashby": {
      "raw": 1,
      "distinct": 1
    }
  }
}
```

## Haiku-only extras

The Haiku tuning experiment also tried changes outside the tools. They are not tool versions and are not in this
package; they ran from the isolated experiment copy and are described here so the results in
[results.md](results.md#haiku-tuning) can be read.

- **V0, self-check:** v3 plus one Haiku review of its own draft answer before it is accepted. The only change that held
  up on held-out tasks (28%).
- **V1, drafting tools:** answer-shaped search results and small drafting tools (add a finding, add a limitation, add a
  quote), so Haiku never builds one large answer in a single call. Ended malformed submissions; flagged as overfitting.
- **V2, thinking budget** for Haiku. Flagged as overfitting.
- **V3, attached caveats:** the service attaches every delivered caveat sentence to the answer, so Haiku could not drop
  them. Flagged as overfitting.
