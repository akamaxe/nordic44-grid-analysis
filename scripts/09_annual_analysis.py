"""Analysis of the 8,760-hour run: Swedish cuts, congestion, drivers and Nord Pool error.

Reads results/annual/*.parquet (scripts/08_powerflow_8760.py) and the processed data, and
writes:

- figures in results/figures/: cuts_duration, cut_corridor_loading, month_hour,
  snitt2_drivers, border_errors;
- small summary tables in results/: annual_summary.csv, cut_statistics.csv,
  top_overloaded_branches.csv, border_errors.csv.

The numbers printed at the end feed the README (questions P1 and P2).
"""

import pandas as pd

from n44.config import configured_path
from n44.plotting import (
    plot_border_errors,
    plot_corridor_loading,
    plot_cut_duration,
    plot_drivers,
    plot_month_hour,
)
from n44.snapshots import SnapshotStore, base_net
from n44.zones import CUTS, EXCHANGE_LOAD_KEYS, nordpool_border_flows


def read_annual() -> dict[str, pd.DataFrame]:
    folder = configured_path("results_dir") / "annual"
    return {p.stem: pd.read_parquet(p) for p in folder.glob("*.parquet")}


def cut_branches(net) -> dict[str, list[str]]:
    """Names (as in the annual tables) of the branches crossing each cut."""
    zone = net.bus.zone
    branches = pd.concat(
        [
            pd.DataFrame(
                {
                    "name": [f"line {n}" for n in net.line.name],
                    "a": zone[net.line.from_bus].to_numpy(),
                    "b": zone[net.line.to_bus].to_numpy(),
                    "rate": net.line.rate_a_mva.to_numpy(),
                }
            ),
            pd.DataFrame(
                {
                    "name": [f"transformer {n}" for n in net.trafo.name],
                    "a": zone[net.trafo.hv_bus].to_numpy(),
                    "b": zone[net.trafo.lv_bus].to_numpy(),
                    "rate": net.trafo.rate_a_mva.to_numpy(),
                }
            ),
        ],
        ignore_index=True,
    )
    out = {}
    for cut, (north, south, _) in CUTS.items():
        crossing = (branches.a.isin(north) & branches.b.isin(south)) | (
            branches.a.isin(south) & branches.b.isin(north)
        )
        out[cut] = branches[crossing]
    return out


def zonal_totals(store: SnapshotStore, zone: pd.Series) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Hourly demand (exchange loads excluded) and generation per zone, in MW."""
    load_p, gen_p = store.tables["load_p"], store.tables["gen_p"]
    exchange = {f"{b}_{i}" for b, i in EXCHANGE_LOAD_KEYS}
    demand_cols = [c for c in load_p.columns if c not in exchange]
    load_zone = [zone[int(c.split("_")[0])] for c in demand_cols]
    demand = load_p[demand_cols].T.groupby(load_zone).sum().T
    gen_zone = [zone[int(c.split("_")[0])] for c in gen_p.columns]
    generation = gen_p.T.groupby(gen_zone).sum().T
    return demand, generation


def main() -> None:
    annual = read_annual()
    processed = configured_path("processed_dir")
    figures = configured_path("results_dir") / "figures"
    cal = pd.read_parquet(processed / "calendar.parquet")
    summary, cuts, borders = annual["summary"], annual["cuts_mw"], annual["borders_mw"]
    loading = annual["loading_pct"]
    net = base_net()
    members = cut_branches(net)
    nordpool = nordpool_border_flows(pd.read_parquet(processed / "nordpool_exchange.parquet"))
    real = ~cal.synthetic  # leave out the interpolated hour of the spring clock change

    # --- Swedish cuts: flows, capacities and corridor loading
    capacity = pd.Series({cut: m.rate.sum() for cut, m in members.items()})
    nordpool_cuts = pd.DataFrame(
        {cut: nordpool[f"{a}->{b}"] for cut, (_, _, (a, b)) in CUTS.items()}
    )
    corridor = pd.DataFrame({cut: loading[m.name].max(axis=1) for cut, m in members.items()})
    plot_cut_duration(cuts[real], nordpool_cuts[real], capacity, figures / "cuts_duration.png")
    plot_corridor_loading(corridor, figures / "cut_corridor_loading.png")
    rows = []
    for cut in CUTS:
        hours_at = {f"hours_ge_{p}": int((corridor[cut] >= p).sum()) for p in (80, 90, 100)}
        worst = loading[members[cut].name].ge(100).sum().sort_values(ascending=False)
        rows.append(
            {
                "cut": cut,
                "branches": len(members[cut]),
                "sum_rate_a_mw": capacity[cut],
                "model_mean_mw": cuts[cut].mean(),
                "model_p95_mw": cuts[cut].quantile(0.95),
                "model_max_mw": cuts[cut].max(),
                "nordpool_mean_mw": nordpool_cuts[cut][real].mean(),
                "nordpool_max_mw": nordpool_cuts[cut][real].max(),
                **hours_at,
                "most_overloaded_branch": worst.index[0],
                "its_hours_ge_100": int(worst.iloc[0]),
            }
        )
    cut_stats = pd.DataFrame(rows)
    out = configured_path("results_dir")
    cut_stats.to_csv(out / "cut_statistics.csv", index=False, float_format="%.0f")
    top = loading.ge(100).sum().sort_values(ascending=False)
    top = pd.DataFrame(
        {
            "hours_ge_100": top,
            "hours_ge_90": loading.ge(90).sum()[top.index],
            "max_loading_pct": loading.max()[top.index],
        }
    ).head(12)
    top.to_csv(out / "top_overloaded_branches.csv", float_format="%.1f", index_label="branch")

    # --- When does it happen? month x hour
    any_overload = (loading >= 100).any(axis=1).astype(float) * 100
    table = lambda s: s.groupby([cal.month, cal.hour]).mean().unstack()  # noqa: E731
    plot_month_hour(
        [
            (
                "Mean transfer through snitt 2",
                "SE1+SE2 to SE3+SE4, model",
                table(cuts["snitt 2 (SE2->SE3)"]),
                "MW",
            ),
            (
                "Hours with a branch above RATE A",
                "share of hours, whole network",
                table(any_overload),
                "% of hours",
            ),
        ],
        figures / "month_hour.png",
    )

    # --- What drives the north-south transfer?
    demand, generation = zonal_totals(SnapshotStore(), net.bus.zone)
    snitt2 = cuts["snitt 2 (SE2->SE3)"].rename("snitt 2 transfer")
    plot_drivers(
        snitt2,
        [
            ("SE3 demand (MW)", demand["SE3"]),
            ("SE1 + SE2 generation (MW)", generation["SE1"] + generation["SE2"]),
            (
                "Norwegian generation (MW)",
                generation[["NO1", "NO2", "NO3", "NO4", "NO5"]].sum(axis=1),
            ),
        ],
        "What moves power through snitt 2",
        figures / "snitt2_drivers.png",
    )

    # --- P1: model against Nord Pool per border
    diff = (borders[nordpool.columns] - nordpool)[real]
    mae = diff.abs().mean()
    monthly = diff.abs().groupby(cal.month[real]).mean()[["NO1->NO2", "SE1->FI1", "SE2->SE3"]]
    plot_border_errors(mae, monthly, figures / "border_errors.png")
    errors = pd.DataFrame(
        {
            "mean_abs_mw": mae,
            "bias_mw": diff.mean(),
            "nordpool_mean_abs_mw": nordpool[real].abs().mean(),
        }
    )
    errors["relative"] = errors.mean_abs_mw / errors.nordpool_mean_abs_mw
    errors.to_csv(out / "border_errors.csv", float_format="%.2f", index_label="border")
    loop = diff["NO1->NO2"] + diff["NO1->NO5"]  # zero for a pure loop flow round NO1-NO2-NO5

    ok = summary[summary.converged]
    key_numbers = {
        "hours_solved": len(summary),
        "hours_converged": len(ok),
        "flat_start_share": float((ok.init == "flat").mean()),
        "max_dvm_pu": ok.max_dvm_pu.max(),
        "max_dva_deg": ok.max_dva_deg.max(),
        "max_slack_dp_mw": (ok.slack_p_mw - ok.slack_p_psse_mw).abs().max(),
        "max_plant_dq_mvar": ok.max_dq_plant_mvar.max(),
        "losses_mean_mw": ok.losses_mw.mean(),
        "losses_max_mw": ok.losses_mw.max(),
        "losses_share_of_load_pct": float((ok.losses_mw / demand.sum(axis=1)).mean() * 100),
        "hours_any_overload": int((ok.n_overloaded > 0).sum()),
        "loop_residual_max_mw": float(loop.abs().max()),
        "loop_mean_abs_mw": float(diff["NO1->NO2"].abs().mean()),
    }
    pd.Series(key_numbers).to_csv(
        out / "annual_summary.csv", header=["value"], float_format="%.6g", index_label="metric"
    )

    with pd.option_context("display.width", 220, "display.max_columns", 20):
        print("Key numbers:\n", pd.Series(key_numbers).to_string(), "\n")
        print("Cut statistics:\n", cut_stats.to_string(index=False), "\n")
        print("Branches most often above RATE A:\n", top.to_string(), "\n")
        print(
            "Border errors (model - Nord Pool):\n",
            errors.sort_values("mean_abs_mw", ascending=False).round(2).to_string(),
            "\n",
        )
        norway = generation[["NO1", "NO2", "NO3", "NO4", "NO5"]].sum(axis=1)
        print(
            f"Snitt 2 correlations: SE3 demand {snitt2.corr(demand['SE3']):.2f}, SE1+SE2 "
            f"generation {snitt2.corr(generation['SE1'] + generation['SE2']):.2f}, NO "
            f"generation {snitt2.corr(norway):.2f}"
        )
        print(
            "Any-overload hours by month:", (ok.n_overloaded > 0).groupby(cal.month).sum().to_dict()
        )
        print(
            "Mean snitt 2 by month (MW):",
            cuts["snitt 2 (SE2->SE3)"].groupby(cal.month).mean().round(0).to_dict(),
        )


if __name__ == "__main__":
    main()
