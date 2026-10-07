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
