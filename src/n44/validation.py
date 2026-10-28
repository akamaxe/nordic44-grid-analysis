"""Comparison of load-flow solutions from different tools, keyed by PSS/E identifiers.

Each tool's result is first turned into a ``Solution``: bus voltages, plant outputs and branch
terminal flows indexed by PSS/E bus numbers and branch I-J-CKT, in PSS/E orientation. Any two
solutions can then be compared the same way:

- ``solution_from_case``: the PSS/E solution stored in an after_PF file (the reference);
- ``solution_from_net``: a solved pandapower network;
- ``solution_from_powerfactory``: the tables exported by ``powerfactory/run_ldf_batch.py``.

Tolerances come from ``validation`` in config/base.yaml.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from n44.config import load_config
from n44.io.psse_parser import RawCase
from n44.results import branch_flows as pp_branch_flows
from n44.ybus import branch_flows as reference_branch_flows
from n44.ybus import build_reference
from n44.zones import bus_zones

BRANCH_KEYS = ["kind", "i", "j", "ckt"]
FLOW_COLUMNS = ["p_i_mw", "q_i_mvar", "p_j_mw", "q_j_mvar", "loading_a_pct"]


@dataclass
class Solution:
    """A load-flow solution in PSS/E identifiers.

    ``buses`` (index ``bus``): vm_pu, va_degree. ``plants`` (index ``bus``, generator buses
    only): p_mw, q_mvar of all in-service machines together. ``branches`` (index kind, i, j,
    ckt): terminal flows in PSS/E orientation and loading on RATE A, as
    ``n44.ybus.branch_flows``. ``slack_p_mw`` is the output of the swing bus and
    ``losses_mw`` total generation minus total load.
    """

    buses: pd.DataFrame
    plants: pd.DataFrame
    branches: pd.DataFrame
    slack_p_mw: float
    losses_mw: float


def solution_from_case(case: RawCase) -> Solution:
    """The PSS/E solution stored in a case (meaningful for after_PF files only)."""
    live = case.bus[case.bus.ide != 4]
    buses = pd.DataFrame(
        {"vm_pu": live.vm.to_numpy(), "va_degree": live.va.to_numpy()},
        index=pd.Index(live.i.to_numpy(), name="bus"),
    )
    gen = case.generator[case.generator.stat == 1]
    plants = pd.DataFrame({"p_mw": gen.groupby("i").pg.sum(), "q_mvar": gen.groupby("i").qg.sum()})
    plants.index.name = "bus"
    flows = reference_branch_flows(case, build_reference(case)).set_index(BRANCH_KEYS)
    swing = case.bus.i[case.bus.ide == 3]
    load = case.load[case.load.status == 1]
    return Solution(
        buses=buses,
        plants=plants,
        branches=flows[FLOW_COLUMNS],
        slack_p_mw=float(gen.pg[gen.i.isin(swing)].sum()),
        losses_mw=float(gen.pg.sum() - load.pl.sum()),
    )


def solution_from_net(net) -> Solution:
    """A solved pandapower network built by ``n44.io.build_net``."""
    live = net.bus.index[net.bus.in_service]
    buses = pd.DataFrame(
        {"vm_pu": net.res_bus.vm_pu[live], "va_degree": net.res_bus.va_degree[live]}
    )
    buses.index.name = "bus"
    parts = [
        net.res_gen[["p_mw", "q_mvar"]].groupby(net.gen.bus).sum(),
        net.res_ext_grid[["p_mw", "q_mvar"]].groupby(net.ext_grid.bus).sum(),
    ]
    plants = pd.concat(parts).groupby(level=0).sum()
    plants.index.name = "bus"
    flows = pp_branch_flows(net).set_index(BRANCH_KEYS)
    generation = net.res_ext_grid.p_mw.sum() + net.res_gen.p_mw.sum()
    return Solution(
        buses=buses,
        plants=plants,
        branches=flows[FLOW_COLUMNS],
        slack_p_mw=float(net.res_ext_grid.p_mw.sum()),
        losses_mw=float(generation - net.res_load.p_mw.sum()),
    )


def branch_orientation(case: RawCase) -> dict[tuple, tuple]:
    """Map (kind, bus_a, bus_b, ckt) in either order to the PSS/E key and a swap flag."""
    keys = {}
    tables = (("line", case.branch, "st"), ("transformer", case.transformer, "stat"))
    for kind, table, status in tables:
        for i, j, ckt in table.loc[table[status] == 1, ["i", "j", "ckt"]].itertuples(index=False):
            keys[(kind, i, j, ckt)] = ((kind, i, j, ckt), False)
            keys[(kind, j, i, ckt)] = ((kind, i, j, ckt), True)
    return keys


def solution_from_powerfactory(
    buses: pd.DataFrame, machines: pd.DataFrame, branches: pd.DataFrame, case: RawCase
) -> Solution:
    """Tables exported from PowerFactory for one hour, oriented and keyed like PSS/E.

    PowerFactory reports each branch from its own first terminal (``bus1``/``bus2``, or HV/LV
    for transformers); the PSS/E record of the same hour decides the orientation. The swing
    bus and the loads (constant power) also come from ``case``.

    Loading follows PSS/E's convention, recomputed from PowerFactory's terminal flows and
    voltages: PowerFactory's own figure uses the series current of a line (without its end
    shunts) and the current of a transformer, while PSS/E uses the terminal current and
    MVA (validation log, entry 7). PowerFactory's figure is kept as ``loading_pf_pct``.
    """
    orient = branch_orientation(case)
    rows = []
    for row in branches[branches.in_service.astype(bool)].itertuples(index=False):
        key, swap = orient[(row.kind, row.bus1, row.bus2, str(row.ckt))]
        flows = (row.p2_mw, row.q2_mvar, row.p1_mw, row.q1_mvar)
        if not swap:
            flows = (row.p1_mw, row.q1_mvar, row.p2_mw, row.q2_mvar)
        rows.append((*key, *flows, row.loading_pct))
    columns = BRANCH_KEYS + FLOW_COLUMNS[:4] + ["loading_pf_pct"]
    flows = pd.DataFrame(rows, columns=columns).set_index(BRANCH_KEYS)
    vm = buses.set_index("bus").vm_pu
    ratings = build_reference(case).branches.set_index(BRANCH_KEYS)
    ratings = ratings.loc[flows.index, ["rate_a", "rating_basis"]]
    s_i = np.hypot(flows.p_i_mw, flows.q_i_mvar).to_numpy()
    s_j = np.hypot(flows.p_j_mw, flows.q_j_mvar).to_numpy()
    v_i = vm[flows.index.get_level_values("i")].to_numpy()
    v_j = vm[flows.index.get_level_values("j")].to_numpy()
    by_current = np.maximum(s_i / v_i, s_j / v_j)
    worst = np.where(ratings.rating_basis == "current", by_current, np.maximum(s_i, s_j))
    rate = ratings.rate_a.to_numpy()
    with np.errstate(divide="ignore", invalid="ignore"):
        flows.insert(4, "loading_a_pct", np.where(rate > 0, worst / rate * 100, np.nan))
    on = machines[machines.in_service.astype(bool)]
    plants = on.groupby("bus")[["p_mw", "q_mvar"]].sum()
    swing = case.bus.i[case.bus.ide == 3]
    load = case.load[case.load.status == 1]
    return Solution(
        buses=buses.set_index("bus")[["vm_pu", "va_degree"]],
        plants=plants,
        branches=flows,
        slack_p_mw=float(on.p_mw[on.bus.isin(swing)].sum()),
        losses_mw=float(on.p_mw.sum() - load.pl.sum()),
    )


@dataclass
class HourComparison:
    buses: pd.DataFrame
    plants: pd.DataFrame
    branches: pd.DataFrame
    summary: dict


def compare(
    test: Solution, ref: Solution, case: RawCase, labels: tuple[str, str] = ("pp", "ref")
) -> HourComparison:
    """Compare two solutions of the same hour and check the result against the tolerances.

    Columns carry the two labels (``vm_pp``, ``vm_ref``...); differences are test minus
    reference. ``case`` supplies the zones and the reactive limits of the plants.
    """
    t, r = labels
    tol = load_config()["validation"]
    zone = bus_zones(case.bus, case.area)
    buses = test.buses.join(ref.buses, lsuffix=f"_{t}", rsuffix=f"_{r}", how="inner")
    buses = buses.rename(columns=lambda c: c.replace("vm_pu", "vm").replace("va_degree", "va"))
    buses.insert(0, "zone", zone.reindex(buses.index).to_numpy())
    buses["dvm"] = buses[f"vm_{t}"] - buses[f"vm_{r}"]
    buses["dva"] = buses[f"va_{t}"] - buses[f"va_{r}"]

    gen = case.generator[case.generator.stat == 1].groupby("i")
    plants = pd.DataFrame({f"q_{r}": ref.plants.q_mvar, "qmax": gen.qt.sum(), "qmin": gen.qb.sum()})
    plants = plants.dropna(subset=[f"q_{r}"])
    plants[f"q_{t}"] = test.plants.q_mvar.reindex(plants.index)
    plants["dq"] = plants[f"q_{t}"] - plants[f"q_{r}"]
    margin = 0.5  # Mvar
    for label in (r, t):
        q = plants[f"q_{label}"]
        plants[f"limited_{label}"] = (q >= plants.qmax - margin) | (q <= plants.qmin + margin)
    plants.index.name = "bus"

    branches = test.branches[FLOW_COLUMNS].join(
        ref.branches[FLOW_COLUMNS], lsuffix=f"_{t}", rsuffix=f"_{r}", how="inner"
    )
    for quantity in FLOW_COLUMNS:
        branches[f"d_{quantity}"] = branches[f"{quantity}_{t}"] - branches[f"{quantity}_{r}"]

    def ends_within(columns: tuple[str, str], absolute: float, relative: float) -> bool:
        """Every branch end within the absolute or the relative tolerance."""
        diff = branches[[f"d_{c}" for c in columns]].abs().to_numpy()
        value = branches[[f"{c}_{r}" for c in columns]].abs().to_numpy()
        return bool(((diff <= absolute) | (diff <= relative * value)).all())

    dp = branches[["d_p_i_mw", "d_p_j_mw"]].abs().to_numpy().max()
    dq = branches[["d_q_i_mvar", "d_q_j_mvar"]].abs().to_numpy().max()
    summary = {
        "max_dvm_pu": buses.dvm.abs().max(),
        "rmse_dvm_pu": np.sqrt((buses.dvm**2).mean()),
        "max_dva_deg": buses.dva.abs().max(),
        "rmse_dva_deg": np.sqrt((buses.dva**2).mean()),
        "max_dp_branch_mw": dp,
        "max_dq_branch_mvar": dq,
        "rmse_dp_branch_mw": np.sqrt((branches.d_p_i_mw**2).mean()),
        "max_dloading_pct": branches.d_loading_a_pct.abs().max(),
        "slack_dp_mw": test.slack_p_mw - ref.slack_p_mw,
        f"losses_{r}_mw": ref.losses_mw,
        f"losses_{t}_mw": test.losses_mw,
        "losses_rel": abs(test.losses_mw - ref.losses_mw) / ref.losses_mw,
        "max_dq_plant_mvar": plants.dq.abs().max(),
        "limited_plants_match": bool((plants[f"limited_{r}"] == plants[f"limited_{t}"]).all()),
        f"n_limited_{r}": int(plants[f"limited_{r}"].sum()),
    }
    summary["within_tolerance"] = bool(
        summary["max_dvm_pu"] <= tol["vm_pu"]
        and summary["max_dva_deg"] <= tol["va_deg"]
        and ends_within(("p_i_mw", "p_j_mw"), tol["p_mw"], tol["p_rel"])
        and ends_within(("q_i_mvar", "q_j_mvar"), tol["q_mvar"], tol["q_rel"])
        and summary["losses_rel"] <= tol["losses_rel"]
        and abs(summary["slack_dp_mw"]) <= tol["slack_p_mw"]
    )
    return HourComparison(buses, plants, branches, summary)


def compare_hour(net, case: RawCase) -> HourComparison:
    """pandapower against the PSS/E solution of the same hour."""
    return compare(solution_from_net(net), solution_from_case(case), case, ("pp", "ref"))


def representative_hours(
    demand: pd.Series,
    nordpool_exchange: pd.DataFrame,
    nordpool_production: pd.DataFrame,
    max_loading: pd.Series,
    calendar: pd.DataFrame,
) -> pd.DataFrame:
    """At least ten hours covering seasons, load levels and transfer extremes.

    Each criterion picks the best hour not already chosen. The synthetic hour of the spring
    clock change is never picked.
    """
    month = calendar.month
    winter, summer = month.isin([12, 1, 2]), month.isin([6, 7, 8])
    snitt2 = -nordpool_exchange["SE2_SE3"]  # SE2 -> SE3
    se3_se4 = -nordpool_exchange["SE3_SE4"]  # SE3 -> SE4
    hydro = nordpool_production[["NO1", "NO2", "NO3", "NO4", "NO5", "SE1", "SE2"]].sum(axis=1)
    criteria = [
        ("winter peak demand", demand.where(winter), "max"),
        ("winter lowest demand", demand.where(winter), "min"),
        ("summer peak demand", demand.where(summer), "max"),
        ("summer lowest demand", demand.where(summer), "min"),
        ("highest SE2->SE3 transfer (snitt 2)", snitt2, "max"),
        ("lowest SE2->SE3 transfer (snitt 2)", snitt2, "min"),
        ("highest SE3->SE4 transfer", se3_se4, "max"),
        ("highest Finnish import", nordpool_exchange["FI"], "max"),
        ("highest NO+SE1+SE2 production (hydro proxy)", hydro, "max"),
        ("highest branch loading", max_loading, "max"),
    ]
    chosen, rows = set(), []
    for label, series, how in criteria:
        ranked = series.drop(index=calendar.index[calendar.synthetic]).dropna()
        ranked = ranked.sort_values(ascending=(how == "min"))
        h = int(next(k for k in ranked.index if k not in chosen))
        chosen.add(h)
        rows.append(
            {
                "h": h,
                "criterion": label,
                "value": float(series[h]),
                "date": str(calendar.date[h].date()),
                "hour": int(calendar.hour[h]),
            }
        )
    return pd.DataFrame(rows)
