"""Independent, optional news sources. HTTP failures never escape these clients."""

from datetime import datetime
import requests

from brain.config import load_config
from brain.logging_setup import setup_logging
from brain.news.blackout import UTC, _utc

logger = setup_logging("news_apis")


def _headline(title, source, published_at, url):
    if not isinstance(title, str) or not title.strip():
        raise ValueError("Missing headline")
    if isinstance(published_at, (int, float)):
        published_at = datetime.fromtimestamp(published_at, UTC)
    return {"title": title.strip(), "source": str(source or "Unknown"),
            "published_at": _utc(published_at), "url": str(url or "")}


class _Client:
    key_name = ""

    def __init__(self):
        try:
            self.api_key = getattr(load_config(), self.key_name, None)
        except Exception:
            logger.warning("%s disabled: configuration unavailable", type(self).__name__)
            self.api_key = None

    def fetch_headlines(self, query=None) -> list[dict]:
        if not self.api_key:
            return []
        try:
            url, params = self._request(query)
            response = requests.get(url, params=params, timeout=20)
            response.raise_for_status()
            body = response.json()
            if isinstance(body, dict) and any(k in body for k in ("error", "errors", "error_code", "Note", "Information", "Error Message")):
                raise ValueError("Provider reported failure or rate limit")
            results = []
            for item in self._items(body):
                try:
                    row = self._parse(item)
                    if not query or str(query).casefold() in row["title"].casefold():
                        results.append(row)
                except Exception:
                    logger.warning("%s skipped malformed headline", type(self).__name__)
            return results
        except Exception:
            # Avoid exception text: HTTP exceptions can contain API keys in URLs.
            logger.warning("%s headline request failed", type(self).__name__)
            return []


class FredClient(_Client):
    key_name = "FRED_API_KEY"

    def _request(self, query):
        return "https://api.stlouisfed.org/fred/releases/dates", {
            "api_key": self.api_key, "file_type": "json", "sort_order": "desc",
            "include_release_dates_with_no_data": "false", "limit": 100}

    def _items(self, body):
        return body["release_dates"]

    def _parse(self, item):
        # NOTE: FRED provides economic releases, not news articles; date-only
        # publication dates use midnight UTC and must not become calendar gates.
        return _headline(item["release_name"], "FRED", item["date"] + "T00:00:00Z",
                         f"https://fred.stlouisfed.org/release?rid={int(item['release_id'])}")


class FinnhubClient(_Client):
    key_name = "FINNHUB_API_KEY"

    def _request(self, query):
        return "https://finnhub.io/api/v1/news", {"token": self.api_key, "category": "forex"}

    def _items(self, body):
        return body

    def _parse(self, item):
        return _headline(item["headline"], item.get("source", "Finnhub"), item["datetime"], item["url"])


class MarketauxClient(_Client):
    key_name = "MARKETAUX_API_KEY"

    def _request(self, query):
        params = {"api_token": self.api_key, "language": "en"}
        if query:
            params["search"] = query
        return "https://api.marketaux.com/v1/news/all", params

    def _items(self, body):
        return body["data"]

    def _parse(self, item):
        return _headline(item["title"], item.get("source", "Marketaux"), item["published_at"], item["url"])


class AlphaVantageClient(_Client):
    key_name = "ALPHAVANTAGE_API_KEY"

    def _request(self, query):
        return "https://www.alphavantage.co/query", {
            "apikey": self.api_key, "function": "NEWS_SENTIMENT", "topics": "economy_monetary", "sort": "LATEST"}

    def _items(self, body):
        return body["feed"]

    def _parse(self, item):
        published = datetime.strptime(item["time_published"], "%Y%m%dT%H%M%S").replace(tzinfo=UTC)
        return _headline(item["title"], item.get("source", "Alpha Vantage"), published, item["url"])


class NewsDataClient(_Client):
    key_name = "NEWSDATA_API_KEY"

    def _request(self, query):
        params = {"apikey": self.api_key, "language": "en", "category": "business"}
        if query:
            params["q"] = query
        return "https://newsdata.io/api/1/latest", params

    def _items(self, body):
        if body.get("status") != "success":
            raise ValueError("NewsData reported failure")
        return body["results"]

    def _parse(self, item):
        published = datetime.fromisoformat(item["pubDate"])
        if published.utcoffset() is None:
            from zoneinfo import ZoneInfo
            published = published.replace(tzinfo=ZoneInfo(item.get("pubDateTZ") or "UTC"))
        return _headline(item["title"], item.get("source_name") or item.get("source_id", "NewsData"), published, item["link"])


def fetch_all_headlines(clients: list) -> list[dict]:
    results, urls, titles = [], set(), set()
    for client in clients:
        try:
            for row in client.fetch_headlines():
                title = " ".join(row["title"].casefold().split())
                url = row.get("url", "").strip().rstrip("/")
                if title in titles or (url and url in urls):
                    continue
                titles.add(title)
                if url:
                    urls.add(url)
                results.append(row)
        except Exception:
            logger.warning("%s failed during headline aggregation", type(client).__name__)
    return results
