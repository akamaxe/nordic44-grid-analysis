"""DC optimal power flow and nodal prices.

The OPF is a linear program in PTDF form, solved with HiGHS through ``scipy.optimize``:

    min  sum_g c_g P_g
    s.t. sum_g P_g = sum_n D_n                     (balance)
         |PTDF (C_g P - D)| <= F_max               (branch limits, RATE A taken as MW)
         PB_g <= P_g <= PT_g

D holds the loads, the exchange loads (fixed: they stand for links to neighbours outside the
model), the active power of the line-end shunts and, in the loss-compensated variant, each
branch's AC losses split between its ends.

Nodal prices come from the duals. The balance constraint gives the price at the slack
bus (the energy component), and each binding branch adds PTDF x its shadow price (the
congestion component). A lossless DC-OPF has no loss component.

pandapower's ``rundcopp`` solves the same problem and checks this one (tests/test_opf.py).
Generation costs are the assumptions in config/costs.yaml.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import cache

import numpy as np
import pandas as pd
import yaml
from scipy.optimize import linprog

from n44.config import repo_path
from n44.contingency import DcModel


@cache
def cost_assumptions() -> dict:
    with open(repo_path("config", "costs.yaml"), encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def machine_table(net) -> pd.DataFrame:
    """Every generating unit with its bus, limits, technology and marginal cost.

    The swing bus's machines, which pandapower holds as one external grid, form one unit
    here, so the table matches the network's generators one to one.
    """
    costs = cost_assumptions()
    plants = costs["plants"]
    tech_cost = costs["technologies"]
    rows = []
    for idx, g in net.gen.iterrows():
        plant = plants[int(g.bus)]
        tech = plant.get("machines", {}).get(str(g.psse_id), plant["technology"])
        rows.append(
            (
                "gen",
                idx,
                int(g.bus),
                str(g.psse_id),
                g.min_p_mw,
                g.max_p_mw,
                tech,
                plant["zone"],
                bool(g.in_service),
            )
        )
    for idx, e in net.ext_grid.iterrows():
        plant = plants[int(e.bus)]
        rows.append(
            (
                "ext_grid",
                idx,
                int(e.bus),
                e.psse_machines,
                e.min_p_mw,
                e.max_p_mw,
                plant["technology"],
                plant["zone"],
                bool(e.in_service),
            )
        )
    table = pd.DataFrame(
        rows,
        columns=[
            "element",
            "index",
            "bus",
            "id",
            "p_min_mw",
            "p_max_mw",
            "technology",
            "zone",
            "in_service",
        ],
    )
    table["cost"] = table.technology.map(tech_cost)
    return table


def demand_vector(net, losses: pd.Series | None = None) -> pd.Series:
    """Active demand per bus (MW): loads, line-end shunts and, optionally, branch losses."""
    load = net.load[net.load.in_service]
    demand = load.p_mw.groupby(load.bus).sum()
    shunt = net.shunt[net.shunt.in_service]
    demand = demand.add(shunt.p_mw.groupby(shunt.bus).sum(), fill_value=0.0)
    if losses is not None:
        demand = demand.add(losses, fill_value=0.0)
    return demand.reindex(net.bus.index, fill_value=0.0)


def loss_loads(flows: pd.DataFrame) -> pd.Series:
    """Each branch's AC losses as loads, half at each end (MW per bus)."""
    half = flows.loss_mw / 2
    return (
        pd.concat([half.groupby(flows.i).sum(), half.groupby(flows.j).sum()]).groupby(level=0).sum()
    )


@dataclass
class OpfResult:
    success: bool
    message: str
    dispatch: pd.Series | None = None  # MW per machine (machine table order)
    lmp: pd.Series | None = None  # EUR/MWh per bus
    energy: float = np.nan  # EUR/MWh, price at the slack bus
    flows: pd.Series | None = None  # MW per branch, I -> J
    shadow: pd.Series | None = None  # EUR/MWh per MW of limit, per branch
    cost: float = np.nan  # EUR/h
    fixed_overload_mw: float = 0.0  # overload on branches no unit can relieve

    @property
    def congestion(self) -> pd.Series:
        return self.lmp - self.energy


def dc_opf(dc: DcModel, machines: pd.DataFrame, demand: pd.Series, limits: pd.Series) -> OpfResult:
    """DC-OPF of one hour. ``limits`` are MW per branch (``dc.ptdf`` row order).

    A branch whose flow no unit can change (a radial connection carrying a fixed exchange) is
    not a constraint of the dispatch; its overload, if any, is reported as
    ``fixed_overload_mw``. In 24 hours of 2015 the Estlink exchange exceeds the 1,000 MW
    rating of its radial connection in the equivalent by up to 3 MW.
    """
    live = machines[machines.in_service]
    ptdf = dc.ptdf.loc[limits.index]
    h_all = ptdf[live.bus].to_numpy()  # branches x machines
    base_all = ptdf.to_numpy() @ demand.reindex(ptdf.columns, fill_value=0.0).to_numpy()
    keep = np.abs(h_all).max(axis=1) > 1e-9  # branches the dispatch can move
    h, base, cap = h_all[keep], base_all[keep], limits.to_numpy()[keep]
    result = linprog(
        live.cost.to_numpy(),
        A_ub=np.r_[h, -h],
        b_ub=np.r_[cap + base, cap - base],
        A_eq=np.ones((1, len(live))),
        b_eq=[demand.sum()],
        bounds=list(zip(live.p_min_mw, live.p_max_mw, strict=True)),
        method="highs",
    )
    if not result.success:
        return OpfResult(False, result.message)
    n = len(cap)
    mu_up, mu_down = result.ineqlin.marginals[:n], result.ineqlin.marginals[n:]
    energy = float(result.eqlin.marginals[0])
    lmp = energy + ptdf.to_numpy()[keep].T @ (mu_up - mu_down)
    dispatch = pd.Series(0.0, index=machines.index)
    dispatch[live.index] = result.x
    shadow = np.zeros(len(limits))
    shadow[keep] = -(mu_up + mu_down)
    flows = h_all @ result.x - base_all
    fixed = np.abs(flows[~keep]) - limits.to_numpy()[~keep]
    return OpfResult(
        success=True,
        message=result.message,
        dispatch=dispatch,
        lmp=pd.Series(lmp, index=ptdf.columns),
        energy=energy,
        flows=pd.Series(flows, index=limits.index),
        shadow=pd.Series(shadow, index=limits.index),
        cost=float(result.fun),
        fixed_overload_mw=float(np.clip(fixed, 0, None).sum()),
    )
