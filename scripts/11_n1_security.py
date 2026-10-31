"""N-1 security of every hour of 2015.

1. Contingencies: the 79 lines and transformers minus the 8 radial HVDC connections.
2. DC model: PTDF and LODF of the topology, which is the same in every snapshot.
3. DC screening of all 8,760 hours: each hour's AC flows plus the LODF changes.
4. AC N-1 of all 8,760 hours with the reference-model solver (``n44.acpf``).
5. Cross-check of that solver against pandapower's AC N-1 on the 100 hours of highest
   snitt-2 transfer.

Criteria, after an outage:
- an induced overload: a branch above RATE B that was within RATE B before;
- a low voltage: below 0.90 p.u.;
- no AC solution at all.

Writes the arrays to results/n1/ (large, not committed), the summaries
results/n1_{ranking,screening,crosscheck,hvdc_links}.csv and the figures
results/figures/n1_*.png.

Usage: python scripts/11_n1_security.py [--force] [--crosscheck-hours N]
"""

import argparse
import time

import numpy as np
import pandas as pd

from n44.acpf import grid_from_case, solve
from n44.annual import pre_contingency_flows
from n44.config import configured_path
from n44.contingency import (
    ac_n1_sweep,
    branch_table,
    dc_model,
    radial_branches,
    run_n1,
    screen_loading,
    shunt_outage_shift,
)
from n44.io.dataset import Dataset
from n44.io.psse_parser import parse_raw
from n44.plotting import plot_n1_map, plot_n1_ranking, plot_n1_screening
from n44.powerflow import run_ac
from n44.snapshots import SnapshotStore, base_net, load_snapshot
from n44.zones import EXCHANGE_LOADS

VOLTAGE_MIN = 0.90  # p.u., post-contingency
HOURS = 8760
THRESHOLDS = (100, 95, 90, 85, 80, 75, 70)  # % of RATE B, screening flags


def dc_screening(net, table, names, outages, folder) -> np.ndarray:
    dc = dc_model(net)
    out_pos = np.array([names.index(o) for o in outages])
    shift = shunt_outage_shift(net, dc, outages)
    pre = pre_contingency_flows(names, table)
    result = np.lib.format.open_memmap(
        folder / "dc_loading.npy", "w+", np.float32, (HOURS, len(outages), len(names))
    )
    start = time.perf_counter()
    for first in range(0, HOURS, 730):
        rows = slice(first, first + 730)
        chunk = {key: value[rows] for key, value in pre.items()}
        result[rows] = screen_loading(
            chunk,
            dc.lodf.to_numpy(),
            out_pos,
            table.rate_a.to_numpy(),
            (table.basis == "current").to_numpy(),
            shift,
        )
    result.flush()
    print(
        f"DC screening of {HOURS} h x {len(outages)} outages: {time.perf_counter() - start:.1f} s"
    )
    return result


def ac_sweep(names, outages, folder, resume: bool = True) -> dict[str, np.ndarray]:
    """AC N-1 of every hour into memory-mapped arrays on disk.

    With ``resume``, existing arrays are kept and the sweep continues from the first hour
    without results, so an interrupted run does not start over.
    """
    shapes = {
        "ac_loading": ((HOURS, len(outages), len(names)), np.float32),
        "ac_base_loading": ((HOURS, len(names)), np.float32),
        "ac_vm_min": ((HOURS, len(outages)), np.float32),
        "ac_vm_min_bus": ((HOURS, len(outages)), np.int32),
        "ac_converged": ((HOURS, len(outages)), np.bool_),
    }
    present = all((folder / f"{key}.npy").exists() for key in shapes)
    if resume and present:
        arrays = {key: np.load(folder / f"{key}.npy", mmap_mode="r+") for key in shapes}
        done = np.asarray(arrays["ac_converged"]).any(axis=1)
        first = HOURS if done.all() else int(np.argmin(done))
        print(f"  AC N-1: resuming at h={first}", flush=True)
    else:
        arrays = {
            key: np.lib.format.open_memmap(folder / f"{key}.npy", "w+", dtype, shape)
            for key, (shape, dtype) in shapes.items()
        }
        first = 0
    ds = Dataset()
    start = time.perf_counter()
    positions = None
    for h in range(first, HOURS):
        grid = grid_from_case(parse_raw(ds.raw_text(h, "after")))
        if positions is None:
            positions = [grid.names.index(o) for o in outages]
            order = [grid.names.index(n) for n in names]
        result = ac_n1_sweep(grid, positions)
        arrays["ac_loading"][h] = result["loading"][:, order]
        arrays["ac_base_loading"][h] = result["base_loading"][order]
        arrays["ac_vm_min"][h] = result["vm_min"]
        arrays["ac_vm_min_bus"][h] = result["vm_min_bus"]
        arrays["ac_converged"][h] = result["converged"]
        if h % 1000 == 999:
            print(f"  AC N-1: {h + 1} h, {time.perf_counter() - start:.0f} s", flush=True)
    for array in arrays.values():
        array.flush()
    print(f"AC N-1 of {HOURS} h x {len(outages)} outages: {time.perf_counter() - start:.0f} s")
    return arrays


def load_arrays(folder) -> dict[str, np.ndarray]:
    keys = (
        "dc_loading",
        "ac_loading",
        "ac_base_loading",
        "ac_vm_min",
        "ac_vm_min_bus",
        "ac_converged",
    )
    return {key: np.load(folder / f"{key}.npy", mmap_mode="r") for key in keys}


def crosscheck(outages, names, arrays, hours: list[int]) -> pd.DataFrame:
    """pandapower's AC N-1 against the reference-model solver, hour by hour."""
    net, store, ds = base_net(), SnapshotStore(), Dataset()
    rows = []
    for h in hours:
        load_snapshot(h, store, net)
        if not run_ac(net).converged:
            raise RuntimeError(f"pandapower base case of h={h} does not converge")
        flows, summary = run_n1(net, outages)
        pp_loading = flows.pivot(index="outage", columns="branch", values="loading_a_pct")
        pp_loading = pp_loading.reindex(index=outages, columns=names).to_numpy()
        fast = np.asarray(arrays["ac_loading"][h], dtype=float)
        pp_conv = summary.set_index("outage").converged.reindex(outages).to_numpy()
        fast_conv = np.asarray(arrays["ac_converged"][h])
        grid = grid_from_case(parse_raw(ds.raw_text(h, "after")))
        base = solve(grid)
        inconsistent = 0
        for k, name in enumerate(outages):
            if fast_conv[k]:
                sol = solve(grid, outage=grid.names.index(name), v0=base.v, max_iter=20)
                inconsistent += bool(sol.converged and sol.inconsistent)
        both = pp_conv & fast_conv
        diff = np.abs(fast[both] - pp_loading[both])
        rows.append(
            {
                "h": h,
                "max_dloading_pct": float(np.nanmax(diff)) if both.any() else np.nan,
                "converged_pandapower": int(pp_conv.sum()),
                "converged_fast": int(fast_conv.sum()),
                "convergence_agrees": bool((pp_conv == fast_conv).all()),
                "fast_inconsistent": inconsistent,
            }
        )
        print(f"  cross-check h={h}: {rows[-1]}", flush=True)
    return pd.DataFrame(rows)


def screening_accuracy(arrays, table) -> tuple[pd.DataFrame, dict]:
    """How well the DC screening anticipates the AC N-1, contingency by contingency.

    An (hour, outage) pair is insecure in AC for one of three reasons, counted in this order:
    no solution, an induced overload, or only a voltage below VOLTAGE_MIN. The screening
    flags a pair if some branch within RATE B before the outage is predicted at or above the
    threshold; the pairs it misses are counted per reason. DC sees active power only, so the
    voltage cases are out of its reach by construction.
    """
    to_b = (table.rate_a / table.rate_b).to_numpy()
    reasons = ("no_solution", "overload", "low_voltage")
    rows = {
        t: {"threshold_pct": t, "flagged": 0, **{f"missed_{r}": 0 for r in reasons}}
        for t in THRESHOLDS
    }
    totals = dict.fromkeys(reasons, 0)
    errors = []
    for first in range(0, HOURS, 730):
        sl = slice(first, first + 730)
        base = np.asarray(arrays["ac_base_loading"][sl], float) * to_b
        within = (base <= 100)[:, None, :]
        ac = np.asarray(arrays["ac_loading"][sl], float) * to_b
        dc = np.asarray(arrays["dc_loading"][sl], float) * to_b
        conv = np.asarray(arrays["ac_converged"][sl])
        vmin = np.where(conv, np.asarray(arrays["ac_vm_min"][sl], float), np.inf)
        overload = np.any((ac > 100) & within, axis=2)
        kind = {
            "no_solution": ~conv,
            "overload": overload,
            "low_voltage": conv & ~overload & (vmin < VOLTAGE_MIN),
        }
        for reason, mask in kind.items():
            totals[reason] += int(mask.sum())
        # worst predicted loading on branches within RATE B before the outage, (h, outage);
        # the outaged branch itself is NaN and must not hide the others
        predicted = np.where(within & np.isfinite(dc), dc, -np.inf).max(axis=2)
        for t in THRESHOLDS:
            flag = predicted >= t
            rows[t]["flagged"] += int(flag.sum())
            for reason, mask in kind.items():
                rows[t][f"missed_{reason}"] += int((mask & ~flag).sum())
        ok = conv[:, :, None] & np.isfinite(ac) & np.isfinite(dc)
        errors.append((dc - ac)[ok].astype(np.float32))
    pairs = HOURS * arrays["ac_converged"].shape[1]
    table_out = pd.DataFrame(rows.values())
    for reason in reasons:
        table_out[f"insecure_{reason}"] = totals[reason]
    table_out["ac_runs_saved_pct"] = 100 * (1 - table_out.flagged / pairs)
    err = np.concatenate(errors)
    stats = {
        "pairs": pairs,
        **{f"insecure_{r}": totals[r] for r in reasons},
        "error_mean_pct": float(err.mean()),
        "error_mae_pct": float(np.abs(err).mean()),
        "error_p95_abs_pct": float(np.percentile(np.abs(err), 95)),
        "error_max_abs_pct": float(np.abs(err).max()),
    }
    return table_out, stats


def ranking(arrays, table, names, outages) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Per contingency: hours with an induced overload, low voltage or no solution.

    ``insecure_h`` counts hours with any of the three. Also returns, for every contingency
    and branch, the number of hours the outage overloads that branch.
    """
    to_b = (table.rate_a / table.rate_b).to_numpy()
    n_out = len(outages)
    counts = np.zeros((n_out, len(names)), dtype=np.int32)
    keys = ("overload_h", "no_solution_h", "low_voltage_h", "insecure_h")
    stats = {key: np.zeros(n_out, dtype=int) for key in keys}
    worst = np.full(n_out, -np.inf)
    worst_branch = np.full(n_out, -1)
    worst_h = np.full(n_out, -1)
    vmin_worst = np.full(n_out, np.inf)
    vmin_bus = np.full(n_out, -1)
    for first in range(0, HOURS, 730):
        sl = slice(first, first + 730)
        base = np.asarray(arrays["ac_base_loading"][sl], float) * to_b
        ac = np.asarray(arrays["ac_loading"][sl], float) * to_b
        conv = np.asarray(arrays["ac_converged"][sl])
        vmin = np.where(conv, np.asarray(arrays["ac_vm_min"][sl], float), np.inf)
        vbus = np.asarray(arrays["ac_vm_min_bus"][sl])
        induced = (ac > 100) & (base <= 100)[:, None, :]
        overload = induced.any(axis=2)
        counts += induced.sum(axis=0)
        stats["overload_h"] += overload.sum(axis=0)
        stats["no_solution_h"] += (~conv).sum(axis=0)
        stats["low_voltage_h"] += (vmin < VOLTAGE_MIN).sum(axis=0)
        stats["insecure_h"] += (overload | ~conv | (vmin < VOLTAGE_MIN)).sum(axis=0)
        masked = np.where(induced, ac, -np.inf)  # (hours, outages, branches)
        for k in np.flatnonzero(masked.max(axis=(0, 2)) > worst):
            h_rel, m = np.unravel_index(np.argmax(masked[:, k, :]), masked[:, k, :].shape)
            worst[k], worst_branch[k], worst_h[k] = masked[h_rel, k, m], m, first + h_rel
        low = vmin.min(axis=0)
        for k in np.flatnonzero(low < vmin_worst):
            vmin_worst[k], vmin_bus[k] = low[k], vbus[np.argmin(vmin[:, k]), k]
    rank = pd.DataFrame(
        {
            "outage": outages,
            **stats,
            "worst_loading_rate_b_pct": np.where(np.isfinite(worst), worst, np.nan),
            "worst_branch": [names[m] if m >= 0 else "" for m in worst_branch],
            "worst_h": worst_h,
            "lowest_vm_pu": np.where(np.isfinite(vmin_worst), vmin_worst, np.nan),
            "lowest_vm_bus": vmin_bus,
        }
    )
    rank = rank.sort_values(["insecure_h", "no_solution_h"], ascending=False)
    pairs = pd.DataFrame(counts, index=outages, columns=names)
    return rank.reset_index(drop=True), pairs


def hvdc_links(radial: list[str], table: pd.DataFrame) -> pd.DataFrame:
    """Radial HVDC connections: the exchange load at each terminal bus over the year."""
    load_p = SnapshotStore().tables["load_p"]
    degree = pd.concat([table.i, table.j]).value_counts()
    rows = []
    for name in radial:
        i, j = table.loc[name, ["i", "j"]]
        terminal = i if degree[i] == 1 else j  # the bus this branch alone connects
        for ex in EXCHANGE_LOADS:
            if ex.bus == terminal:
                p = load_p[f"{ex.bus}_{ex.load_id}"]
                rows.append(
                    {
                        "branch": name,
                        "terminal_bus": terminal,
                        "link": ex.link,
                        "max_import_mw": float(-p.min()),
                        "max_export_mw": float(p.max()),
                        "mean_abs_mw": float(p.abs().mean()),
                    }
                )
    return pd.DataFrame(rows)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--force", action="store_true", help="recompute the arrays from scratch")
    parser.add_argument("--crosscheck-hours", type=int, default=100)
    args = parser.parse_args()

    net = base_net()
    table = branch_table(net)
    names = list(table.index)
    radial = radial_branches(table)
    outages = [n for n in names if n not in radial]
    folder = configured_path("results_dir") / "n1"
    folder.mkdir(parents=True, exist_ok=True)
    pd.Series(outages, name="outage").to_csv(folder / "outages.csv", index=False)
    pd.Series(names, name="branch").to_csv(folder / "branches.csv", index=False)
    print(f"{len(outages)} contingencies; radial, left out: {radial}")

    if args.force or not (folder / "dc_loading.npy").exists():
        dc_screening(net, table, names, outages, folder)
    ac_sweep(names, outages, folder, resume=not args.force)
    arrays = load_arrays(folder)

    out = configured_path("results_dir")
    check_file = out / "n1_crosscheck.csv"
    if args.force or not check_file.exists():
        cuts = pd.read_parquet(out / "annual" / "cuts_mw.parquet")
        top = cuts["snitt 2 (SE2->SE3)"].nlargest(args.crosscheck_hours).index
        crosscheck(outages, names, arrays, sorted(int(h) for h in top)).to_csv(
            check_file, index=False, float_format="%.3g"
        )
    check = pd.read_csv(check_file)
    print(
        f"cross-check on {len(check)} h: max |dloading| {check.max_dloading_pct.max():.2g} "
        f"pp, convergence agrees in {check.convergence_agrees.sum()} h"
    )

    accuracy, errors = screening_accuracy(arrays, table)
    accuracy.to_csv(out / "n1_screening.csv", index=False, float_format="%.4g")
    print(accuracy.to_string(index=False))
    print({k: round(v, 3) for k, v in errors.items()})

    rank, pairs = ranking(arrays, table, names, outages)
    rank.to_csv(out / "n1_ranking.csv", index=False, float_format="%.4g")
    pairs.to_csv(folder / "induced_overload_pairs.csv")
    print(rank.head(15).to_string(index=False))
    hvdc = hvdc_links(radial, table)
    hvdc.to_csv(out / "n1_hvdc_links.csv", index=False, float_format="%.0f")
    print(hvdc.to_string(index=False))

    figures = out / "figures"
    plot_n1_ranking(rank, figures / "n1_ranking.png")
    plot_n1_map(pairs, rank, figures / "n1_map.png")
    plot_n1_screening(arrays, table, accuracy, errors, figures / "n1_screening.png")


if __name__ == "__main__":
    main()
