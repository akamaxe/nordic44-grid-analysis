"""DC-OPF and nodal prices for every hour of 2015 (part of question P4).

Each hour's demand is the snapshot's loads and exchange loads, the line-end shunts, and the
AC losses of the annual run placed at the branch ends (loss-compensated DC). The OPF
(``n44.opf.dc_opf``) dispatches the units at the costs of config/costs.yaml within RATE A.

Nodal prices split into an energy component (the price at the slack bus) and a congestion
component (binding branches x PTDF). They describe the network under the cost assumptions;
the comparison with 2015's real zonal prices waits for the ENTSO-E data.

Also writes the comparison of the OPF's generation per zone with the market's, and checks
the OPF against pandapower's rundcopp on the representative hours.

Writes results/annual/opf_{lmp,shadow,dispatch}.parquet, results/opf_summary.csv,
results/opf_zones.csv, results/opf_binding.csv, results/opf_pandapower_check.csv,
results/opf_ac_check.csv (pandapower's AC-OPF on the representative hours) and
results/figures/opf_prices.png.

Usage: python scripts/14_opf_prices.py [--workers N]
"""

import argparse
import copy
from concurrent.futures import ProcessPoolExecutor

import numpy as np
import pandapower as pp
import pandas as pd
import yaml

from n44.config import configured_path, repo_path
from n44.contingency import branch_table, dc_model
from n44.opf import dc_opf, demand_vector, machine_table
from n44.plotting import plot_opf_prices
from n44.snapshots import SnapshotStore, base_net, load_snapshot

HOURS = 8760


def hourly_demand(net, table) -> pd.DataFrame:
    """Demand per bus for every hour (hours x buses): loads, shunts and branch losses."""
    load_p = SnapshotStore().tables["load_p"]
    buses = net.bus.index
    by_bus = load_p.T.groupby(lambda c: int(c.split("_")[0])).sum().T
    demand = by_bus.reindex(columns=buses, fill_value=0.0)
    shunt = net.shunt[net.shunt.in_service].p_mw.groupby(net.shunt.bus).sum()
    demand = demand + shunt.reindex(buses, fill_value=0.0).to_numpy()[None, :]
    annual = configured_path("results_dir") / "annual"
    names = list(table.index)
    loss = (
        pd.read_parquet(annual / "p_i_mw.parquet")[names]
        + pd.read_parquet(annual / "p_j_mw.parquet")[names]
    ).to_numpy() / 2
    for column, ends in ((loss, table.i.to_numpy()), (loss, table.j.to_numpy())):
        for k, bus in enumerate(ends):
            demand[bus] += column[:, k]
    return demand


def solve_hours(hours: np.ndarray) -> dict[str, np.ndarray]:
    net = base_net()
    table = branch_table(net)
    dc = dc_model(net)
    units = machine_table(net)
    demand = hourly_demand(net, table)
    out = {"lmp": [], "energy": [], "shadow": [], "dispatch": [], "cost": [], "success": []}
    for h in hours:
        r = dc_opf(dc, units, demand.loc[h], table.rate_a)
        out["success"].append(r.success)
        if not r.success:
            out["lmp"].append(np.full(len(net.bus), np.nan))
            out["energy"].append(np.nan)
            out["shadow"].append(np.full(len(table), np.nan))
            out["dispatch"].append(np.full(len(units), np.nan))
            out["cost"].append(np.nan)
            continue
        out["lmp"].append(r.lmp.reindex(net.bus.index).to_numpy())
        out["energy"].append(r.energy)
        out["shadow"].append(r.shadow.to_numpy())
        out["dispatch"].append(r.dispatch.to_numpy())
        out["cost"].append(r.cost)
    return {key: np.asarray(value) for key, value in out.items()} | {"hours": hours}


def pandapower_check(hours: list[int]) -> pd.DataFrame:
    """The OPF against pandapower's rundcopp (lossless, same costs and limits)."""
    net, store = base_net(), SnapshotStore()
    table = branch_table(net)
    dc = dc_model(net)
    units = machine_table(net)
    rows = []
    for h in hours:
        load_snapshot(h, store, net)
        own = dc_opf(dc, units, demand_vector(net), table.rate_a)
        work = copy.deepcopy(net)
        work.line["max_loading_percent"] = 100.0
        work.trafo["max_loading_percent"] = 100.0
        work.gen["controllable"] = True
        work.load["controllable"] = False
        for row in units.itertuples():
            pp.create_poly_cost(work, row.index, row.element, cp1_eur_per_mw=row.cost)
        pp.rundcopp(work)
        lam = work.res_bus.lam_p.loc[own.lmp.index]
        rows.append(
            {
                "h": h,
                "cost_own_eur": own.cost,
                "cost_pandapower_eur": float(work.res_cost),
                "max_dlmp_eur": float(np.abs(lam - own.lmp).max()),
                "binding_branches": int((own.shadow > 1e-6).sum()),
            }
        )
    return pd.DataFrame(rows)


def ac_opf_check(hours: list[int]) -> pd.DataFrame:
    """pandapower's AC-OPF against the loss-compensated DC-OPF on the same hours.

    The AC-OPF prices losses and reactive power and may move voltages within 0.9-1.1 p.u.;
    the zonal mean prices of the two show the size of the loss component.
    """
    net, store = base_net(), SnapshotStore()
    table = branch_table(net)
    dc = dc_model(net)
    units = machine_table(net)
    demand = hourly_demand(net, table)
    rows = []
    for h in hours:
        load_snapshot(h, store, net)
        dc_result = dc_opf(dc, units, demand.loc[h], table.rate_a)
        work = copy.deepcopy(net)
        work.line["max_loading_percent"] = 100.0
        work.trafo["max_loading_percent"] = 100.0
        work.gen["controllable"] = True
        work.load["controllable"] = False
        for row in units.itertuples():
            pp.create_poly_cost(work, row.index, row.element, cp1_eur_per_mw=row.cost)
        try:
            pp.runopp(work, init="pf", calculate_voltage_angles=True)
        except pp.OPFNotConverged:
            rows.append({"h": h, "converged": False})
            continue
        zone = net.bus.zone
        ac_zone = work.res_bus.lam_p.groupby(zone).mean()
        dc_zone = dc_result.lmp.groupby(zone).mean()
        generation = work.res_gen.p_mw.sum() + work.res_ext_grid.p_mw.sum()
        rows.append(
            {
                "h": h,
                "converged": True,
                "cost_ac_eur": float(work.res_cost),
                "cost_dc_eur": dc_result.cost,
                "losses_ac_mw": float(generation - work.res_load.p_mw.sum()),
                "max_zone_price_diff_eur": float((ac_zone - dc_zone).abs().max()),
                **{f"ac_{z}": ac_zone[z] for z in ac_zone.index},
                **{f"dc_{z}": dc_zone[z] for z in dc_zone.index},
            }
        )
    return pd.DataFrame(rows)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workers", type=int, default=3)
    args = parser.parse_args()
    net = base_net()
    table = branch_table(net)
    units = machine_table(net)
    chunks = [c for c in np.array_split(np.arange(HOURS), args.workers * 3) if len(c)]
    with ProcessPoolExecutor(max_workers=args.workers) as pool:
        parts = list(pool.map(solve_hours, chunks))
    merged = {key: np.concatenate([p[key] for p in parts]) for key in parts[0]}
    order = np.argsort(merged["hours"])
    merged = {key: value[order] for key, value in merged.items()}
    lmp = pd.DataFrame(merged["lmp"], columns=net.bus.index.astype(str))
    shadow = pd.DataFrame(merged["shadow"], columns=list(table.index))
    dispatch = pd.DataFrame(
        merged["dispatch"], columns=[f"{b}_{i}" for b, i in zip(units.bus, units.id, strict=True)]
    )
    annual = configured_path("results_dir") / "annual"
    lmp.to_parquet(annual / "opf_lmp.parquet")
    shadow.to_parquet(annual / "opf_shadow.parquet")
    dispatch.to_parquet(annual / "opf_dispatch.parquet")
    energy = pd.Series(merged["energy"])

    zone = net.bus.zone
    zone_price = lmp.T.groupby(zone.to_numpy()).mean().T
    congestion = lmp.sub(energy, axis=0)
    zone_congestion = congestion.T.groupby(zone.to_numpy()).mean().T
    gen_p = SnapshotStore().tables["gen_p"]
    market = gen_p.T.groupby(lambda c: int(c.split("_")[0])).sum().T
    slack = pd.read_parquet(annual / "summary.parquet").slack_p_mw.to_numpy()
    market[int(net.ext_grid.bus.iloc[0])] = slack
    plant_zone = (
        pd.Series(units.zone.to_numpy(), index=units.bus.to_numpy()).groupby(level=0).first()
    )
    market_zone = market.T.groupby(plant_zone.reindex(market.columns).to_numpy()).sum().T
    opf_zone = dispatch.T.groupby(units.zone.to_numpy()).sum().T
    zones = pd.DataFrame(
        {
            "mean_nodal_price_eur": zone_price.mean(),
            "mean_congestion_eur": zone_congestion.mean(),
            "market_generation_twh": market_zone.sum() / 1e6,
            "opf_generation_twh": opf_zone.sum() / 1e6,
        }
    )
    out = configured_path("results_dir")
    zones.to_csv(out / "opf_zones.csv", float_format="%.4g")
    binding = shadow.gt(1e-6)
    binding_table = (
        pd.DataFrame({"hours": binding.sum(), "mean_shadow_eur": shadow.where(binding).mean()})
        .query("hours > 0")
        .sort_values("hours", ascending=False)
    )
    binding_table.to_csv(out / "opf_binding.csv", float_format="%.4g")
    spread = lmp.max(axis=1) - lmp.min(axis=1)
    summary = pd.DataFrame(
        [
            {
                "hours_solved": int(merged["success"].sum()),
                "hours_congested": int((binding.any(axis=1)).sum()),
                "mean_energy_price_eur": energy.mean(),
                "mean_spread_eur": spread.mean(),
                "hours_spread_above_5_eur": int((spread > 5).sum()),
                "mean_opf_cost_meur_per_h": np.nanmean(merged["cost"]) / 1e6,
            }
        ]
    )
    summary.to_csv(out / "opf_summary.csv", index=False, float_format="%.4g")
    print(summary.T.to_string())
    print(zones.round(2).to_string())
    print(binding_table.head(10).to_string())

    with open(repo_path("config", "representative_hours.yaml"), encoding="utf-8") as fh:
        hours = [row["h"] for row in yaml.safe_load(fh)]
    check = pandapower_check(hours)
    check.to_csv(out / "opf_pandapower_check.csv", index=False, float_format="%.6g")
    print(check.to_string(index=False))
    ac = ac_opf_check(hours)
    ac.to_csv(out / "opf_ac_check.csv", index=False, float_format="%.5g")
    print(ac.to_string(index=False))
    plot_opf_prices(zones, binding_table, out / "figures" / "opf_prices.png")


if __name__ == "__main__":
    main()
