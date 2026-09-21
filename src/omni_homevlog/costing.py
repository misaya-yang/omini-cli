"""Cost estimation (§12.2, §22).

Estimates only. `pricing.yaml` carries the rates and states its own staleness;
nothing here is a billing-grade number. The manifest records both the estimate
and the raw usage counts, so a later reconciliation is possible.
"""

from __future__ import annotations

from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from omni_homevlog.config import REPO_ROOT

DEFAULT_PRICING_PATH = REPO_ROOT / "pricing.yaml"


@lru_cache(maxsize=4)
def load_pricing(path: str | None = None) -> dict[str, Any]:
    target = Path(path) if path else DEFAULT_PRICING_PATH
    if not target.exists():
        return {}
    with target.open("r", encoding="utf-8") as handle:
        return yaml.safe_load(handle) or {}


def estimate_video_seconds_cost(
    *,
    model: str,
    video_seconds: int,
    pricing: dict[str, Any] | None = None,
) -> Decimal:
    """Pre-call estimate from a requested duration.

    Used by `Budget.authorize` so the ceiling can bind *before* money is spent.
    """
    data = pricing if pricing is not None else load_pricing()
    model_cfg = (data.get("models") or {}).get(model) or {}
    tokens_per_second = Decimal(str(model_cfg.get("video_tokens_per_second", 0) or 0))
    rate = Decimal(str(model_cfg.get("video_output_usd_per_1k_tokens", 0) or 0))
    per_call = Decimal(str(data.get("per_call_usd", 0) or 0))
    tokens = tokens_per_second * Decimal(video_seconds)
    return (tokens / Decimal(1000) * rate) + per_call


def estimate_from_usage(
    *,
    model: str,
    usage: dict[str, Any] | None,
    pricing: dict[str, Any] | None = None,
) -> Decimal:
    """Post-call estimate from the provider's own token counts.

    Preferred over the duration estimate when available: it reflects what the
    provider says it produced.
    """
    data = pricing if pricing is not None else load_pricing()
    model_cfg = (data.get("models") or {}).get(model) or {}
    if not usage:
        return Decimal("0")

    def _num(*keys: str) -> Decimal:
        for key in keys:
            value = usage.get(key)
            if isinstance(value, (int, float, str)):
                try:
                    return Decimal(str(value))
                except Exception:
                    continue
        return Decimal("0")

    video_out = _num("video_output_tokens", "output_video_tokens", "video_tokens")
    text_in = _num("text_input_tokens", "input_text_tokens", "input_tokens")

    video_rate = Decimal(str(model_cfg.get("video_output_usd_per_1k_tokens", 0) or 0))
    text_rate = Decimal(str(model_cfg.get("text_input_usd_per_1k_tokens", 0) or 0))
    per_call = Decimal(str(data.get("per_call_usd", 0) or 0))

    return (
        (video_out / Decimal(1000) * video_rate) + (text_in / Decimal(1000) * text_rate) + per_call
    )


def pricing_staleness_note(pricing: dict[str, Any] | None = None) -> str:
    data = pricing if pricing is not None else load_pricing()
    updated = data.get("updated_at", "unknown")
    return (
        f"Cost figures are ESTIMATES from pricing.yaml (updated_at={updated}). "
        "Cloud Billing is authoritative."
    )
