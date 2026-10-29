"""Fit hourly snapshots to zonal data with the dataset's method (n44.fitting).

1. **Check on 2015.** The method runs on the dataset's own Nord Pool data and the result is
   compared with the dataset's snapshots: machine outputs, voltages and taps against the
   after_PF files, and border and cut flows against the annual run (script 08).
2. **2025.** The same method on ENTSO-E data (script 18): actual load and generation of
   each zone and the physical flows on every border and link. Gaps the download could not
   fill are interpolated, and the hours affected are counted. The 2015 topology is kept.

Writes results/fitting_check_2015.csv, the 2025 snapshot tables in
data/processed/snapshots_2025/, the table of fitted hours results/annual/fit_2025.parquet
and results/fit_2025_zones.csv (the inputs per zone and what each area-slack bus absorbed).

Usage: python scripts/20_fit_snapshots.py [--workers N] [--skip-check | --check-only]
"""

import argparse
import time

import numpy as np
import pandas as pd

from n44.config import configured_path
from n44.fitting import fit_year, flow_inputs, nordpool_inputs
from n44.io import entsoe
from n44.io.dataset import Dataset
from n44.snapshots import SnapshotStore, calendar, save_tables
from n44.zones import CUTS, EXTERNAL_LINKS, NEW_LINKS, NORDPOOL_BORDERS, ZONES

YEAR = 2025


def check_2015(base_text: str, workers: int) -> pd.DataFrame:
    processed = configured_path("processed_dir")
    inputs = nordpool_inputs(
        *(
            pd.read_parquet(processed / f"nordpool_{k}.parquet")
            for k in ("consumption", "production", "exchange")
        )
    )
    tables, hours = fit_year(base_text, inputs, 2015, workers)
    store = SnapshotStore()
    annual = configured_path("results_dir") / "annual"
    cuts = pd.read_parquet(annual / "cuts_mw.parquet")
    borders = pd.read_parquet(annual / "borders_mw.parquet")
    rows = []

    def add(quantity, ours, reference, unit):
        diff = (ours - reference).abs().to_numpy().ravel()
        diff = diff[~np.isnan(diff)]
        rows.append(
            {
                "quantity": quantity,
                "unit": unit,
                "mean_abs": diff.mean(),
                "p99_abs": np.quantile(diff, 0.99),
                "max_abs": diff.max(),
            }
        )

    # In a few days the dataset's loads do not add up to Nord Pool's consumption, so its
    # own rule was not followed there. Those hours are counted and left out of the check.
    loads = tables["load_p"].set_axis(store.tables["load_p"].columns, axis=1)
    off = (loads - store.tables["load_p"]).abs().max(axis=1) > 1.0
    ok = hours.index[hours.converged & ~off.reindex(hours.index, fill_value=False)]
    for cut in CUTS:
        add(f"flow {cut}", hours.loc[ok, f"cut {cut}"], cuts.loc[ok, cut], "MW")
    names = [c for c in borders.columns if f"border {c}" in hours]
    add(
        "flow on each border",
        hours.loc[ok, [f"border {c}" for c in names]].set_axis(names, axis=1),
        borders.loc[ok, names],
        "MW",
    )
    for table, label, unit in (
        ("gen_p", "machine P", "MW"),
        ("bus_vm", "bus voltage", "p.u."),
        ("trafo_windv1", "transformer ratio", "p.u."),
    ):
        ref = store.tables[table].loc[ok]
        add(label, tables[table].loc[ok].set_axis(ref.columns, axis=1), ref, unit)
    summary = pd.DataFrame(rows)
    summary.insert(0, "hours", len(ok))
    print(
        f"2015: {hours.converged.sum()} of {len(hours)} hours fitted, at most "
        f"{hours.rounds.max()} rounds; {int(off.sum())} hours left out because the dataset's "
        "loads differ from Nord Pool's consumption"
    )
    print(summary.to_string(index=False, float_format=lambda x: f"{x:.4g}"))
    return summary, int(off.sum())


def inputs_2025() -> tuple:
    processed = configured_path("processed_dir")
    load = pd.read_parquet(processed / f"entsoe_{YEAR}_load.parquet")
    generation = pd.read_parquet(processed / f"entsoe_{YEAR}_generation.parquet")
    production = generation.T.groupby(level="zone").sum(min_count=1).T
    physical = pd.read_parquet(processed / f"entsoe_{YEAR}_physical.parquet")
    borders = list(NORDPOOL_BORDERS) + list(EXTERNAL_LINKS) + list(NEW_LINKS)
    flows = entsoe.net_flow(physical, borders)
    silent = [c for c in flows if flows[c].isna().all()]
    if silent:  # links with no data all year carried nothing (FI-RU stopped in 2022)
        print(f"no data all year, taken as zero: {silent}")
        flows[silent] = 0.0
    # gaps longer than the download filled: interpolate, and count the hours
    frames = {"load": load[list(ZONES)], "production": production[list(ZONES)], "flows": flows}
    estimated = pd.Series(False, index=load.index)
    for name, frame in frames.items():
        missing = frame.isna()
        estimated |= missing.any(axis=1)
        frames[name] = frame.interpolate(limit_area="inside").ffill().bfill()
        if missing.any().any():
            worst = missing.sum().sort_values(ascending=False)
            print(f"{name}: interpolated {dict(worst[worst > 0].head(5))}")
    inputs = flow_inputs(frames["load"], frames["production"], frames["flows"])
    return inputs, frames, estimated


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workers", type=int, default=3)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--skip-check", action="store_true", help="only fit 2025")
    group.add_argument("--check-only", action="store_true", help="only the check on 2015")
    args = parser.parse_args()
    base_text = Dataset().base_case_text()
    results = configured_path("results_dir")
    start = time.perf_counter()
    if not args.skip_check:
        summary, off = check_2015(base_text, args.workers)
        summary["hours_left_out"] = off
        summary.to_csv(results / "fitting_check_2015.csv", index=False, float_format="%.4g")
        print(f"check done in {time.perf_counter() - start:.0f} s\n")
    if args.check_only:
        return

    inputs, frames, estimated = inputs_2025()
    # each hour from the day's starting state: ENTSO-E's zone balances and flows disagree by
    # up to several hundred megawatts, and chained hours would pile up the corrections
    tables, hours = fit_year(base_text, inputs, YEAR, args.workers, chain=False)
    hours["estimated_inputs"] = estimated.reindex(hours.index)
    failed = hours.index[~hours.converged]
    if len(failed):
        print(f"{len(failed)} hours did not converge: {list(failed[:10])}")
    save_tables(tables, configured_path("processed_dir") / f"snapshots_{YEAR}")
    (results / "annual").mkdir(exist_ok=True)
    hours.to_parquet(results / "annual" / f"fit_{YEAR}.parquet")
    cal = calendar(YEAR)
    real = ~cal.synthetic
    # what each area-slack bus absorbed (SE3 holds the swing bus and has no entry)
    slack = hours.reindex(columns=[f"slack {z}" for z in ZONES]).set_axis(list(ZONES), axis=1)
    zones = pd.DataFrame(
        {
            "load_twh": frames["load"][real].sum() / 1e6,
            "production_twh": frames["production"][real].sum() / 1e6,
            "pdes_mean_mw": inputs.pdes[real].mean(),
            "slack_mean_mw": slack[real].mean(),
            "slack_p95_abs_mw": slack[real].abs().quantile(0.95),
        }
    )
    zones.index.name = "zone"
    zones.to_csv(results / f"fit_{YEAR}_zones.csv", float_format="%.4g")
    print(zones.round(1).to_string())
    print(
        f"{YEAR}: {hours.converged.sum()} of {len(hours)} hours fitted in "
        f"{time.perf_counter() - start:.0f} s; {int(estimated.sum())} hours with estimated "
        f"inputs; machines above Pmax in {(hours.units_above_pmax > 0).sum()} hours"
    )


if __name__ == "__main__":
    main()
