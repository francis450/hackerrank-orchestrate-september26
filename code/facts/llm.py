"""Claude client, on-disk fact cache, and token accounting for the facts layer.

Every call is recorded from the first request - model, provider, input and
output tokens - into the ledger that evaluation/usage_report.md is built from.
Cached facts cost zero API calls, so a re-run of the full dataset makes no
requests at all.
"""
from __future__ import annotations

import json
import os
import threading
from dataclasses import asdict, dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any, Optional

CACHE_DIR = Path(__file__).resolve().parents[1] / "facts_cache"
FACTS_PATH = CACHE_DIR / "facts.json"
USAGE_PATH = CACHE_DIR / "usage.json"
PROVIDER = "anthropic"
DEFAULT_MODEL = "claude-sonnet-4-6"

#: USD per 1M tokens, for the cost columns of the usage report.
PRICING: dict[str, tuple[Decimal, Decimal]] = {
    "claude-sonnet-4-6": (Decimal("3.00"), Decimal("15.00")),
    "claude-sonnet-5": (Decimal("2.00"), Decimal("10.00")),
    "claude-opus-5": (Decimal("5.00"), Decimal("25.00")),
    "claude-haiku-4-5": (Decimal("1.00"), Decimal("5.00")),
}

_lock = threading.Lock()


@dataclass
class ModelUsage:
    provider: str = PROVIDER
    model: str = DEFAULT_MODEL
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def cost_usd(self) -> Decimal:
        rate_in, rate_out = PRICING.get(self.model, (Decimal(0), Decimal(0)))
        return (Decimal(self.input_tokens) * rate_in
                + Decimal(self.output_tokens) * rate_out) / Decimal(1_000_000)


@dataclass
class UsageLedger:
    """Per-model token totals, persisted so a partial run is never lost."""
    by_model: dict[str, ModelUsage] = field(default_factory=dict)

    def record(self, model: str, usage: Any) -> None:
        entry = self.by_model.setdefault(model, ModelUsage(model=model))
        entry.calls += 1
        entry.input_tokens += int(getattr(usage, "input_tokens", 0) or 0)
        entry.output_tokens += int(getattr(usage, "output_tokens", 0) or 0)
        entry.cache_read_tokens += int(getattr(usage, "cache_read_input_tokens", 0) or 0)

    @property
    def calls(self) -> int:
        return sum(m.calls for m in self.by_model.values())

    @property
    def total_tokens(self) -> int:
        return sum(m.total_tokens for m in self.by_model.values())

    def cost_usd(self) -> Decimal:
        return sum((m.cost_usd() for m in self.by_model.values()), Decimal(0))

    def save(self, path: Path = USAGE_PATH) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"provider": PROVIDER,
                   "models": {k: asdict(v) for k, v in sorted(self.by_model.items())},
                   "totals": {"calls": self.calls, "total_tokens": self.total_tokens,
                              "cost_usd": str(self.cost_usd().quantize(Decimal("0.000001")))}}
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    @classmethod
    def load(cls, path: Path = USAGE_PATH) -> "UsageLedger":
        if not path.exists():
            return cls()
        raw = json.loads(path.read_text(encoding="utf-8"))
        return cls(by_model={k: ModelUsage(**v) for k, v in raw.get("models", {}).items()})


class FactCache:
    """JSON cache keyed by namespace (messages/images) and record id."""

    def __init__(self, path: Path = FACTS_PATH) -> None:
        self.path = path
        self.data: dict[str, dict[str, Any]] = {"messages": {}, "images": {}}
        if path.exists():
            try:
                self.data.update(json.loads(path.read_text(encoding="utf-8")))
            except json.JSONDecodeError:
                pass

    def get(self, namespace: str, key: str) -> Optional[dict]:
        return self.data.setdefault(namespace, {}).get(key)

    def put(self, namespace: str, key: str, value: dict) -> None:
        self.data.setdefault(namespace, {})[key] = value

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with _lock:
            self.path.write_text(json.dumps(self.data, indent=2, sort_keys=True,
                                            default=str), encoding="utf-8")

    def __len__(self) -> int:
        return sum(len(v) for v in self.data.values())


def get_client():
    """Build the Anthropic client. The key is read from the environment only."""
    import anthropic
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise RuntimeError("ANTHROPIC_API_KEY is not set; extraction cannot run")
    return anthropic.Anthropic()


def call_json(client, model: str, system: str, content: list[dict],
              json_schema: dict, ledger: UsageLedger, max_tokens: int = 1024) -> dict:
    """One structured-output request. Returns the parsed JSON object.

    Token usage is recorded before the response is inspected, so a malformed
    payload still shows up in the cost report.
    """
    response = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": content}],
        output_config={"format": {"type": "json_schema", "schema": json_schema}},
    )
    ledger.record(model, response.usage)
    text = "".join(b.text for b in response.content if b.type == "text")
    return json.loads(text)
