"""Read-only view over the cached facts, for the solver's hot path.

Loading from code/facts_cache/facts.json never touches the network. A run with
no cache yields an empty store, so the solver degrades to the structured
dataset alone rather than failing.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Optional

from facts.llm import FactCache
from facts.schema import ImageFact, MessageFact


@dataclass
class FactStore:
    messages: list[MessageFact] = field(default_factory=list)
    images: list[ImageFact] = field(default_factory=list)
    _by_user: dict[str, list[MessageFact]] = field(default_factory=dict)

    @classmethod
    def empty(cls) -> "FactStore":
        return cls()

    @classmethod
    def load(cls, path: Optional[Path] = None, user_of=None) -> "FactStore":
        cache = FactCache(path) if path else FactCache()
        messages, images = [], []
        for raw in cache.data.get("messages", {}).values():
            try:
                messages.append(MessageFact.model_validate(raw))
            except Exception:
                continue
        for raw in cache.data.get("images", {}).values():
            try:
                images.append(ImageFact.model_validate(raw))
            except Exception:
                continue
        return cls(messages=messages, images=images)

    def index_by_user(self, ds) -> "FactStore":
        """Attach each message fact to its user via messages.csv."""
        owner = {m.message_id: m.user_id for m in ds.messages}
        self._by_user = {}
        for fact in self.messages:
            uid = owner.get(fact.message_id)
            if uid:
                self._by_user.setdefault(uid, []).append(fact)
        return self

    def messages_for(self, user_id: str) -> list[MessageFact]:
        return self._by_user.get(user_id, [])

    def image_amounts(self) -> dict[str, Decimal]:
        """event_id -> amount, for events whose amount is blank in the CSV."""
        return {f.event_id: f.amount for f in self.images
                if f.event_id and f.amount is not None}
