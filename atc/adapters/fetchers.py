"""Deterministic data fetchers (M7): Bybit public candles, FRED, ECB, arXiv,
and generic RSS/Atom.

Invariants enforced here:

* A fetcher runs ONLY for a ``data_sources`` row in status ACTIVE.  Anything
  else raises ``FetcherError`` before any network access (defense in depth:
  the Source Registry gate is the authoritative check upstream).
* Endpoints are https only; no credentials exist anywhere in this module.
* Parsing is deterministic: no wall clock, no randomness, no external process.
* All external text is treated as untrusted data.  XML is parsed with a
  hardened stdlib parser that never expands external entities; nothing fetched
  is ever executed, evaluated, or interpreted as instructions.
* Money stays integer cents where a fetch carries prices (Bybit).

Transports are injectable ``fetch(url) -> bytes|str`` callables so tests use
recorded fixtures and never touch the network.
"""

from __future__ import annotations

import csv
import io
import json
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Mapping, Sequence

from atc.core.fx_rates import parse_ecb_rates

BYBIT_PUBLIC_URL = "https://api.bybit.com"
BYBIT_TESTNET_URL = "https://api-testnet.bybit.com"
FRED_CSV_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv"
ECB_DAILY_URL = "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml"
ARXIV_API_URL = "https://export.arxiv.org/api/query"


#: How far back a market series reaches, on every timeframe.
HISTORY_DAYS = 5 * 365


def _candle_value(candle: Any, key: str) -> Any:
    if isinstance(candle, Mapping):
        return candle.get(key, "")
    return getattr(candle, key, "")


def _parse_ts(value: Any) -> datetime | None:
    """A candle timestamp as an aware datetime: ISO text or epoch (s or ms)."""
    if value is None or value == "":
        return None
    text = str(value)
    try:
        numeric = float(text)
    except ValueError:
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    if numeric > 100_000_000_000:
        numeric /= 1000
    return datetime.fromtimestamp(numeric, timezone.utc)


def _iso_ms(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")

_ARXIV_NS = "{http://www.w3.org/2005/Atom}"
_RSS_NS = {"atom": "http://www.w3.org/2005/Atom"}


class FetcherError(RuntimeError):
    """A fetch refused or failed; nothing was ingested."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _iso_day(value: str) -> str:
    """Normalise a plain date (YYYY-MM-DD) to a UTC ISO-8601 timestamp."""
    text = str(value).strip()
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return f"{text}T00:00:00.000Z" if text else text
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def require_active_source(source: Mapping[str, Any] | None) -> dict[str, Any]:
    """Fail closed unless the source row is usable: registered, ACTIVE,
    tos_ok, https.  Raises FetcherError otherwise."""
    if not isinstance(source, Mapping) or not source.get("id"):
        raise FetcherError("unregistered data source cannot be fetched")
    slug = str(source.get("slug") or source.get("id"))
    if source.get("status") != "ACTIVE":
        raise FetcherError(f"data source {slug!r} is not ACTIVE; fetch refused")
    if not source.get("tos_ok"):
        raise FetcherError(f"data source {slug!r} has not accepted ToS; fetch refused")
    endpoint = str(source.get("endpoint") or "")
    if not endpoint.startswith("https://"):
        raise FetcherError(f"data source {slug!r} endpoint is not https; fetch refused")
    return dict(source)


def default_fetch(url: str, *, timeout: float = 15.0) -> bytes:
    """Stdlib transport; replaced by fixtures in tests."""
    with urllib.request.urlopen(url, timeout=timeout) as response:
        return response.read()


@dataclass(frozen=True)
class FetchPayload:
    """Deterministic, canonical output of one fetch run.

    ``payload_kind`` selects the ingestion path: ``market`` rows become
    datasets/market_data files; ``literature`` rows become literature rows.
    """

    source_id: str
    payload_kind: str  # "market" | "literature"
    rows: tuple[dict[str, Any], ...]
    dataset_identity: dict[str, str] = field(default_factory=dict)
    literature_kind: str = "paper"
    raw: str = ""
    fetched_at: str = field(default_factory=utc_now)


def _dedupe_sorted(rows: Sequence[Mapping[str, Any]], key: str) -> tuple[dict[str, Any], ...]:
    """Deterministic ordering: sort by ``key`` ascending, keep the first row
    per key (content dedup of repeated timestamps)."""
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for row in sorted(rows, key=lambda r: str(r.get(key) or "")):
        identity = str(row.get(key) or "")
        if identity in seen:
            continue
        seen.add(identity)
        out.append(dict(row))
    return tuple(out)


def _require_text(payload: str | bytes | None) -> str:
    if payload is None:
        raise FetcherError("fetch returned no payload")
    try:
        return payload.decode("utf-8") if isinstance(payload, bytes) else str(payload)
    except UnicodeDecodeError as exc:
        raise FetcherError(f"fetch payload is not UTF-8: {exc}") from None


class BybitPublicFetcher:
    """Public Bybit kline (candles) fetch.  Keyless; integer cents output."""

    def __init__(
        self,
        source: Mapping[str, Any],
        *,
        fetch: Callable[..., Any] | None = None,
        connector: Any = None,
        connector_factory: Callable[[], Any] | None = None,
        instrument: str = "BTCUSDT",
        interval: str = "1h",
        limit: int = 1_000,
        history_pages: int = 60,
        history_days: int | None = None,
        held: tuple[str | None, str | None] | None = None,
    ):
        self.source = require_active_source(source)
        self._fetch = fetch
        self.connector = connector
        self.connector_factory = connector_factory
        self.instrument = str(instrument).upper()
        self.interval = str(interval)
        # Bybit caps a kline response at 1000 candles.  Fetching a single page
        # of 200 gave the backtester ~8 days of 1h data, split into four
        # walk-forward folds of 50 bars: not enough history for any verdict to
        # mean anything.  Paging backwards is how the company gets a sample.
        self.limit = max(1, min(1_000, int(limit)))
        # Twelve pages was a row count, not a span: 12 000 candles are five
        # years at 4h and sixteen months at 1h, so every intraday verdict the
        # company reached was drawn from a single regime.  The target is a
        # span, the same on every timeframe; the page count is only a ceiling
        # on one run's requests.
        self.history_pages = max(1, int(history_pages))
        self.history_days = HISTORY_DAYS if history_days is None else max(1, int(history_days))
        # (first ts, last ts) of what the company already holds for this pair.
        # A refresh fetches what is newer than that and, while the record is
        # shorter than history_days, what is older -- not the whole span again
        # every six hours.
        self.held = held

    def _candle_fetch(self) -> list[Any]:
        if self.connector_factory is not None:
            return self.connector_factory().get_candles(
                {"instrument": self.instrument, "interval": self.interval, "limit": self.limit}
            )
        if self.connector is not None:
            return self.connector.get_candles(
                {"instrument": self.instrument, "interval": self.interval, "limit": self.limit}
            )
        if self._fetch is not None:
            # Fixture transport: callable returning a Bybit kline JSON document.
            raw = self._fetch(BYBIT_PUBLIC_URL)
            doc = json.loads(raw) if isinstance(raw, (bytes, str)) else raw
            if not isinstance(doc, dict):
                raise FetcherError("Bybit kline payload is not a JSON object")
            rows = doc.get("result", {}).get("list", []) or []
            candles: list[Any] = []
            for row in rows:
                if len(row) < 6:
                    continue
                candles.append(
                    {
                        "ts": row[0],
                        "open_cents": int(Decimal(str(row[1])) * 100),
                        "high_cents": int(Decimal(str(row[2])) * 100),
                        "low_cents": int(Decimal(str(row[3])) * 100),
                        "close_cents": int(Decimal(str(row[4])) * 100),
                        "volume": Decimal(str(row[5])),
                    }
                )
            return candles
        from atc.adapters.bybit import BybitPublicData

        # Mainnet, read-only.  This used to be BybitConnector(bybit_testnet=True):
        # every candle the company ever backtested came from the testnet book,
        # where ETHUSD closes at 1.00 and 15 of 790 symbols trade at all.
        connector = BybitPublicData()
        return self._paged(connector.get_candles, datetime.now(timezone.utc))

    def _paged(self, get_candles: Callable[[dict[str, Any]], list[Any]],
               now: datetime) -> list[Any]:
        """Page backwards through the venue's candles, newest first.

        Two walks at most: from now back to the newest candle already held,
        then from the oldest candle held back to ``history_days`` ago.  Both
        stop when the venue returns nothing new (the listing date), and both
        share one budget of ``history_pages`` requests.
        """
        target = now - timedelta(days=self.history_days)
        held_start = _parse_ts(self.held[0]) if self.held else None
        held_end = _parse_ts(self.held[1]) if self.held else None
        walks: list[tuple[datetime | None, datetime]] = []
        if held_start is None or held_end is None:
            walks.append((None, target))
        else:
            walks.append((None, held_end))
            if held_start > target:
                walks.append((held_start - timedelta(milliseconds=1), target))
        collected: list[Any] = []
        seen: set[str] = set()
        budget = self.history_pages
        for walk_end, stop_at in walks:
            end_ts = None if walk_end is None else _iso_ms(walk_end)
            while budget > 0:
                budget -= 1
                request: dict[str, Any] = {
                    "instrument": self.instrument, "interval": self.interval,
                    "limit": self.limit,
                }
                if end_ts:
                    request["end_ts"] = end_ts
                page = get_candles(request)
                fresh = [candle for candle in page
                         if str(_candle_value(candle, "ts")) not in seen]
                if not fresh:
                    break
                for candle in fresh:
                    seen.add(str(_candle_value(candle, "ts")))
                collected.extend(fresh)
                oldest = _parse_ts(min(str(_candle_value(candle, "ts")) for candle in fresh))
                if oldest is None or oldest <= stop_at:
                    break
                # Walk one millisecond further back so the next page cannot
                # repeat the oldest candle we already hold.
                end_ts = _iso_ms(oldest - timedelta(milliseconds=1))
        return collected

    def fetch(self) -> FetchPayload:
        candles = self._candle_fetch()
        rows: list[dict[str, Any]] = []
        for candle in candles:
            mapping = candle if isinstance(candle, Mapping) else getattr(candle, "to_dict", lambda: {})()

            def _iso(ts: Any) -> str:
                text = str(ts)
                try:
                    numeric = float(text)
                except ValueError:
                    return _iso_day(text)
                if numeric > 100_000_000_000:
                    numeric /= 1000
                return datetime.fromtimestamp(numeric, timezone.utc).isoformat(
                    timespec="milliseconds"
                ).replace("+00:00", "Z")

            row = {
                "ts": _iso(mapping.get("ts")),
                "open_cents": int(mapping["open_cents"]),
                "high_cents": int(mapping["high_cents"]),
                "low_cents": int(mapping["low_cents"]),
                "close_cents": int(mapping["close_cents"]),
                "volume": float(Decimal(str(mapping.get("volume") or 0))),
            }
            for key in ("open_cents", "high_cents", "low_cents", "close_cents"):
                if not isinstance(row[key], int) or isinstance(row[key], bool):
                    raise FetcherError("Bybit candle prices must be integer cents")
            if row["high_cents"] < row["low_cents"] or row["high_cents"] < min(row["open_cents"], row["close_cents"]) or row["low_cents"] > max(row["open_cents"], row["close_cents"]):
                # Untrusted external data: a candle violating OHLC bounds is
                # kept (evidence) but flagged so the deterministic cleaner can
                # decide; fetching never silently rewrites history.
                row["suspicious"] = True
            rows.append(row)
        rows = _dedupe_sorted(rows, "ts")
        if not rows:
            raise FetcherError("Bybit kline fetch produced no candles")
        return FetchPayload(
            source_id=str(self.source["id"]),
            payload_kind="market",
            rows=rows,
            dataset_identity={"instrument": self.instrument, "timeframe": self.interval},
        )


class FREDFetcher:
    """FRED public fredgraph.csv download (keyless, official endpoint)."""

    def __init__(
        self,
        source: Mapping[str, Any],
        *,
        fetch: Callable[..., Any] | None = None,
        series: str = "DFF",
        url: str = FRED_CSV_URL,
    ):
        self.source = require_active_source(source)
        self._fetch = fetch or default_fetch
        self.series = str(series).strip().upper()
        self.url = url

    def _url(self) -> str:
        return f"{self.url}?{urllib.parse.urlencode({'id': self.series})}"

    def fetch(self) -> FetchPayload:
        try:
            payload = self._fetch(self._url())
        except FetcherError:
            raise
        except Exception as exc:
            raise FetcherError(f"FRED fetch failed: {exc}") from None
        text = _require_text(payload)
        if self.series in text and text.lstrip().lower().startswith(("<!doctype", "<html")):
            raise FetcherError("FRED returned an error page instead of CSV")
        reader = csv.reader(io.StringIO(text))
        header = next(reader, None)
        if not header:
            raise FetcherError("FRED CSV has no header")
        value_column = None
        for index, column in enumerate(header):
            if str(column).strip().upper() == self.series:
                value_column = index
                break
        if value_column is None:
            raise FetcherError(f"FRED CSV lacks column {self.series}")
        rows: list[dict[str, Any]] = []
        for line in reader:
            if len(line) <= value_column:
                continue
            date = str(line[0]).strip()
            value_text = str(line[value_column]).strip()
            if not date:
                continue
            value: float | None
            if value_text in {"", "."}:
                value = None  # FRED marks missing observations with "."; cleaner decides
            else:
                try:
                    value = float(Decimal(value_text))
                except (InvalidOperation, ValueError):
                    raise FetcherError(f"FRED non-numeric value {value_text!r} for {date}") from None
            rows.append({"ts": _iso_day(date), "series": self.series, "value": value})
        rows = _dedupe_sorted(rows, "ts")
        if not rows:
            raise FetcherError("FRED CSV produced no observations")
        return FetchPayload(
            source_id=str(self.source["id"]),
            payload_kind="market",
            rows=rows,
            dataset_identity={"instrument": self.series, "timeframe": "1d"},
            raw=text,
        )


class ECBFetcher:
    """ECB euro foreign exchange reference rates (daily XML)."""

    def __init__(self, source: Mapping[str, Any], *, fetch: Callable[..., Any] | None = None, url: str = ECB_DAILY_URL):
        self.source = require_active_source(source)
        self._fetch = fetch or default_fetch
        self.url = url

    def fetch(self) -> FetchPayload:
        try:
            payload = self._fetch(self.url)
        except FetcherError:
            raise
        except Exception as exc:
            raise FetcherError(f"ECB fetch failed: {exc}") from None
        try:
            rates = parse_ecb_rates(payload if isinstance(payload, (bytes, str)) else json.dumps(payload), source=self.url)
        except ValueError as exc:
            raise FetcherError(f"ECB payload invalid: {exc}") from None
        rows = tuple(
            {"ts": rate.ts, "pair": rate.pair, "rate": float(rate.rate)}
            for rate in sorted(rates, key=lambda r: (r.pair, r.ts))
        )
        if not rows:
            raise FetcherError("ECB payload contained no rates")
        return FetchPayload(
            source_id=str(self.source["id"]),
            payload_kind="market",
            rows=rows,
            dataset_identity={"instrument": "EUR", "timeframe": "1d"},
            raw=_require_text(payload),
        )


def _element_text(element: ET.Element | None) -> str:
    if element is None:
        return ""
    return " ".join("".join(element.itertext()).split())


class ArxivFetcher:
    """arXiv public API (Atom feed).  External text is never executed."""

    def __init__(
        self,
        source: Mapping[str, Any],
        *,
        fetch: Callable[..., Any] | None = None,
        query: str = "all:quantitative finance",
        max_results: int = 10,
        url: str = ARXIV_API_URL,
    ):
        self.source = require_active_source(source)
        self._fetch = fetch or default_fetch
        self.query = str(query)
        self.max_results = max(1, min(200, int(max_results)))
        self.url = url

    def _url(self) -> str:
        params = urllib.parse.urlencode(
            {"search_query": self.query, "start": 0, "max_results": self.max_results}
        )
        return f"{self.url}?{params}"

    def fetch(self) -> FetchPayload:
        try:
            payload = self._fetch(self._url())
        except FetcherError:
            raise
        except Exception as exc:
            raise FetcherError(f"arXiv fetch failed: {exc}") from None
        text = _require_text(payload)
        try:
            root = ET.fromstring(text)
        except ET.ParseError as exc:
            raise FetcherError(f"arXiv payload is not well-formed XML: {exc}") from None
        rows: list[dict[str, Any]] = []
        for entry in root.findall(f"{_ARXIV_NS}entry"):
            entry_id = _element_text(entry.find(f"{_ARXIV_NS}id"))
            title = _element_text(entry.find(f"{_ARXIV_NS}title"))
            summary = _element_text(entry.find(f"{_ARXIV_NS}summary"))
            published = _element_text(entry.find(f"{_ARXIV_NS}published"))
            link = _element_text(entry.find(f"{_ARXIV_NS}link"))
            if not link:
                link = entry_id
            authors = [
                _element_text(author.find(f"{_ARXIV_NS}name"))
                for author in entry.findall(f"{_ARXIV_NS}author")
            ]
            if not entry_id or not title:
                continue
            rows.append(
                {
                    "external_id": entry_id,
                    "url": link,
                    "title": title[:2000],
                    "abstract": summary[:20000],
                    "published_at": _iso_day(published),
                    "authors": [name for name in authors if name],
                }
            )
        if not rows:
            raise FetcherError("arXiv payload contained no entries")
        return FetchPayload(
            source_id=str(self.source["id"]),
            payload_kind="literature",
            rows=tuple(rows),
            literature_kind="paper",
            raw=text,
        )


class RSSFetcher:
    """Generic RSS 2.0 / Atom feed fetch for any ACTIVE, allowlisted rss-kind
    source.  Titles/summaries are untrusted text, stored verbatim (length
    capped) and never interpreted."""

    def __init__(self, source: Mapping[str, Any], *, fetch: Callable[..., Any] | None = None):
        self.source = require_active_source(source)
        if str(source.get("kind")) != "rss":
            raise FetcherError("RSSFetcher requires an rss-kind data source")
        self._fetch = fetch or default_fetch
        self.url = str(source.get("endpoint") or "")

    def fetch(self) -> FetchPayload:
        try:
            payload = self._fetch(self.url)
        except FetcherError:
            raise
        except Exception as exc:
            raise FetcherError(f"RSS fetch failed: {exc}") from None
        text = _require_text(payload)
        try:
            root = ET.fromstring(text)
        except ET.ParseError as exc:
            raise FetcherError(f"RSS payload is not well-formed XML: {exc}") from None
        if root.tag == f"{_ARXIV_NS}feed":
            return self._parse_atom(root, text)
        channel = root.find("channel") if root.tag == "rss" else None
        if channel is not None:
            return self._parse_rss(channel, text)
        if root.tag.endswith("feed"):
            return self._parse_atom(root, text)
        raise FetcherError("RSS payload is neither RSS 2.0 nor Atom")

    def _parse_atom(self, root: ET.Element, text: str) -> FetchPayload:
        rows: list[dict[str, Any]] = []
        for entry in root.findall(f"{_ARXIV_NS}entry"):
            entry_id = _element_text(entry.find(f"{_ARXIV_NS}id"))
            title = _element_text(entry.find(f"{_ARXIV_NS}title"))
            summary = _element_text(entry.find(f"{_ARXIV_NS}summary"))
            link = _element_text(entry.find(f"{_ARXIV_NS}link"))
            published = _element_text(entry.find(f"{_ARXIV_NS}published"))
            if not entry_id and not link:
                continue
            rows.append(
                {
                    "external_id": entry_id or link,
                    "url": link or entry_id,
                    "title": title[:2000],
                    "abstract": summary[:20000],
                    "published_at": _iso_day(published) if published else None,
                    "authors": [],
                }
            )
        if not rows:
            raise FetcherError("Atom payload contained no entries")
        return FetchPayload(
            source_id=str(self.source["id"]), payload_kind="literature", rows=tuple(rows),
            literature_kind="news", raw=text,
        )

    def _parse_rss(self, channel: ET.Element, text: str) -> FetchPayload:
        rows: list[dict[str, Any]] = []
        for item in channel.findall("item"):
            title = _element_text(item.find("title"))
            link = _element_text(item.find("link"))
            description = _element_text(item.find("description"))
            guid = _element_text(item.find("guid"))
            published = _element_text(item.find("pubDate"))
            if not title and not link:
                continue
            rows.append(
                {
                    "external_id": guid or link or title,
                    "url": link,
                    "title": title[:2000],
                    "abstract": description[:20000],
                    "published_at": published or None,
                    "authors": [],
                }
            )
        if not rows:
            raise FetcherError("RSS payload contained no items")
        return FetchPayload(
            source_id=str(self.source["id"]), payload_kind="literature", rows=tuple(rows),
            literature_kind="news", raw=text,
        )


#: What the company looks at by default.  One instrument on one timeframe is
#: not a research universe: every idea gets backtested on the same 1h BTC
#: series, so the fleet can only ever rediscover the same thing.  Spot-only,
#: keyless, public endpoints — nothing here implies a live venue.
BYBIT_DEFAULT_UNIVERSE: tuple[tuple[str, str], ...] = (
    ("BTCUSDT", "1h"),
    ("BTCUSDT", "4h"),
    ("BTCUSDT", "1d"),
    ("ETHUSDT", "1h"),
    ("ETHUSDT", "4h"),
    ("SOLUSDT", "1h"),
)


#: Quote currencies worth cataloguing.  A venue lists hundreds of symbols and
#: most are illiquid or exotic; the company trades spot against these.
CATALOG_QUOTES: tuple[str, ...] = ("USDT", "USDC")

#: Dollar-pegged assets.  A pair whose base is one of these is a stablecoin
#: traded against a stablecoin: it moves by fractions of a basis point, so no
#: strategy can work on it and a backtest there is a wasted engine run.
STABLE_BASES: tuple[str, ...] = (
    "USDC", "USDT", "USDE", "USD1", "DAI", "TUSD", "FDUSD", "BUSD", "RLUSD",
    "PYUSD", "USDD", "USDP", "EURC",
)

#: Catalogue ceiling.  The catalogue is prompt material with a token budget
#: behind it, so it is a menu, not an inventory.
CATALOG_LIMIT = 60


def bybit_instrument_catalog(*, connector: Any = None,
                             limit: int = CATALOG_LIMIT) -> list[str]:
    """The most liquid spot symbols Bybit lists, for the researcher to choose
    from.

    The researcher was shown what the company already covers and asked to add
    something new, which is a guessing game: it knew eight symbols were taken,
    not that the venue lists hundreds.  It answered NO_OP.  Naming a symbol
    from memory is worse than that — a symbol the venue does not list fails the
    fetch every cycle forever — so the catalogue comes from the exchange, and
    an unreachable exchange yields an empty list rather than a plausible one.

    Ranked by 24 h turnover, because a truncated catalogue is a choice about
    what the company may trade.  Sorting the symbols alphabetically and cutting
    at sixty offered 0GUSDT, 2ZUSDC and 5IREUSDT while BTCUSDT fell past the
    cut: real symbols whose spreads bury a 30 bps cost model, and no majors at
    all.  A catalogue truncated by the wrong key is worse than none, because it
    looks right.
    """
    if connector is None:
        from atc.adapters.bybit import BybitPublicData

        connector = BybitPublicData()
    symbols: list[str] = []
    for instrument in connector.list_instruments():
        symbol = str(getattr(instrument, "symbol", "") or "").upper()
        status = str(getattr(instrument, "status", "") or "").upper()
        if not symbol or status not in ("TRADING", "", "UNKNOWN"):
            continue
        if not symbol.endswith(CATALOG_QUOTES):
            continue
        if any(symbol == base + quote
               for base in STABLE_BASES for quote in CATALOG_QUOTES):
            continue
        symbols.append(symbol)

    turnover: Mapping[str, Any] = {}
    lister = getattr(connector, "list_turnover", None)
    if callable(lister):
        try:
            turnover = lister() or {}
        except Exception:
            # Ranking is an improvement on the catalogue, not a precondition
            # for having one; without it the order is merely alphabetical.
            turnover = {}

    def _volume(symbol: str) -> float:
        try:
            return float(turnover.get(symbol, 0.0))
        except (TypeError, ValueError):
            return 0.0

    unique = sorted(set(symbols))
    if turnover:
        # A symbol nobody trades is not a candidate, and padding the list back
        # up to the limit with alphabetical filler reintroduces exactly the
        # 0GUSDT/5IREUSDT problem under a better-looking sort.  A short honest
        # catalogue beats a full one.
        unique = [symbol for symbol in unique if _volume(symbol) > 0.0]
    return sorted(unique, key=lambda symbol: (-_volume(symbol), symbol))[:max(0, int(limit))]


def fetchers_for_source(source: Mapping[str, Any], *,
                        fetch: Callable[..., Any] | None = None,
                        universe: Sequence[tuple[str, str]] | None = None,
                        held: Mapping[tuple[str, str], tuple[str | None, str | None]] | None = None,
                        **kwargs: Any) -> list[Any]:
    """Every fetcher a source yields.

    Most sources map to exactly one dataset.  A market-data exchange maps to
    one per (instrument, timeframe) in the configured universe.
    """
    if str(source.get("slug") or "") == "bybit_public":
        pairs = tuple(universe or BYBIT_DEFAULT_UNIVERSE)
        return [
            BybitPublicFetcher(source, fetch=fetch, instrument=instrument,
                               interval=interval,
                               held=(held or {}).get((instrument.upper(), interval)),
                               **kwargs)
            for instrument, interval in pairs
        ]
    return [fetcher_for_source(source, fetch=fetch, **kwargs)]


def fetcher_for_source(source: Mapping[str, Any], *, fetch: Callable[..., Any] | None = None, **kwargs: Any) -> Any:
    """Deterministic fetcher factory keyed by registered source slug/kind.

    Unknown slugs fail closed: no fetcher exists for an unregistered source,
    so it can never be ingested (M7 VERIFY).
    """
    slug = str(source.get("slug") or "")
    kind = str(source.get("kind") or "")
    if slug == "bybit_public":
        return BybitPublicFetcher(source, fetch=fetch, **kwargs)
    if slug == "fred":
        return FREDFetcher(source, fetch=fetch, **kwargs)
    if slug == "ecb":
        return ECBFetcher(source, fetch=fetch, **kwargs)
    if slug == "arxiv":
        return ArxivFetcher(source, fetch=fetch, **kwargs)
    if kind == "rss":
        return RSSFetcher(source, fetch=fetch)
    raise FetcherError(f"no fetcher registered for data source {slug or kind!r}")


__all__ = [
    "FetchPayload",
    "FetcherError",
    "BybitPublicFetcher",
    "FREDFetcher",
    "ECBFetcher",
    "ArxivFetcher",
    "RSSFetcher",
    "fetcher_for_source",
    "fetchers_for_source",
    "BYBIT_DEFAULT_UNIVERSE",
    "CATALOG_LIMIT",
    "CATALOG_QUOTES",
    "STABLE_BASES",
    "bybit_instrument_catalog",
    "require_active_source",
    "default_fetch",
    "utc_now",
]
