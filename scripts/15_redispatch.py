"""Redispatch needed to make the 2015 market dispatch secure (question P4).

For every hour, the cheapest-volume redispatch (``n44.redispatch``) that respects:
- **N-0:** the intact network within RATE A;
- **N-1:** additionally, every single outage within RATE B.

The starting point is each snapshot's dispatch: the scheduled output of every machine and
the swing bus's output from the annual AC run.

The redispatch is split into an **intra-zonal** part, a reshuffle between units of the same
zone, and an **inter-zonal** part, the net change per zone, which is a countertrade between
zones.

A sample of hours is checked in AC after the redispatch, with a full N-1 by the
reference-model solver.

**Sensitivity.** The N-1 redispatch is solved again without the outage of the Sima
transformer (5300-5301), whose post-contingency limits come from the equivalent lumping NO5's
hydro behind one transformer (README, question P3).

Writes results/annual/redispatch.parquet, redispatch_units.parquet and
redispatch_sensitivity.parquet, results/redispatch_summary.csv,
results/redispatch_sensitivity.csv, results/redispatch_ac_check.csv and the figures
redispatch.png and n1_bottlenecks.png (the branches that limit the N-1 redispatch, on the
map).

Usage: python scripts/15_redispatch.py [--workers N] [--force]
"""

import argparse
from concurrent.futures import ProcessPoolExecutor
from dataclasses import replace

import numpy as np
import pandas as pd
import yaml

from n44.acpf import branch_flows, grid_from_case, solve
from n44.annual import pre_contingency_flows
from n44.config import configured_path, load_config, repo_path
from n44.contingency import (
    branch_table,
    dc_model,
    radial_branches,
    shunt_outage_shift,
)
from n44.io.dataset import Dataset
from n44.io.psse_parser import parse_raw
from n44.opf import machine_table
from n44.plotting import plot_bottleneck_map, plot_redispatch, read_coordinates
from n44.redispatch import redispatch
from n44.snapshots import SnapshotStore, base_net

HOURS = 8760
SPREADS = (10.0, 20.0, 30.0)  # EUR/MWh between up- and down-regulation, for the cost range
AC_SAMPLE = 100  # random hours checked in AC, besides the representative ones
SENSITIVITY_OUTAGE = "transformer 5300-5301-1"  # Sima, an artefact of aggregation


def _setup():
    net = base_net()
    table = branch_table(net)
    names = list(table.index)
    outages = [n for n in names if n not in radial_branches(table)]
    dc = dc_model(net)
    return net, table, names, outages, dc, shunt_outage_shift(net, dc, outages)


def dispatch_table(net, units: pd.DataFrame) -> np.ndarray:
    """Output of every unit in every hour (hours x units), MW."""
    gen_p = SnapshotStore().tables["gen_p"]
    slack = pd.read_parquet(configured_path("results_dir") / "annual" / "summary.parquet")
    columns = []
    for row in units.itertuples():
        if row.element == "gen":
            columns.append(gen_p[f"{row.bus}_{row.id}"].to_numpy())
        else:
            columns.append(slack.slack_p_mw.to_numpy())
    return np.column_stack(columns)


def solve_hours(hours: np.ndarray) -> list[dict]:
    net, table, names, outages, dc, shift = _setup()
    units = machine_table(net)
    pre_all = pre_contingency_flows(names, table)
    dispatch = dispatch_table(net, units)
    rows = []
    for h in hours:
        pre = {key: value[h] for key, value in pre_all.items()}
        row = {"h": int(h)}
        for label, n1 in (("n0", False), ("n1", True)):
            r = redispatch(pre, dc, table, units, dispatch[h], outages, shift, n1=n1)
            delta = r.up - r.down
            zonal = pd.Series(delta, index=units.zone).groupby(level=0).sum()
            inter = float(zonal.clip(lower=0).sum())
            row.update(
                {
                    f"{label}_success": r.success,
                    f"{label}_volume_mw": r.volume_mw,
                    f"{label}_interzonal_mw": inter,
                    f"{label}_cost_eur": r.cost_eur,
                    f"{label}_unresolved_mw": r.unresolved_mw,
                    f"{label}_binding": "; ".join(r.binding),
                }
            )
            if n1:
                row["delta"] = delta
        rows.append(row)
    return rows


def solve_hours_without(hours: np.ndarray) -> list[dict]:
    """N-1 redispatch of ``hours`` with every outage except ``SENSITIVITY_OUTAGE``."""
    net, table, names, outages, dc, _ = _setup()
    outages = [o for o in outages if o != SENSITIVITY_OUTAGE]
    shift = shunt_outage_shift(net, dc, outages)
    units = machine_table(net)
    pre_all = pre_contingency_flows(names, table)
    dispatch = dispatch_table(net, units)
    rows = []
    for h in hours:
        pre = {key: value[h] for key, value in pre_all.items()}
        r = redispatch(pre, dc, table, units, dispatch[h], outages, shift, n1=True)
        zonal = pd.Series(r.up - r.down, index=units.zone).groupby(level=0).sum()
        rows.append(
            {
                "h": int(h),
                "success": r.success,
                "volume_mw": r.volume_mw,
                "interzonal_mw": float(zonal.clip(lower=0).sum()),
                "unresolved_mw": r.unresolved_mw,
                **{f"net_{zone}_mw": value for zone, value in zonal.items()},
            }
        )
    return rows


def _apply(grid, units: pd.DataFrame, delta: np.ndarray):
    """The grid with each machine's output moved by ``delta``; the swing bus balances."""
    s_spec = grid.s_spec.copy()
    for bus, element, d in zip(units.bus, units.element, delta, strict=True):
        if element == "gen":
            s_spec[grid.model.positions([bus])[0]] += d / grid.model.sbase
    return replace(grid, s_spec=s_spec)


def _ac_states(grid, positions, table) -> tuple[dict, dict, dict]:
    """AC base flows, the AC flow and active-power limit of every state, and an N-1 count.

    States: intact, then one per outage (NaN rows for outages without a solution). The limit
    is the rating at the more loaded end's voltage, reduced by the branch's reactive flow,
    as in ``n44.redispatch.effective_limits``. The count has the overloads the redispatch
    is meant to remove: intact branches beyond RATE A, post-contingency branches beyond
    RATE B, and outages without a solution.
    """
    base = solve(grid)
    current = (table.basis == "current").to_numpy()
    rates = (table.rate_a.to_numpy(), table.rate_b.to_numpy())
    n = len(positions) + 1
    flow = np.full((n, len(grid.names)), np.nan)
    limit = np.full_like(flow, np.nan)
    count = {"n0_overloads": 0, "post_overloads": 0, "no_solution": 0, "worst_post_pct_b": 0.0}
    pre = None
    for row, k in enumerate([None, *positions]):
        sol = base if k is None else solve(grid, outage=k, v0=base.v, max_iter=20)
        if not sol.converged:
            count["no_solution"] += 1
            continue
        si, sj, loading = branch_flows(grid, sol.v, outage=k)
        v = np.minimum(np.abs(sol.v[grid.f]), np.abs(sol.v[grid.t]))
        q = np.maximum(np.abs(si.imag), np.abs(sj.imag))
        rate = rates[0] if k is None else rates[1]
        scale = np.where(current, v, 1.0)
        flow[row] = (si.real - sj.real) / 2
        limit[row] = np.sqrt(np.maximum((rate * scale) ** 2 - q**2, 0.0))
        if k is None:
            pre = {
                "p_i": si.real,
                "p_j": sj.real,
                "q_i": si.imag,
                "q_j": sj.imag,
                "v_i": np.abs(sol.v[grid.f]),
                "v_j": np.abs(sol.v[grid.t]),
            }
            count["n0_overloads"] = int(np.nansum(loading > 100))
        else:
            pct_b = loading * rates[0] / rates[1]
            count["post_overloads"] += int(np.nansum(pct_b > 100))
            count["worst_post_pct_b"] = max(count["worst_post_pct_b"], float(np.nanmax(pct_b)))
    return pre, {"flow": flow, "limit": limit}, count


def ac_check(hours, units, table, outages, results, dc, shift, dispatch) -> pd.DataFrame:
    """Sample hours in AC: before the redispatch, after it, and after an AC correction.

    The correction re-linearises the redispatch around the AC states the DC redispatch
    leads to (``ac_states`` of ``n44.redispatch.redispatch``) and redispatches again, up to
    three times, until the hour is secure in AC.
    """
    ds = Dataset()
    rows = []
    for h in hours:
        grid = grid_from_case(parse_raw(ds.raw_text(h, "after")))
        positions = [grid.names.index(o) for o in outages]
        delta = np.asarray(results.loc[h, "delta"], dtype=float)
        row = {"h": h, "dc_volume_mw": float(np.clip(delta, 0, None).sum())}
        for label, d in (("before", np.zeros_like(delta)), ("after", delta)):
            _, _, count = _ac_states(_apply(grid, units, d), positions, table)
            row.update({f"{label}_{key}": value for key, value in count.items()})
        total, rounds = delta.copy(), 0
        for rounds in range(1, 4):
            pre, states, count = _ac_states(_apply(grid, units, total), positions, table)
            if pre is None or not (
                count["n0_overloads"] + count["post_overloads"] + count["no_solution"]
            ):
                rounds -= 1
                break
            r = redispatch(
                pre,
                dc,
                table,
                units,
                dispatch[h] + total,
                outages,
                shift,
                n1=True,
                ac_states=states,
            )
            total = total + (r.up - r.down)
        _, _, count = _ac_states(_apply(grid, units, total), positions, table)
        row.update({f"corrected_{key}": value for key, value in count.items()})
        row["corrected_volume_mw"] = float(np.clip(total, 0, None).sum())
        row["correction_rounds"] = rounds
        rows.append(row)
        print(f"  AC check h={h}: {row}", flush=True)
    return pd.DataFrame(rows)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workers", type=int, default=3)
    parser.add_argument("--force", action="store_true", help="recompute the annual redispatch")
    args = parser.parse_args()
    net, table, names, outages, dc, shift = _setup()
    units = machine_table(net)

    out = configured_path("results_dir")
    cached = out / "annual" / "redispatch_units.parquet"
    if args.force or not cached.exists():
        chunks = [c for c in np.array_split(np.arange(HOURS), args.workers * 3) if len(c)]
        with ProcessPoolExecutor(max_workers=args.workers) as pool:
            parts = list(pool.map(solve_hours, chunks))
        results = pd.DataFrame([row for part in parts for row in part]).set_index("h").sort_index()
        deltas = np.vstack(results.pop("delta").to_numpy())
        results.to_parquet(out / "annual" / "redispatch.parquet")
        unit_frame = pd.DataFrame(
            deltas, columns=[f"{b}_{i}" for b, i in zip(units.bus, units.id, strict=True)]
        )
        unit_frame.to_parquet(cached)
    else:
        results = pd.read_parquet(out / "annual" / "redispatch.parquet")
        deltas = pd.read_parquet(cached).to_numpy()

    zone_up = pd.DataFrame(np.clip(deltas, 0, None), columns=units.zone).T.groupby(level=0).sum().T
    zone_down = (
        pd.DataFrame(np.clip(-deltas, 0, None), columns=units.zone).T.groupby(level=0).sum().T
    )
    summary = []
    for label in ("n0", "n1"):
        volume = results[f"{label}_volume_mw"]
        inter = results[f"{label}_interzonal_mw"]
        row = {
            "criterion": "N-0 (RATE A)" if label == "n0" else "N-0 and N-1 (RATE A / RATE B)",
            "hours_with_redispatch": int((volume > 1).sum()),
            "volume_twh": volume.sum() / 1e6,
            "interzonal_twh": inter.sum() / 1e6,
            "mean_mw_when_active": volume[volume > 1].mean(),
            "max_mw": volume.max(),
            "unresolved_hours": int((results[f"{label}_unresolved_mw"] > 1).sum()),
            "failed_hours": int((~results[f"{label}_success"]).sum()),
        }
        for spread in SPREADS:
            row[f"cost_meur_at_{spread:.0f}"] = inter.sum() * spread / 1e6
        summary.append(row)
    summary = pd.DataFrame(summary)
    summary.to_csv(out / "redispatch_summary.csv", index=False, float_format="%.4g")
    print(summary.T.to_string())
    zones = pd.DataFrame({"up_twh": zone_up.sum() / 1e6, "down_twh": zone_down.sum() / 1e6})
    zones.to_csv(out / "redispatch_zones.csv", float_format="%.4g")
    print(zones)
    binding = results.n1_binding.str.split("; ").explode()
    binding = binding[binding != ""].value_counts().head(15)
    binding.rename("hours").to_csv(out / "redispatch_binding.csv")
    print(binding)

    sens_path = out / "annual" / "redispatch_sensitivity.parquet"
    if args.force or not sens_path.exists():
        chunks = [c for c in np.array_split(np.arange(HOURS), args.workers * 3) if len(c)]
        with ProcessPoolExecutor(max_workers=args.workers) as pool:
            parts = list(pool.map(solve_hours_without, chunks))
        sensitivity = pd.DataFrame([row for part in parts for row in part]).set_index("h")
        sensitivity.sort_index().to_parquet(sens_path)
    sensitivity = pd.read_parquet(sens_path)
    net_change = (zone_up.sum() - zone_down.sum()) / 1e6
    compared = pd.DataFrame(
        [
            {
                "outages": f"all {len(outages)}",
                "hours_with_redispatch": int((results.n1_volume_mw > 1).sum()),
                "volume_twh": results.n1_volume_mw.sum() / 1e6,
                "interzonal_twh": results.n1_interzonal_mw.sum() / 1e6,
                "unresolved_hours": int((results.n1_unresolved_mw > 1).sum()),
                **{f"net_{zone}_twh": value for zone, value in net_change.items()},
            },
            {
                "outages": f"all but {SENSITIVITY_OUTAGE}",
                "hours_with_redispatch": int((sensitivity.volume_mw > 1).sum()),
                "volume_twh": sensitivity.volume_mw.sum() / 1e6,
                "interzonal_twh": sensitivity.interzonal_mw.sum() / 1e6,
                "unresolved_hours": int((sensitivity.unresolved_mw > 1).sum()),
                **{
                    f"net_{zone}_twh": sensitivity[f"net_{zone}_mw"].sum() / 1e6
                    for zone in net_change.index
                },
            },
        ]
    )
    compared.to_csv(out / "redispatch_sensitivity.csv", index=False, float_format="%.4g")
    print(compared.T.to_string())

    with open(repo_path("config", "representative_hours.yaml"), encoding="utf-8") as fh:
        hours = [row["h"] for row in yaml.safe_load(fh)]
    rng = np.random.default_rng(2015)
    pool = np.setdiff1d(np.arange(HOURS), hours)
    hours += sorted(int(h) for h in rng.choice(pool, AC_SAMPLE, replace=False))
    results["delta"] = list(deltas)
    check = ac_check(hours, units, table, outages, results, dc, shift, dispatch_table(net, units))
    check.to_csv(out / "redispatch_ac_check.csv", index=False, float_format="%.4g")
    print(check.describe().T.to_string())
    plot_redispatch(results, zones, out / "figures" / "redispatch.png")
    items = results.n1_binding.str.split("; ").explode()
    items = items[items.fillna("") != ""]
    pairs = items.str.split(" | ", expand=True, regex=False)
    pairs.columns = ["branch", "state"]
    pairs = pairs.rename_axis("h").reset_index()
    url = load_config()["dataset"]["geo_coordinates_url"]
    coordinates = read_coordinates(configured_path("github_dir") / url.rsplit("/", 1)[-1])
    plot_bottleneck_map(net, coordinates, pairs, out / "figures" / "n1_bottlenecks.png")


if __name__ == "__main__":
    main()
