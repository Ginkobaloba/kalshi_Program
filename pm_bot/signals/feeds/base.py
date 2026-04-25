"""
Abstract base for news/signal feeds.

A feed produces a stream of NewsItems. Each item has:
  - text content (what was said)
  - timestamp (when it was published)
  - source (which feed it came from)
  - tags (entity hints — player names, team names, dates)
  - severity (rough classification: info, watch, breaking)
  - raw (original data for debugging)

Feeds are deduplicating — calling poll() returns ONLY items new since
the last call. Implementations track the last-seen marker (timestamp,
ID, ETag, etc.) internally.

Feeds are expected to be cheap to call — caller polls every 30-60s.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass
class NewsItem:
    """A single piece of news from a feed."""
    text: str
    timestamp: datetime
    source: str
    severity: str = "info"          # info | watch | breaking
    tags: dict[str, Any] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)
    item_id: str = ""               # source-specific stable ID for dedup


class NewsFeed(abc.ABC):
    """All news feeds implement this interface."""

    name: str = "abstract"

    @abc.abstractmethod
    def poll(self) -> list[NewsItem]:
        """
        Return new items since last call. Should be deduplicating —
        only return items the caller hasn't seen yet.
        """
