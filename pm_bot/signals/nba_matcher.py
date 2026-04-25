"""
Match NBA injury news to Kalshi NBA game tickers.

For each breaking ESPN injury update, we want to:
  1. Identify the player's team
  2. Find their next scheduled game on Kalshi
  3. Estimate the price impact based on player tier (star vs role player)
  4. Compare to current Kalshi yes_bid/yes_ask — emit signal if gap exists

Tiers and impact (rough — calibrate from data after running):
  STAR (LeBron, Curry, Doncic, etc.) status -> Out:    line moves ~8-12 cents
  STARTER (rotation regular)            status -> Out:    line moves ~3-5 cents
  ROLE PLAYER                           status -> Out:    line moves ~0-2 cents

These are the team's WIN PROB drops, so for "Player X Out", buy NO on
their team's win, OR buy YES on the opponent's win.

Star list is editable in config. Default is a hand-picked list of high-
impact NBA players as of April 2026.
"""

from __future__ import annotations

from pm_bot.exchanges.base import ExchangeAdapter
from pm_bot.logger import get_logger

log = get_logger("nba_matcher")

# Hand-curated NBA stars whose absence materially moves win prob.
# Update when superstars retire / get traded.
NBA_STARS_2026 = {
    "Nikola Jokic", "Shai Gilgeous-Alexander", "Luka Doncic",
    "Giannis Antetokounmpo", "Jayson Tatum", "Stephen Curry",
    "LeBron James", "Anthony Edwards", "Joel Embiid",
    "Donovan Mitchell", "Damian Lillard", "Kawhi Leonard",
    "Devin Booker", "Kevin Durant", "Anthony Davis",
    "Tyrese Haliburton", "Victor Wembanyama", "Paolo Banchero",
    "Jalen Brunson", "Trae Young", "Karl-Anthony Towns",
    "De'Aaron Fox", "Pascal Siakam", "Jaylen Brown",
}

# Map team display name to Kalshi 3-letter code
TEAM_TO_KALSHI = {
    "Atlanta Hawks": "ATL", "Boston Celtics": "BOS", "Brooklyn Nets": "BKN",
    "Charlotte Hornets": "CHA", "Chicago Bulls": "CHI", "Cleveland Cavaliers": "CLE",
    "Dallas Mavericks": "DAL", "Denver Nuggets": "DEN", "Detroit Pistons": "DET",
    "Golden State Warriors": "GSW", "Houston Rockets": "HOU", "Indiana Pacers": "IND",
    "LA Clippers": "LAC", "Los Angeles Lakers": "LAL", "Memphis Grizzlies": "MEM",
    "Miami Heat": "MIA", "Milwaukee Bucks": "MIL", "Minnesota Timberwolves": "MIN",
    "New Orleans Pelicans": "NOP", "New York Knicks": "NYK", "Oklahoma City Thunder": "OKC",
    "Orlando Magic": "ORL", "Philadelphia 76ers": "PHI", "Phoenix Suns": "PHX",
    "Portland Trail Blazers": "POR", "Sacramento Kings": "SAC", "San Antonio Spurs": "SAS",
    "Toronto Raptors": "TOR", "Utah Jazz": "UTA", "Washington Wizards": "WAS",
}


def estimate_price_impact_cents(
    player_name: str,
    status_from: str | None,
    status_to: str,
) -> float:
    """
    Estimate how much the team's YES win-probability drops on this status change.
    Positive = team WIN prob falls (star is out / hurt worse).
    """
    star = player_name in NBA_STARS_2026

    # Severity transitions of interest
    going_out = status_to in ("Out", "Suspended", "Inactive")
    becoming_questionable = status_to in ("Questionable", "Doubtful")
    coming_back = status_to in ("Probable", "Day-To-Day") and status_from in ("Out", "Doubtful")

    if going_out and (status_from in (None, "Probable", "Day-To-Day", "Questionable")):
        return 10.0 if star else 3.0
    if becoming_questionable and status_from in (None, "Probable"):
        return 4.0 if star else 1.0
    if coming_back:
        return -5.0 if star else -1.0  # team prob goes UP
    return 0.0


def find_team_next_game(
    kalshi: ExchangeAdapter,
    team_abbr: str,
) -> dict | None:
    """
    Find Kalshi's next open game ticker for a given team. Returns:
      {"event": event_ticker, "ticker": market_ticker, "yes_bid": ..., "yes_ask": ..., "is_home_or_away": "MIN" or "DEN" etc}
    or None if not found.

    Kalshi NBA game event tickers look like KXNBAGAME-<DATE>-<AWAY><HOME>.
    Each event has 2 markets, one per team. We find any open game with
    this team in either position.
    """
    # Pull list of NBA game events
    try:
        data = kalshi._request(
            "GET", "/events",
            params={"series_ticker": "KXNBAGAME", "status": "open", "limit": 50},
        )
    except Exception as e:
        log.warning("Kalshi /events query failed: %s", e)
        return None

    events = data.get("events") or []
    for ev in events:
        ev_ticker = ev.get("event_ticker") or ""
        # Format: KXNBAGAME-26APR27MINDEN — last 6 chars are AWY+HME team codes
        # Sometimes shorter codes (3+3) sometimes paired
        if team_abbr not in ev_ticker:
            continue
        # Pull markets for this event
        try:
            markets = kalshi.list_markets(event_id=ev_ticker, limit=5)
        except Exception:
            continue
        for m in markets:
            if m.ticker.endswith(f"-{team_abbr}"):
                return {
                    "event": ev_ticker,
                    "ticker": m.ticker,
                    "yes_bid": m.yes_bid,
                    "yes_ask": m.yes_ask,
                    "no_bid": m.no_bid,
                    "no_ask": m.no_ask,
                    "title": m.title,
                }
    return None
