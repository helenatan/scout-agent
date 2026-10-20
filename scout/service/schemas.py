"""Public request/response models for the Scout Agent HTTP API (spec Section 2)."""
from __future__ import annotations

import hashlib
import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

MODEL_IDS = {"haiku": "claude-haiku-4-5-20251001", "opus": "claude-opus-5", "sonnet": "claude-sonnet-5-5"}
MAX_COST_CAP_USD = 5.00


class RunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task: str = Field(min_length=1, max_length=4000)
    snapshot_ids: list[str] = Field(min_length=1, max_length=4)
    max_cost_usd: float = Field(gt=0, le=MAX_COST_CAP_USD)
    deadline_seconds: int = Field(ge=10, le=1800)
    model: Literal["haiku", "opus", "sonnet"]
    tool_design: Literal["baseline", "redesigned", "tuned", "dated", "v5"]
    require_complete_results: bool = False

    @field_validator("task")
    @classmethod
    def _trim(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("task must not be blank")
        return v

    @field_validator("snapshot_ids")
    @classmethod
    def _snapshots(cls, v: list[str]) -> list[str]:
        from scout.tools.pai_tools import ToolError, parse_snapshot_id

        if len(set(v)) != len(v):
            raise ValueError("snapshot_ids must not contain duplicates")
        for s in v:
            try:
                parse_snapshot_id(s)
            except ToolError as e:
                raise ValueError(e.message) from None
        return v

    def canonical_hash(self) -> str:
        payload = json.dumps(self.model_dump(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode()).hexdigest()

    def resolved_model_id(self) -> str:
        return MODEL_IDS[self.model]


class SubmitResponse(BaseModel):
    run_id: str
    state: str
    created_at: str
    deadline_at: str
    links: dict[str, str]
