"""Readers for the daily Excel files of the dataset.

**Nord Pool files** (``Consumption_XX``, ``Production_XX``, ``Exchange_XX``; XX = FI, NO, SE)
are the original Nord Pool exports for 2015, in MWh/h:

    row 0        title, e.g. "Exchange in MWh"
    row 1        header: date (DD-MM-YYYY) and column names
    rows 2...    one row per hour, labelled "00 - 01" ... "23 - 00"
    then         a blank row, "Min", "Max", "Total" and the header again

The Finnish consumption and production files are weekly tables: the header row holds the
current day and the seven previous ones, and only the first data column belongs to the file's
day. Exchange columns are named "A - B"; a positive value is an import into A from B. Columns
such as "SE net exchange" or "SE - NO" are country aggregates.

On 2015-03-29 the clock skips 02:00-03:00; the row "02 - 03" is still there, with blank
country totals. On 2015-10-25 the hour 02:00-03:00 occurs twice and the day has 25 rows. The
original toolbox took the first 24 rows of every day, so on that day dataset hours 3-23 are
local hours 02-22 and the last hour, 23-00, has no snapshot.

**``PSSE_in_out.xlsx``** is the per-day summary written by the toolbox after running PSS/E.
Per hour it has three columns with the scheduled and solved production, consumption and
interchange of each area, the 13 exchange loads, and convergence and limit-check flags. Rows
are located by their labels, because their positions differ between toolbox versions.
"""

from __future__ import annotations

import datetime
import io
import re
from dataclasses import dataclass

import numpy as np
import pandas as pd

AREAS = ("NO1", "NO2", "NO3", "NO4", "NO5", "SE1", "SE2", "SE3", "SE4", "FI1")
_HOUR_LABEL = re.compile(r"^(\d{2}) - (\d{2})$")
_NET_EXCHANGE = re.compile(r"^(FI|NO|SE) net exchange$", re.IGNORECASE)
_FLAGS = (
    "No convergence",
    "Convergence",
    "Bus voltage problem",
    "Generator active power output problem",
    "Generator reactive power output problem",
    "Generator overloading problem",
    "Branch overloading problem (Rate A)",
    "Branch overloading problem (Rate B)",
    "Branch overloading problem (Rate C)",
)


def _clean(text) -> str:
    return re.sub(r"\s+", " ", str(text).replace("\xa0", " ")).strip()


def _date(cell) -> pd.Timestamp:
    """Header date: text "DD-MM-YYYY" in most files, a real Excel date in a few."""
    if isinstance(cell, pd.Timestamp | datetime.date):
        return pd.Timestamp(cell).normalize()
    return pd.to_datetime(_clean(cell), format="%d-%m-%Y")


def column_key(name: str) -> str:
    """Normalised column name: "SE1 - SE2" -> "SE1_SE2", "SE net exchange" -> "SE"."""
    name = _clean(name)
    if m := _NET_EXCHANGE.match(name):
        return m.group(1).upper()
    return name.replace(" - ", "_")


@dataclass
class DailySheet:
    """One Nord Pool daily file."""

    quantity: str  # consumption, production or exchange
    date: pd.Timestamp
    labels: list[str]  # hour labels, 23 to 25 of them
    values: pd.DataFrame  # one row per hour label, columns normalised with column_key
    total: pd.Series | None  # the "Total" row

    def dataset_hours(self) -> pd.DataFrame:
        """The 24 rows the original toolbox mapped to snapshot hours 0-23 (its first 24)."""
        hours = self.values.iloc[:24].copy()
        hours.index = pd.RangeIndex(len(hours), name="hour")
        return hours

    def problems(self) -> list[str]:
        """Layout irregularities: hour count, blank cells, hours not adding up to Total."""
        found = []
        if len(self.labels) != 24:
            found.append(f"{len(self.labels)} hour rows")
        blank = self.values.columns[self.values.isna().any()].tolist()
        if blank:
            found.append(f"blank cells in {blank}")
        if self.total is not None:
            diff = (self.values.sum() - self.total).abs()
            off = diff[diff > 1.0].index.tolist()
            if off:
                found.append(f"hours do not add up to Total in {off}")
        return found


def read_daily_sheet(content: bytes) -> DailySheet:
    """Parse one Nord Pool daily file (see the module docstring for the layout)."""
    sheet = pd.read_excel(io.BytesIO(content), header=None, engine="openpyxl")
    quantity = _clean(sheet.iat[0, 0]).split()[0].lower()
    labels_all = [_clean(v) for v in sheet.iloc[:, 0]]
    rows = [k for k in range(2, len(sheet)) if _HOUR_LABEL.match(labels_all[k])]
    if not rows or rows != list(range(rows[0], rows[0] + len(rows))):
        raise ValueError("hour rows not found or not contiguous")
    header = sheet.iloc[1]
    weekly = pd.isna(header.iloc[0])
    if weekly:
        date = _date(header.iloc[1])
        names, positions = ["FI"], [1]
    else:
        date = _date(header.iloc[0])
        positions = [k for k in range(1, sheet.shape[1]) if not pd.isna(header.iloc[k])]
        names = [column_key(header.iloc[k]) for k in positions]
    values = sheet.iloc[rows, positions].apply(pd.to_numeric, errors="coerce").astype("float64")
    values.columns = names
    values.index = pd.RangeIndex(len(rows))
    total = None
    if "Total" in labels_all:
        k = labels_all.index("Total")
        total = pd.to_numeric(sheet.iloc[k, positions], errors="coerce").astype("float64")
        total.index = names
    return DailySheet(quantity, date, [labels_all[k] for k in rows], values, total)


def read_psse_in_out(content: bytes) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Parse ``PSSE_in_out.xlsx``.

    Returns two tables:

    - per hour and area: ``scheduled_production``, ``scheduled_consumption``,
      ``scheduled_exchange`` (the area PDES), ``psse_production``, ``psse_consumption`` and
      ``psse_exchange`` (solved interchange), in MW;
    - per hour: one boolean column per flag written by the toolbox (``Convergence``,
      ``Branch overloading problem (Rate A)``...), plus ``branch_overload``.

    The toolbox writes the Rate A, Rate B and Rate C overload messages into the same cell, so a
    later message replaces an earlier one. A branch above Rate B therefore shows only "Rate B".
    ``branch_overload`` is the reliable flag: some branch is at or above 100 % of Rate A.
    """
    sheet = pd.read_excel(io.BytesIO(content), header=None, engine="openpyxl")
    labels = [_clean(v) for v in sheet.iloc[:, 0]]
    scheduled = [labels.index(area) for area in AREAS]
    results_row = labels.index("Results after Power Flow in PSSE")
    solved = [labels.index(area, results_row) for area in AREAS]
    flag_rows = range(solved[-1] + 1, len(sheet))
    area_rows, flag_records = [], []
    for col in range(1, sheet.shape[1], 3):
        head = _clean(sheet.iat[0, col])
        if not head.startswith("hour "):
            continue
        hour = int(head.split()[1])
        for area, r_s, r_p in zip(AREAS, scheduled, solved, strict=True):
            area_rows.append(
                (
                    hour,
                    area,
                    *sheet.iloc[r_s, col : col + 3].to_numpy(dtype=float),
                    *sheet.iloc[r_p, col : col + 3].to_numpy(dtype=float),
                )
            )
        texts = {_clean(sheet.iat[r, col]) for r in flag_rows}
        record = {"hour": hour, **{flag: flag in texts for flag in _FLAGS}}
        record["branch_overload"] = any(t.startswith("Branch overloading") for t in texts)
        flag_records.append(record)
    names = [
        "hour",
        "area",
        "scheduled_production",
        "scheduled_consumption",
        "scheduled_exchange",
        "psse_production",
        "psse_consumption",
        "psse_exchange",
    ]
    areas = pd.DataFrame(area_rows, columns=names)
    areas[names[2:]] = areas[names[2:]].astype(np.float64)
    return areas, pd.DataFrame(flag_records)
