"""Download the ENTSO-E data of 2015 and 2025 and put them on the hour index h.

Needs a token in the ENTSOE_API_KEY environment variable (see n44.io.entsoe). The answers are
cached under data/raw/entsoe/, so a second run downloads nothing.

2015, to compare with the market data of the dataset:

- day-ahead prices of the ten zones;
- on the 15 borders between modelled zones, in both directions, the capacity offered to the
  day-ahead market, the physical flows and the day-ahead scheduled exchanges;
- load and generation per production type of the ten zones.

2025, for the snapshots of a recent year: load and generation per production type of the ten
zones, and physical flows on the borders between modelled zones and on the links to the
neighbours.

Gaps of up to three hours are interpolated. Writes data/processed/entsoe_{2015,2025}_*.parquet
and prints how many hours each series still misses.

Usage: python scripts/18_download_entsoe.py
"""

import argparse

import pandas as pd

from n44.config import configured_path
from n44.io import entsoe
from n44.zones import EXTERNAL_LINKS, NEW_LINKS, NORDPOOL_BORDERS, ZONES

INTERNAL = list(NORDPOOL_BORDERS)


def save(frame: pd.DataFrame, name: str, report: list[str]) -> None:
    filled, missing = entsoe.fill_gaps(frame)
    filled.index.name = "h"
    filled.columns = (
        filled.columns if isinstance(filled.columns, pd.MultiIndex) else filled.columns.astype(str)
    )
    filled.to_parquet(configured_path("processed_dir") / f"entsoe_{name}.parquet")
    worst = missing.sort_values(ascending=False)
    gaps = ", ".join(f"{k} {v}" for k, v in worst[worst > 0].head(6).items()) or "none"
    report.append(f"{name:18s} {frame.shape[1]:3d} series, hours still missing: {gaps}")
    print(report[-1], flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()
    entsoe.token()  # fail early, with instructions, when the token is missing
    report: list[str] = []

    save(entsoe.prices(2015, ZONES), "2015_prices", report)
    for document, name in (("A31", "capacity"), ("A11", "physical"), ("A09", "scheduled")):
        save(entsoe.directed(document, 2015, INTERNAL), f"2015_{name}", report)
    for year in (2015, 2025):
        save(entsoe.load(year, ZONES), f"{year}_load", report)
        save(entsoe.generation(year, ZONES), f"{year}_generation", report)
    save(
        entsoe.directed("A11", 2025, INTERNAL + list(EXTERNAL_LINKS) + list(NEW_LINKS)),
        "2025_physical",
        report,
    )
    print(f"\n{len(report)} tables written to {configured_path('processed_dir')}")


if __name__ == "__main__":
    main()
