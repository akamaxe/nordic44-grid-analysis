"""The 2015 grid with the flows of 2025: AC power flow and AC N-1 of every hour.

Runs on the 2025 snapshots of scripts/20_fit_snapshots.py, which keep the 2015 topology:

1. AC power flow of every hour with pandapower (``n44.annual``), as script 08 did for 2015.
   Its difference from the fitted solution of ``n44.acpf`` is a cross-check of the fit.
2. AC N-1 of every hour with the reference-model solver, the 71 outages and the criteria of
   script 11: an induced overload beyond RATE B, a voltage below 0.90 p.u., or no solution.

The hours the fit could not solve have no AC solution on the 2015 grid even before an outage.
They are counted, with the real SE2->SE3 flow in them, and left out of the rest.

Then compares 2025 with 2015 (scripts 08, 09 and 11) and writes results/annual_2025/*.parquet
(not committed), results/year2025_summary.csv, results/year2025_n1_ranking.csv and the
figures year2025_cuts.png and year2025_balances.png.

Usage: python scripts/21_year_2025.py [--workers N]
"""

import argparse
import time
from concurrent.futures import ProcessPoolExecutor

import numpy as np
import pandas as pd

from n44.acpf import grid_from_case
from n44.annual import run_year
from n44.config import configured_path
from n44.contingency import ac_n1_sweep, branch_table, radial_branches
from n44.io.dataset import Dataset
from n44.io.psse_parser import parse_raw
from n44.plotting import plot_year_balances, plot_year_cuts
from n44.snapshots import SnapshotStore, base_net, calendar, case_from_values
from n44.zones import CUTS, ZONES

YEAR = 2025
VOLTAGE_MIN = 0.90  # p.u., post-contingency, as in script 11


def insecure(base, loading, converged, vm_min, to_b) -> dict[str, np.ndarray]:
    """Per (hour, outage): induced overload, no solution, low voltage only, and any of them."""
    base_b, post_b = base * to_b, loading * to_b
    overload = np.any((post_b > 100) & (base_b <= 100)[:, None, :], axis=2) & converged
    low = converged & ~overload & (np.where(converged, vm_min, np.inf) < VOLTAGE_MIN)
    return {
        "overload": overload,
        "no_solution": ~converged,
        "low_voltage": low,
        "insecure": overload | ~converged | low,
    }


def n1_job(job) -> dict[str, np.ndarray]:
    """AC N-1 of a chunk of 2025 hours."""
    hours, names, outages, to_b = job
    store = SnapshotStore(configured_path("processed_dir") / f"snapshots_{YEAR}")
    base = parse_raw(Dataset().base_case_text())
    parts = {k: [] for k in ("base", "loading", "converged", "vm_min")}
    positions = order = None
    for h in hours:
        grid = grid_from_case(case_from_values(base, store.values(int(h))))
        if positions is None:
            positions = [grid.names.index(o) for o in outages]
            order = [grid.names.index(n) for n in names]
        result = ac_n1_sweep(grid, positions)
        parts["base"].append(result["base_loading"][order])
        parts["loading"].append(result["loading"][:, order])
        parts["converged"].append(result["converged"])
        parts["vm_min"].append(result["vm_min"])
    arrays = {k: np.stack(v) for k, v in parts.items()}
    return insecure(arrays["base"], arrays["loading"], arrays["converged"], arrays["vm_min"], to_b)


def flags_2015(to_b) -> dict[str, np.ndarray]:
    """The same flags from the arrays of the 2015 AC N-1 (script 11)."""
    folder = configured_path("results_dir") / "n1"
    arrays = {
        k: np.load(folder / f"ac_{k}.npy", mmap_mode="r")
        for k in ("base_loading", "loading", "converged", "vm_min")
    }
    out = {}
    for first in range(0, 8760, 730):
        sl = slice(first, first + 730)
        part = insecure(
            np.asarray(arrays["base_loading"][sl], float),
            np.asarray(arrays["loading"][sl], float),
            np.asarray(arrays["converged"][sl]),
            np.asarray(arrays["vm_min"][sl], float),
            to_b,
        )
        for k, v in part.items():
            out.setdefault(k, []).append(v)
    return {k: np.concatenate(v) for k, v in out.items()}


def zone_positions(year: int) -> pd.DataFrame:
    """Hourly production minus consumption per zone (MW), from the data each year used."""
    processed = configured_path("processed_dir")
    if year == 2015:
        names = {("FI" if z == "FI1" else z): z for z in ZONES}
        prod = pd.read_parquet(processed / "nordpool_production.parquet")[list(names)]
        cons = pd.read_parquet(processed / "nordpool_consumption.parquet")[list(names)]
        return (prod - cons).rename(columns=names)
    generation = pd.read_parquet(processed / f"entsoe_{year}_generation.parquet")
    production = generation.T.groupby(level="zone").sum(min_count=1).T[list(ZONES)]
    load = pd.read_parquet(processed / f"entsoe_{year}_load.parquet")[list(ZONES)]
    return production - load


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workers", type=int, default=2)
    args = parser.parse_args()
    results = configured_path("results_dir")
    folder = configured_path("processed_dir") / f"snapshots_{YEAR}"
    out = results / f"annual_{YEAR}"
    out.mkdir(parents=True, exist_ok=True)
    start = time.perf_counter()

    # the hours that the fit could solve; the others have no AC solution on the 2015 grid
    fitted = pd.read_parquet(folder / "load_p.parquet").index.to_numpy()
    tables = run_year(workers=args.workers, hours=fitted, directory=folder)
    for name, frame in tables.items():
        frame.to_parquet(out / f"{name}.parquet")
    s = tables["summary"]
    ok = s[s.converged]
    print(
        f"AC power flow: {len(ok)} of {len(fitted)} fitted hours converged in "
        f"{time.perf_counter() - start:.0f} s; against the fit max |dVm| "
        f"{ok.max_dvm_pu.max():.1e} pu, max |dVa| {ok.max_dva_deg.max():.1e} deg"
    )

    net = base_net()
    table = branch_table(net)
    names = list(table.index)
    outages = [n for n in names if n not in radial_branches(table)]
    to_b = (table.rate_a / table.rate_b).to_numpy()
    solved = ok.index.to_numpy()
    chunks = np.array_split(solved, args.workers * 8)
    with ProcessPoolExecutor(max_workers=args.workers) as pool:
        parts = list(pool.map(n1_job, [(c, names, outages, to_b) for c in chunks]))
    flags = {
        k: pd.DataFrame(np.concatenate([p[k] for p in parts]), index=solved, columns=outages)
        for k in parts[0]
    }
    flags["insecure"].to_parquet(out / "n1_insecure.parquet")
    print(f"AC N-1 done at {time.perf_counter() - start:.0f} s")
    old = {k: pd.DataFrame(v, columns=outages) for k, v in flags_2015(to_b).items()}

    # every hour counts, the interpolated spring hour too, as in scripts 09 and 11
    cal = {y: calendar(y) for y in (2015, YEAR)}
    cuts = {
        2015: pd.read_parquet(results / "annual" / "cuts_mw.parquet"),
        YEAR: tables["cuts_mw"].loc[solved],
    }
    margins = pd.read_parquet(results / "annual" / "transfer_margins.parquet")
    capability = margins.groupby("cut").capability_n1_mw.median()
    sum_rate = pd.read_csv(results / "cut_statistics.csv").set_index("cut").sum_rate_a_mw
    summaries = {2015: pd.read_parquet(results / "annual" / "summary.parquet"), YEAR: ok}
    positions = {y: zone_positions(y) for y in cal}
    unsolved = np.setdiff1d(np.arange(8760), solved)
    physical = pd.read_parquet(configured_path("processed_dir") / f"entsoe_{YEAR}_physical.parquet")
    snitt2_real = physical["SE2->SE3"] - physical["SE3->SE2"]
    rows = []
    for y, f in ((2015, old), (YEAR, flags)):
        insecure = f["insecure"]
        row = {"year": y, "hours with an ac solution": len(insecure)}
        for z in ZONES:
            row[f"net position {z} twh"] = positions[y][z].sum() / 1e6
        for cut in CUTS:
            c = cuts[y][cut]
            row |= {
                f"{cut} mean mw": c.mean(),
                f"{cut} p95 mw": c.quantile(0.95),
                f"{cut} max mw": c.max(),
                f"{cut} hours above sum rate a": int((c > sum_rate[cut]).sum()),
                f"{cut} hours above 2015 median n-1 capability": int((c > capability[cut]).sum()),
            }
        summary = summaries[y]
        row |= {
            "hours with an n-0 overload": int((summary.n_overloaded > 0).sum()),
            "losses mean mw": summary.losses_mw.mean(),
            "n-1 secure hours": int((~insecure.any(axis=1)).sum()),
            "insecure outages per hour": float(insecure.sum(axis=1).mean()),
            "insecure cases": int(insecure.to_numpy().sum()),
            "cases with an induced overload": int(f["overload"].to_numpy().sum()),
            "cases with no solution": int(f["no_solution"].to_numpy().sum()),
            "cases with low voltage only": int(f["low_voltage"].to_numpy().sum()),
        }
        rows.append(row)
    comparison = pd.DataFrame(rows).set_index("year").T
    comparison.index.name = "quantity"
    comparison.loc["real SE2->SE3 flow in hours with no ac solution, mean mw", YEAR] = (
        snitt2_real[unsolved].mean() if len(unsolved) else np.nan
    )
    comparison.loc["real SE2->SE3 flow in all hours, mean mw", YEAR] = snitt2_real.mean()
    comparison.to_csv(results / f"year{YEAR}_summary.csv", float_format="%.4g")
    print(comparison.round(1).to_string())
    months = cal[YEAR].month[unsolved].value_counts().sort_index()
    print(f"{len(unsolved)} hours with no AC solution, by month: {months.to_dict()}")

    ranking = pd.DataFrame(
        {
            "outage": outages,
            "insecure_h_2015": old["insecure"].sum().to_numpy(),
            f"insecure_h_{YEAR}": flags["insecure"].sum().to_numpy(),
        }
    ).sort_values(f"insecure_h_{YEAR}", ascending=False)
    ranking.to_csv(results / f"year{YEAR}_n1_ranking.csv", index=False)
    print(ranking.head(12).to_string(index=False))

    figures = results / "figures"
    plot_year_cuts(cuts, sum_rate, capability, figures / "year2025_cuts.png")
    monthly = {
        y: f["insecure"].sum(axis=1).groupby(cal[y].month[f["insecure"].index].to_numpy()).mean()
        for y, f in ((2015, old), (YEAR, flags))
    }
    plot_year_balances(positions, monthly, figures / "year2025_balances.png")
    print(f"done in {time.perf_counter() - start:.0f} s")


if __name__ == "__main__":
    main()
