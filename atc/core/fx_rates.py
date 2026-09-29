"""ECB FX ingestion with deterministic parsing and idempotent storage."""

from __future__ import annotations

import csv
import io
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any, Callable


ECB_DAILY_URL = "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml"


def _utc_timestamp(value: Any = None) -> str:
    if value is None:
        dt = datetime.now(timezone.utc)
    elif isinstance(value, datetime):
        dt = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    elif isinstance(value, date):
        dt = datetime(value.year, value.month, value.day, tzinfo=timezone.utc)
    elif isinstance(value, (int, float)):
        dt = datetime.fromtimestamp(value, timezone.utc)
    else:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


@dataclass(frozen=True)
class FXRate:
    pair: str
    rate: float
    source: str
    ts: str

    def __getitem__(self, key: str) -> Any:
        return getattr(self, key)


def _parse_decimal(value: Any) -> Decimal:
    parsed = Decimal(str(value))
    if not parsed.is_finite() or parsed <= 0:
        raise ValueError("FX rate must be a positive finite number")
    return parsed


def parse_ecb_rates(payload: str | bytes, *, source: str = ECB_DAILY_URL) -> list[FXRate]:
    """Parse either the ECB daily XML feed or ECB tabular CSV output."""

    text = payload.decode() if isinstance(payload, bytes) else str(payload)
    stripped = text.lstrip()
    rates: list[FXRate] = []
    if stripped.startswith("<"):
        root = ET.fromstring(text)
        default_ts: str | None = None
        for element in root.iter():
            if element.tag.rsplit("}", 1)[-1] == "Cube" and element.get("time"):
                default_ts = _utc_timestamp(element.get("time"))
                continue
            if element.tag.rsplit("}", 1)[-1] != "Cube" or not element.get("currency"):
                continue
            currency = str(element.get("currency")).upper()
            rate = _parse_decimal(element.get("rate"))
            ts = default_ts or _utc_timestamp()
            rates.append(FXRate(f"EUR/{currency}", float(rate), source, ts))
    else:
        reader = csv.DictReader(io.StringIO(text))
        for row in reader:
            currency = str(row.get("CURRENCY", row.get("currency", ""))).upper().strip()
            value = row.get("OBS_VALUE", row.get("obs_value", row.get("RATE", row.get("rate"))))
            observed = row.get("TIME_PERIOD", row.get("time_period", row.get("DATE", row.get("date"))))
            if not currency or value in (None, ""):
                continue
            rate = _parse_decimal(value)
            rates.append(FXRate(f"EUR/{currency}", float(rate), source, _utc_timestamp(observed)))
    if not rates:
        raise ValueError("ECB payload contained no FX rates")
    return rates


class ECBRateIngestor:
    def __init__(
        self,
        repo: Any,
        *,
        fetch: Callable[..., Any] | None = None,
        fetcher: Callable[..., Any] | None = None,
        http_get: Callable[..., Any] | None = None,
        url: str = ECB_DAILY_URL,
        source: str | None = None,
        clock: Callable[[], Any] | None = None,
    ):
        self.repo = repo
        self.fetch = fetch or fetcher or http_get
        self.url = url
        self.source = source or url
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def _fetch(self) -> str | bytes:
        if self.fetch is not None:
            try:
                response = self.fetch(self.url)
            except TypeError:
                response = self.fetch()
            if hasattr(response, "text"):
                return response.text
            if hasattr(response, "content"):
                return response.content
            return response
        with urllib.request.urlopen(self.url, timeout=15) as response:
            return response.read()

    def ingest(self, payload: str | bytes | None = None) -> list[FXRate]:
        rates = parse_ecb_rates(payload if payload is not None else self._fetch(), source=self.source)
        stored: list[FXRate] = []
        for rate in rates:
            day = rate.ts[:10]
            row = {
                "id": f"fx-{rate.pair.replace('/', '-')}-{day}",
                "pair": rate.pair,
                "rate": rate.rate,
                "source": rate.source,
                "ts": rate.ts,
            }
            self.repo.upsert("fx_rates", row, conflict_columns=("id",))
            stored.append(rate)
        return stored

    run = ingest


FxRatesIngestor = ECBRateIngestor
ECBFXIngestor = ECBRateIngestor
FXRates = ECBRateIngestor


def ingest_ecb_rates(repo: Any, payload: str | bytes | None = None, **kwargs: Any) -> list[FXRate]:
    return ECBRateIngestor(repo, **kwargs).ingest(payload)


ingest_ecb = ingest_ecb_rates


__all__ = [
    "ECB_DAILY_URL",
    "FXRate",
    "parse_ecb_rates",
    "ECBRateIngestor",
    "FxRatesIngestor",
    "ECBFXIngestor",
    "FXRates",
    "ingest_ecb_rates",
    "ingest_ecb",
]
