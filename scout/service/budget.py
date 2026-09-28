"""Rate card, per-request cost, and the reservation ledger (spec Section 8, PRD Appendix B.5)."""
from __future__ import annotations

import math
from dataclasses import dataclass, field

RATE_CARD_VERSION = "2026-09-13"
# USD per million tokens: (input, output). Cache write = 1.25x input, cache read = 0.1x input.
RATES: dict[str, tuple[float, float]] = {
    "claude-haiku-4-5-20251001": (1.0, 5.0),
    "claude-opus-5": (5.0, 25.0),
}
CACHE_WRITE_MULT = 1.25
CACHE_READ_MULT = 0.10


def rates_for(model_id: str) -> tuple[float, float]:
    key = model_id.split(":", 1)[1] if model_id.startswith("fake:") else model_id
    if key not in RATES:
        raise KeyError(f"no rate for model {model_id}")
    return RATES[key]


def request_cost(model_id: str, usage: dict) -> float:
    i, o = rates_for(model_id)
    return (
        usage.get("input_tokens", 0) * i
        + usage.get("cache_creation_tokens", 0) * i * CACHE_WRITE_MULT
        + usage.get("cache_read_tokens", 0) * i * CACHE_READ_MULT
        + usage.get("output_tokens", 0) * o
    ) / 1e6


def reservation_for(model_id: str, input_tokens_estimate: int, max_output_tokens: int) -> float:
    """Conservative: every input token priced as a cache write, plus the full output allowance."""
    i, o = rates_for(model_id)
    return (input_tokens_estimate * i * CACHE_WRITE_MULT + max_output_tokens * o) / 1e6


def estimate_tokens_from_chars(chars: int) -> int:
    return int(math.ceil(chars / 3))


@dataclass
class Ledger:
    max_usd: float
    confirmed: float = 0.0
    outstanding: dict[str, float] = field(default_factory=dict)  # request_id -> reserved
    unsettled: set[str] = field(default_factory=set)  # requests whose charge is unknown
    over_reservations: list[dict] = field(default_factory=list)

    def available(self) -> float:
        return self.max_usd - self.confirmed - sum(self.outstanding.values())

    def reserve(self, request_id: str, amount: float) -> bool:
        if amount > self.available() + 1e-12:
            return False
        self.outstanding[request_id] = amount
        return True

    def settle(self, request_id: str, actual_usd: float) -> None:
        reserved = self.outstanding.pop(request_id, 0.0)
        self.unsettled.discard(request_id)
        self.confirmed += actual_usd
        if actual_usd > reserved + 1e-12:
            self.over_reservations.append({"request_id": request_id, "reserved": reserved, "actual": actual_usd})

    def mark_unsettled(self, request_id: str) -> None:
        """Charge unknown: keep the reservation outstanding and remember the request."""
        self.unsettled.add(request_id)

    def release(self, request_id: str) -> None:
        """Request never reached the provider (no charge possible)."""
        self.outstanding.pop(request_id, None)
        self.unsettled.discard(request_id)

    def settled(self) -> bool:
        return not self.outstanding and not self.unsettled
