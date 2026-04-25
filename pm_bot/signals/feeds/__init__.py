"""News/signal feeds. Each feed produces NewsItems on poll."""

from pm_bot.signals.feeds.base import NewsFeed, NewsItem
from pm_bot.signals.feeds.espn_injuries import ESPNInjuriesFeed

__all__ = ["NewsFeed", "NewsItem", "ESPNInjuriesFeed"]
