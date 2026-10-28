"""Build the hourly tables used by every later step.

Writes, under data/processed/:

- ``snapshots/*.parquet``: the values of each after_PF case (see n44.snapshots), one row per
  snapshot h;
- ``nordpool_{consumption,production,exchange}.parquet``: the Nord Pool data, aligned with the
  snapshots the way the original toolbox aligned it (the first 24 rows of each day);
- ``psse_summary_{areas,flags}.parquet``: PSSE_in_out.xlsx of every day;
- ``calendar.parquet``: date, hour, local and UTC time of each h.
"""

import sys
from collections import defaultdict

import pandas as pd

from n44.config import configured_path
from n44.io.dataset import Dataset, read_member
from n44.io.nordpool import read_daily_sheet, read_psse_in_out
from n44.snapshots import build_tables, calendar, save_tables, snapshot_dir

CLOCK_CHANGE_DAYS = {pd.Timestamp("2015-03-29").date(), pd.Timestamp("2015-10-25").date()}


def nordpool_tables(ds: Dataset) -> tuple[dict[str, pd.DataFrame], list[str]]:
    parts, problems = defaultdict(list), []
    files = ds.excel_files()
    for _, row in files[files["country"].notna()].iterrows():
        sheet = read_daily_sheet(read_member(row["archive"], row["member"]))
        hours = sheet.dataset_hours()
        hours.index = hours.index + (pd.Timestamp(row["date"]).dayofyear - 1) * 24
        parts[(row["kind"], row["country"])].append(hours)
        found = sheet.problems()
        if found and row["date"] not in CLOCK_CHANGE_DAYS:
            problems.append(f"{row['date']} {row['kind']} {row['country']}: {found}")
    tables = {}
    for kind in ("consumption", "production", "exchange"):
        frames = [pd.concat(parts[(kind, c)]).sort_index() for c in ("NO", "SE", "FI")]
        table = pd.concat(frames, axis=1)
        table.index.name = "h"
        tables[kind] = table
    return tables, problems


def psse_summaries(ds: Dataset) -> tuple[pd.DataFrame, pd.DataFrame]:
    areas, flags = [], []
    files = ds.excel_files()
    for _, row in files[files["kind"] == "PSSE_in_out"].iterrows():
        a, f = read_psse_in_out(read_member(row["archive"], row["member"]))
        offset = (pd.Timestamp(row["date"]).dayofyear - 1) * 24
        a["h"], f["h"] = a.pop("hour") + offset, f.pop("hour") + offset
        areas.append(a)
        flags.append(f)
    return (
        pd.concat(areas, ignore_index=True).sort_values(["h", "area"]),
        pd.concat(flags).set_index("h").sort_index(),
    )


def main() -> None:
    ds = Dataset()
    out = configured_path("processed_dir")
    if "--force" in sys.argv or not (snapshot_dir() / "area_pdes.parquet").exists():
        tables = build_tables((h, ds.raw_text(h, "after")) for h in range(8760))
        save_tables(tables)
        print(f"snapshot tables: {len(tables)} x {len(tables['bus_vm'])} hours")
    else:
        print("snapshot tables already built (use --force to rebuild)")

    nordpool, problems = nordpool_tables(ds)
    for kind, table in nordpool.items():
        table.to_parquet(out / f"nordpool_{kind}.parquet")
        print(f"nordpool_{kind}: {table.shape}, blank cells: {int(table.isna().sum().sum())}")
    print(f"layout problems outside the clock-change days: {len(problems)}")
    for line in problems[:10]:
        print("  ", line)

    areas, flags = psse_summaries(ds)
    areas.to_parquet(out / "psse_summary_areas.parquet", index=False)
    flags.to_parquet(out / "psse_summary_flags.parquet")
    cal = calendar()
    cal.to_parquet(out / "calendar.parquet")
    print(
        f"PSSE_in_out: {len(flags)} hours, converged {int(flags['Convergence'].sum())}; "
        f"calendar: {len(cal)} hours, synthetic {int(cal.synthetic.sum())}"
    )


if __name__ == "__main__":
    main()
