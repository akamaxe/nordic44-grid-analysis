"""Hourly data from the ENTSO-E Transparency Platform (https://transparency.entsoe.eu).

The REST API needs a free security token. Ask for one on the platform (My Account Settings,
then Web API Security Token) and store it in the ``ENTSOE_API_KEY`` environment variable.
The token is sent with each request and never written to the cache, the console or an error
message.

Each response is cached as gzipped XML under data/raw/entsoe/<year>/, so a second run reads
the disk only. A request spans at most one year, which is the API's limit.

**Documents used** (codes of the API guide):

- A44 day-ahead prices, A65 total load (actual), A75 generation per production type
  (actual);
- A11 physical flows, A09 scheduled commercial exchanges (all timeframes), A31 capacity offered to
  the day-ahead market (implicit allocation).

**Time series.** A document holds one or more TimeSeries, each with Periods that start at a
UTC time and have a resolution (PT15M, PT30M or PT60M). Points are numbered from 1. In a
curve of type A03 a point holds until the next one, so missing positions repeat the last
value. Everything is averaged to hourly values on UTC hours, and ``to_hours`` then puts the
values on the project's hour index ``h`` (see ``n44.snapshots.calendar``).
"""

from __future__ import annotations

import gzip
import os
import re
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd
import requests

from n44.config import configured_path
from n44.snapshots import calendar

API = "https://web-api.tp.entsoe.eu/api"
TOKEN_VARIABLE = "ENTSOE_API_KEY"
RETRY_STATUS = (429, 500, 502, 503, 504)

# EIC codes of the bidding zones (FI1 is the model's name for Finland)
AREAS = {
    "NO1": "10YNO-1--------2",
    "NO2": "10YNO-2--------T",
    "NO3": "10YNO-3--------J",
    "NO4": "10YNO-4--------9",
    "NO5": "10Y1001A1001A48H",
    "SE1": "10Y1001A1001A44P",
    "SE2": "10Y1001A1001A45N",
    "SE3": "10Y1001A1001A46L",
    "SE4": "10Y1001A1001A47J",
    "FI1": "10YFI-1--------U",
    "DK1": "10YDK-1--------W",
    "DK2": "10YDK-2--------M",
    "DE": "10Y1001A1001A82H",  # DE-LU, the German zone since October 2018
    "NL": "10YNL----------L",
    "PL": "10YPL-AREA-----S",
    "LT": "10YLT-1001A0008Q",
    "EE": "10Y1001A1001A39I",
    "GB": "10YGB----------A",
    "RU": "10Y1001A1001A49F",
}

# Production types (psrType) of A75
PRODUCTION_TYPES = {
    "B01": "biomass",
    "B02": "lignite",
    "B03": "coal gas",
    "B04": "gas",
    "B05": "hard coal",
    "B06": "oil",
    "B07": "oil shale",
    "B08": "peat",
    "B09": "geothermal",
    "B10": "hydro pumped storage",
    "B11": "hydro run-of-river",
    "B12": "hydro reservoir",
    "B13": "marine",
    "B14": "nuclear",
    "B15": "other renewable",
    "B16": "solar",
    "B17": "waste",
    "B18": "wind offshore",
    "B19": "wind onshore",
    "B20": "other",
    "B25": "energy storage",
}


class EntsoeError(RuntimeError):
    """A request the API refused, or a missing token."""


def token() -> str:
    value = os.environ.get(TOKEN_VARIABLE, "").strip()
    if not value:
        raise EntsoeError(
            f"{TOKEN_VARIABLE} is not set. Request a token on transparency.entsoe.eu "
            "(My Account Settings, Web API Security Token) and store it in that environment "
            "variable."
        )
    return value


def _redact(text: str, secret: str) -> str:
    return text.replace(secret, "<token>") if secret else text


def year_period(year: int) -> dict[str, str]:
    """periodStart and periodEnd (UTC) that cover the hours of ``year``."""
    utc = calendar(year).utc_time.dropna()
    start, end = utc.min(), utc.max() + pd.Timedelta(hours=1)
    return {"periodStart": start.strftime("%Y%m%d%H%M"), "periodEnd": end.strftime("%Y%m%d%H%M")}


def cache_dir(year: int) -> Path:
    return configured_path("entsoe_dir") / str(year)


def fetch(params: dict[str, str], cache: Path | None = None, retries: int = 5) -> str:
    """XML answer of one API request, from ``cache`` when it is there."""
    if cache is not None and cache.exists():
        return gzip.decompress(cache.read_bytes()).decode("utf-8")
    secret = token()
    for attempt in range(retries):
        try:
            resp = requests.get(API, params={**params, "securityToken": secret}, timeout=300)
        except requests.RequestException as exc:
            # the exception text holds the URL, and so the token
            message = _redact(str(exc), secret)
            if attempt == retries - 1:
                raise EntsoeError(f"request failed: {message}") from None
        else:
            if resp.status_code not in RETRY_STATUS:
                break
            message = f"HTTP {resp.status_code}"
            if attempt == retries - 1:
                raise EntsoeError(f"request failed after {retries} attempts: {message}")
        time.sleep(15 * (attempt + 1))
    text = _redact(resp.text, secret)
    reason = _reason(text)
    if resp.status_code != 200 and not _no_data(reason):
        raise EntsoeError(f"HTTP {resp.status_code}: {reason or 'no reason given'}")
    if cache is not None:
        cache.parent.mkdir(parents=True, exist_ok=True)
        partial = cache.with_name(cache.name + ".part")
        partial.write_bytes(gzip.compress(text.encode("utf-8")))
        partial.replace(cache)
    return text


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _reason(text: str) -> str:
    found = re.search(r"<text>([^<]*)</text>", text)
    return found.group(1).strip() if found else ""


def _no_data(reason: str) -> bool:
    return reason.startswith("No matching data found")


@dataclass
class Series:
    """One TimeSeries of a document: its attributes and its values at native resolution."""

    attributes: dict[str, str] = field(default_factory=dict)
    values: pd.Series = field(default_factory=lambda: pd.Series(dtype=float))


_RESOLUTION = {"PT15M": "15min", "PT30M": "30min", "PT60M": "60min", "P1D": "1D"}


def _child(node, name: str):
    return next((c for c in node if _local(c.tag) == name), None)


def parse(text: str) -> list[Series]:
    """The time series of a document. A "no data" acknowledgement gives an empty list."""
    root = ET.fromstring(text)
    if _local(root.tag) == "Acknowledgement_MarketDocument":
        reason = _reason(text)
        if _no_data(reason):
            return []
        raise EntsoeError(reason or "acknowledgement without a reason")
    out = []
    for ts in (n for n in root if _local(n.tag) == "TimeSeries"):
        attributes, periods = {}, []
        for node in ts:
            name = _local(node.tag)
            if name == "Period":
                periods.append(node)
            elif name == "MktPSRType":
                attributes["psrType"] = _child(node, "psrType").text
            elif len(node) == 0 and node.text:
                attributes[name.removesuffix(".mRID")] = node.text.strip()
        curve = attributes.get("curveType", "A01")
        parts = [_period_values(p, curve) for p in periods]
        values = pd.concat(parts).sort_index() if parts else pd.Series(dtype=float)
        out.append(Series(attributes, values[~values.index.duplicated()]))
    return out


def _period_values(period, curve: str) -> pd.Series:
    interval = _child(period, "timeInterval")
    start = pd.Timestamp(_child(interval, "start").text)
    end = pd.Timestamp(_child(interval, "end").text)
    step = pd.Timedelta(_RESOLUTION[_child(period, "resolution").text])
    count = int((end - start) / step)
    positions, values = [], []
    for point in (n for n in period if _local(n.tag) == "Point"):
        position = int(_child(point, "position").text)
        value = _child(point, "quantity")
        if value is None:
            value = _child(point, "price.amount")
        positions.append(position)
        values.append(float(value.text))
    series = pd.Series(values, index=positions, dtype=float).reindex(range(1, count + 1))
    if curve == "A03":
        series = series.ffill()
    series.index = start + step * (series.index - 1)
    return series


def hourly(series: list[Series], key: str | None = None) -> pd.DataFrame | pd.Series:
    """Hourly means on UTC hours; one column per value of the attribute ``key``.

    Series that share a key value (one per period, say) are joined; where they overlap
    (the same hour at two resolutions) their mean is taken.
    """
    groups: dict[str, list[pd.Series]] = {}
    for s in series:
        if s.values.empty:
            continue
        values = s.values.resample("1h").mean()
        groups.setdefault(s.attributes.get(key, "value") if key else "value", []).append(values)
    columns = {
        name: pd.concat(parts).groupby(level=0).mean() for name, parts in sorted(groups.items())
    }
    frame = pd.DataFrame(columns)
    if frame.empty:
        frame = pd.DataFrame(index=pd.DatetimeIndex([], tz="UTC"))
    return frame if key else frame.get("value", pd.Series(dtype=float, index=frame.index))


def to_hours(values: pd.DataFrame | pd.Series, year: int) -> pd.DataFrame | pd.Series:
    """Values on the hour index ``h`` of ``year``; the synthetic hour is interpolated."""
    cal = calendar(year)
    out = values.reindex(pd.DatetimeIndex(cal.utc_time))
    out.index = cal.index
    synthetic = cal.synthetic.to_numpy()
    if synthetic.any():
        out[synthetic] = np.nan
        out[synthetic] = out.interpolate(limit_area="inside")[synthetic]
    return out


def fill_gaps(values: pd.DataFrame, limit: int = 3) -> tuple[pd.DataFrame, pd.Series]:
    """Interpolate gaps of up to ``limit`` hours; also returns the hours left missing."""
    filled = values.copy()
    for column in values:
        s = values[column]
        run = s.isna().groupby(s.notna().cumsum()).transform("sum")
        short = s.isna() & (run <= limit)
        filled.loc[short, column] = s.interpolate(limit_area="inside")[short]
    return filled, filled.isna().sum()


# --- data sets -------------------------------------------------------------------------


def _get(name: str, year: int, params: dict[str, str]) -> list[Series]:
    return parse(fetch({**params, **year_period(year)}, cache_dir(year) / f"{name}.xml.gz"))


def prices(year: int, zones) -> pd.DataFrame:
    """Day-ahead prices (EUR/MWh) per zone."""
    out = {}
    for zone in zones:
        params = {"documentType": "A44", "in_Domain": AREAS[zone], "out_Domain": AREAS[zone]}
        out[zone] = hourly(_get(f"A44_{zone}", year, params))
    return to_hours(pd.DataFrame(out), year)


def load(year: int, zones) -> pd.DataFrame:
    """Actual total load (MW) per zone."""
    out = {}
    for zone in zones:
        params = {"documentType": "A65", "processType": "A16", "outBiddingZone_Domain": AREAS[zone]}
        out[zone] = hourly(_get(f"A65_{zone}", year, params))
    return to_hours(pd.DataFrame(out), year)


def generation(year: int, zones, psr_type: str | None = None) -> pd.DataFrame:
    """Actual generation (MW) per zone and production type, columns (zone, type).

    Only output into the zone counts; the consumption of pumped storage, which the API
    reports with the zone as the "out" area, is left out.
    """
    frames = {}
    for zone in zones:
        params = {"documentType": "A75", "processType": "A16", "in_Domain": AREAS[zone]}
        name = f"A75_{zone}"
        if psr_type:
            params["psrType"] = psr_type
            name += f"_{psr_type}"
        series = [s for s in _get(name, year, params) if "inBiddingZone_Domain" in s.attributes]
        frames[zone] = hourly(series, key="psrType")
    frame = pd.concat(frames, axis=1, names=["zone", "type"])
    return to_hours(frame, year)


_FLOW_PARAMS = {
    "A11": {"documentType": "A11"},
    "A09": {"documentType": "A09", "contract_MarketAgreement.Type": "A05"},
    "A31": {"documentType": "A31", "auction.Type": "A01", "contract_MarketAgreement.Type": "A01"},
}


def directed(document: str, year: int, borders) -> pd.DataFrame:
    """Values of ``document`` on each border in both directions, columns "A->B" and "B->A".

    A11 gives physical flows, A09 the scheduled commercial exchanges of all timeframes and
    A31 the capacity offered to the day-ahead market (MW). For A11 and A09 a direction with
    no data is all zero when the other direction has data (the flow ran the other way); a
    border with no data at all stays missing.
    """
    out = {}
    for a, b in borders:
        for src, dst in ((a, b), (b, a)):
            params = {**_FLOW_PARAMS[document], "out_Domain": AREAS[src], "in_Domain": AREAS[dst]}
            values = hourly(_get(f"{document}_{src}_{dst}", year, params))
            out[f"{src}->{dst}"] = values
    frame = to_hours(pd.DataFrame(out), year)
    if document in ("A11", "A09"):
        for a, b in borders:
            pair = [f"{a}->{b}", f"{b}->{a}"]
            empty = [c for c in pair if frame[c].isna().all()]
            if len(empty) == 1:
                frame[empty] = 0.0
    return frame


def net_flow(directed_flows: pd.DataFrame, borders) -> pd.DataFrame:
    """Net flow A -> B on each border from the two directions."""
    return pd.DataFrame(
        {f"{a}->{b}": directed_flows[f"{a}->{b}"] - directed_flows[f"{b}->{a}"] for a, b in borders}
    )
