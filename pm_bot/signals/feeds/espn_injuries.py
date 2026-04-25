"""
ESPN NBA injury feed.

ESPN publishes injury statuses on a public web page that updates in
near-real-time. We poll their underlying JSON endpoint — no auth needed,
no rate limit issues at our cadence.

Endpoint: https://site.api.espn.com/apis/site/v2/sports/basketball/nba/news
Backup:   https://www.espn.com/nba/injuries

Each item has:
  - player name (e.g. "LeBron James")
  - team (e.g. "Los Angeles Lakers")
  - status ("Out", "Doubtful", "Questionable", "Probable", "Day-to-day")
  - description (brief reason)
  - last_update timestamp

The interesting moments for trading:
  - Status changes from "Probable" to "Out" right before tip-off
  - "Game-time decision" resolved
  - Late scratches (most valuable — game line moves seconds after)

We track the last-seen state per player and emit a NewsItem only when
status changes. New unseen players are emitted as `info`, status
worsening (e.g. Probable -> Out) is emitted as `breaking`.
"""

from __future__ import annotations

from datetime import datetime, timezone

import requests

from pm_bot.logger import get_logger
from pm_bot.signals.feeds.base import NewsFeed, NewsItem

log = get_logger("feed.espn_injuries")

# Status severity ordering — higher = worse for the player's team
STATUS_SEVERITY = {
    "Probable": 1,
    "Day-to-day": 2,
    "Questionable": 3,
    "Doubtful": 4,
    "Out": 5,
    "Suspended": 5,
    "Inactive": 5,
}


class ESPNInjuriesFeed(NewsFeed):
    name = "espn_injuries_nba"

    BASE_URL = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/injuries"
    USER_AGENT = "pm_bot/0.2.0 (research)"

    def __init__(self) -> None:
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": self.USER_AGENT})
        # Last-seen status per player, keyed by player_id
        self._seen: dict[str, str] = {}
        # Whether we've done first poll (suppress noise on initial load)
        self._primed = False

    def poll(self) -> list[NewsItem]:
        try:
            resp = self.session.get(self.BASE_URL, timeout=10)
            resp.raise_for_status()
            data = resp.json()
        except (requests.RequestException, ValueError) as e:
            log.warning("ESPN injuries fetch failed: %s", e)
            return []

        items: list[NewsItem] = []
        # ESPN response shape (verified April 2026):
        # { "injuries": [
        #     { "id": "1", "displayName": "Atlanta Hawks", "abbreviation": "ATL",
        #       "injuries": [ {athlete, status, date, longComment, ...}, ...] },
        #     ...
        #   ]
        # }
        teams = data.get("injuries", []) or []
        for team_block in teams:
            team_name = team_block.get("displayName") or "Unknown"
            team_abbr = team_block.get("abbreviation") or ""

            for inj in team_block.get("injuries", []) or []:
                athlete = inj.get("athlete") or {}
                player_name = athlete.get("displayName") or ""
                status = inj.get("status") or "Unknown"
                description = inj.get("longComment") or inj.get("shortComment") or ""

                # ESPN doesn't reliably populate athlete.id; try to extract
                # from the player-card link URL, fall back to name+team key.
                player_id = self._extract_player_id(athlete) or f"{team_name}::{player_name}"

                if not player_name:
                    continue

                prior_status = self._seen.get(player_id)
                self._seen[player_id] = status

                # Suppress emission on first poll — just learn current state
                if not self._primed:
                    continue

                # Emit only if status changed
                if prior_status == status:
                    continue

                severity = self._classify(prior_status, status)
                # Use 'now' as timestamp since ESPN doesn't always expose change time
                # Prefer ESPN's reported date if available
                espn_date = inj.get("date")
                ts = datetime.now(timezone.utc)
                if espn_date:
                    try:
                        ts = datetime.fromisoformat(
                            espn_date.replace("Z", "+00:00")
                        )
                    except ValueError:
                        pass

                items.append(NewsItem(
                    text=(
                        f"{player_name} ({team_abbr}) status: "
                        f"{prior_status or 'unknown'} -> {status}"
                    ),
                    timestamp=ts,
                    source=self.name,
                    severity=severity,
                    tags={
                        "player_id": player_id,
                        "player_name": player_name,
                        "team_name": team_name,
                        "team_abbr": team_abbr,
                        "status_from": prior_status,
                        "status_to": status,
                    },
                    raw={"description": description, "espn_inj": inj},
                    item_id=f"espn-inj-{player_id}-{status}",
                ))

        if not self._primed:
            log.info("ESPN injuries: primed with %d players, no items emitted", len(self._seen))
        self._primed = True

        if items:
            log.info("ESPN injuries: %d status changes detected", len(items))
        return items

    @staticmethod
    def _extract_player_id(athlete: dict) -> str:
        """ESPN buries the player id in the playercard link URL.
        Returns empty string if not found."""
        for link in (athlete.get("links") or []):
            href = link.get("href") or ""
            # e.g. https://www.espn.com/nba/player/_/id/4585618/keshon-gilbert
            if "/id/" in href:
                try:
                    return href.split("/id/")[1].split("/")[0]
                except (IndexError, AttributeError):
                    continue
        return ""

    @staticmethod
    def _classify(prior: str | None, new: str) -> str:
        """
        Classify severity of a status change for trading purposes.
          - 'breaking': moved to Out / Suspended (line will move materially)
          - 'watch':    moved to Doubtful / Questionable
          - 'info':     otherwise (Probable upgrades, additions, etc.)
        """
        new_sev = STATUS_SEVERITY.get(new, 0)
        prior_sev = STATUS_SEVERITY.get(prior or "", 0)

        if new_sev >= 5:
            return "breaking"
        if new_sev >= 3 and new_sev > prior_sev:
            return "watch"
        return "info"
