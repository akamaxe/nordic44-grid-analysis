"""Redispatch that makes the market dispatch secure.

The starting point is each snapshot's dispatch, which follows the zonal market outcome. The
redispatch is a linear program solved with HiGHS through ``scipy.optimize``:

    min  sum_g (m + w c_g) up_g + (m - w c_g) down_g + penalty x sum(slack)
    s.t. sum_g (up_g - down_g) = 0                                  (balance, losses held)
         0 <= up_g <= PT_g - P_g,   0 <= down_g <= P_g - PB_g        (unit limits)
         |F_m + sum_g PTDF(m, g) (up_g - down_g)| <= L_m + slack     (intact, RATE A)
         |F_m|k + sum_g PTDF_k(m, g) (up_g - down_g)| <= L_m^B + slack  (after outage k, RATE B)

- F are the hour's AC flows in DC terms, (P_i - P_j)/2. F_m|k are their post-contingency
  values from LODF, with the line-end shunts that trip with a line
  (``n44.contingency.shunt_outage_shift``).
- PTDF_k = PTDF + LODF[:, k] PTDF[k, :] is the sensitivity after outage k.
- Limits are on active power, with each end's reactive flow and voltage held at the hour's
  values: the same "AC base, DC delta" as the N-1 screening.
- **The objective is the volume moved**, with costs only breaking ties (w = 0.02, small
  enough for every coefficient to stay positive). The costs in config/costs.yaml are
  assumptions that do not reproduce the market dispatch: a cost-minimising redispatch would
  also "re-optimise" the market, moving some 10 GW an hour for profit, not for security.
- An overload that cannot be removed stays as slack, at a penalty, and is reported.
- Post-contingency constraints are added as they are violated (constraint generation),
  which converges in a few rounds.

Reported: the volume, sum up_g (= sum down_g); the change in production cost under the
cost assumptions, sum c_g (up_g - down_g); and a handling premium of m per MW moved up or
down. ``n44.contingency.ac_n1_sweep`` then checks the redispatched hour in AC.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd
from scipy.optimize import linprog

from n44.contingency import DcModel
from n44.opf import cost_assumptions

TIE_BREAK = 0.02  # weight of the production cost in the objective (see the module docstring)


def effective_limits(pre: dict[str, np.ndarray], rate: np.ndarray, current_based: np.ndarray):
    """Active-power limits per branch for one hour, with Q and V held (1-D arrays)."""
    v = np.minimum(pre["v_i"], pre["v_j"])
    q = np.maximum(np.abs(pre["q_i"]), np.abs(pre["q_j"]))
    scale = np.where(current_based, v, 1.0)
    return np.sqrt(np.maximum((rate * scale) ** 2 - q**2, 0.0))


@dataclass
class RedispatchResult:
    success: bool
    up: np.ndarray  # MW per unit
    down: np.ndarray
    cost_eur: float  # change in production cost under the cost assumptions
    premium_eur: float  # handling premium on every MW moved up or down
    volume_mw: float  # sum of upward moves
    unresolved_mw: float  # overload left (sum of slacks)
    constraints: int  # rows in the final program
    binding: list[str]  # "branch | outage" of binding limits
    links: np.ndarray  # HVDC set-points, MW from the first bus to the second
    uncontrollable_mw: float  # intact-network overload on branches nothing can relieve


def redispatch(
    pre: dict[str, np.ndarray],
    dc: DcModel,
    table: pd.DataFrame,
    units: pd.DataFrame,
    dispatch: np.ndarray,
    outages: list[str],
    shift: np.ndarray | None = None,
    n1: bool = True,
    links: list[tuple[int, int, float]] | None = None,
    max_rounds: int = 10,
    ac_states: dict[str, np.ndarray] | None = None,
) -> RedispatchResult:
    """Cheapest-volume redispatch of one hour that respects N-0 and, if ``n1``, N-1 limits.

    ``pre`` holds one hour's AC flows (1-D arrays over ``table``'s branches, as
    ``n44.annual.pre_contingency_flows`` row by row); ``units`` is
    ``n44.opf.machine_table`` and ``dispatch`` the current output of each unit (MW).

    ``links`` are controllable point-to-point HVDC links (from bus, to bus, capacity MW),
    idle in ``pre``. Their set-points are free variables of the program (lossless here), and
    the trip of each link is one more contingency: with the link gone, its flow is back on
    the AC network.

    ``ac_states`` re-linearises the program around solved AC states. Its arrays ``flow`` and
    ``limit`` (intact state, then one per outage, x branches) hold each state's AC active flow
    and active-power limit; where they are finite they replace the LODF estimates. Used to
    correct a redispatch whose AC check still shows overloads.
    """
    costs = cost_assumptions()
    premium, penalty = costs["redispatch_premium"], costs["overload_penalty"]
    names = list(table.index)
    n_br = len(names)
    live = units.in_service.to_numpy()
    n_units = len(units)
    links = links or []
    n_links = len(links)
    ptdf = dc.ptdf.loc[names]
    sens = ptdf[units.bus].to_numpy() * live[None, :]  # branches x units
    hvdc = (
        np.column_stack([ptdf[b].to_numpy() - ptdf[a].to_numpy() for a, b, _ in links])
        if links
        else np.zeros((n_br, 0))
    )
    lodf = dc.lodf.loc[names, names].to_numpy()
    out_pos = np.array([names.index(o) for o in outages], dtype=int)
    current = (table.basis == "current").to_numpy()
    lim_a = effective_limits(pre, table.rate_a.to_numpy(), current)
    lim_b = effective_limits(pre, table.rate_b.to_numpy(), current)
    flow = (pre["p_i"] - pre["p_j"]) / 2

    # States: 0 intact, then one per outage, then one per link trip. For each state the base
    # flow (branches), the sensitivities to the units and links, the limit and the branches
    # that can be overloaded (not the outaged one).
    post = flow[:, None] + lodf[:, out_pos] * flow[out_pos][None, :]
    if shift is not None:
        post = post + shift
    base = np.c_[flow, post, np.repeat(flow[:, None], n_links, 1)].T  # (states, branches)
    unit_sens = np.concatenate(
        [
            sens[None],
            sens[None] + lodf[:, out_pos].T[:, :, None] * sens[out_pos][:, None, :],
            np.repeat(sens[None], n_links, 0),
        ]
    )  # (states, branches, units)
    link_sens = np.concatenate(
        [
            hvdc[None],
            hvdc[None] + lodf[:, out_pos].T[:, :, None] * hvdc[out_pos][:, None, :],
            np.repeat(hvdc[None], n_links, 0),
        ]
    )  # (states, branches, links)
    for j in range(n_links):  # a tripped link carries nothing
        link_sens[1 + len(outages) + j, :, j] = 0.0
    limit = np.r_[lim_a[None], np.repeat(lim_b[None], len(outages) + n_links, 0)]
    if ac_states is not None:
        n_ac = ac_states["flow"].shape[0]
        known = np.isfinite(ac_states["flow"]) & np.isfinite(ac_states["limit"])
        base[:n_ac] = np.where(known, ac_states["flow"], base[:n_ac])
        limit[:n_ac] = np.where(known, ac_states["limit"], limit[:n_ac])
    valid = np.ones_like(base, bool)
    valid[1 + np.arange(len(outages)), out_pos] = False
    # a flow no unit or link can change (a radial connection carrying a fixed exchange) is
    # not a constraint of the redispatch; its overload is reported as uncontrollable
    movable = (np.abs(unit_sens).max(axis=2) > 1e-9) | (
        np.abs(link_sens).max(axis=2, initial=0.0) > 1e-9
    )
    uncontrollable = float(np.clip(np.abs(base[0]) - limit[0], 0, None)[~movable[0]].sum())
    valid &= movable
    labels_state = ["intact", *outages, *[f"trip of HVDC {a}-{b}" for a, b, _ in links]]

    c = units.cost.to_numpy()
    objective = np.r_[premium + TIE_BREAK * c, premium - TIE_BREAK * c, np.full(n_links, 0.1)]
    bounds = [(0.0, max(hi - p, 0.0)) for hi, p in zip(units.p_max_mw, dispatch, strict=True)]
    bounds += [(0.0, max(p - lo, 0.0)) for lo, p in zip(units.p_min_mw, dispatch, strict=True)]
    for j in np.flatnonzero(~live):
        bounds[j] = bounds[n_units + j] = (0.0, 0.0)
    bounds += [(-cap, cap) for _, _, cap in links]
    n_vars = 2 * n_units + n_links

    rows = {(0, m) for m in range(n_br) if movable[0, m]}
    if n1:  # start with the pairs already beyond 80 % of their limit
        start = (np.abs(base) > 0.8 * limit) & valid
        start[0] = False
        rows |= {(int(st), int(m)) for st, m in zip(*np.nonzero(start), strict=True)}

    for _ in range(max_rounds):
        ordered = sorted(rows)
        a_rows, b_rows, labels = [], [], []
        for st, m in ordered:
            coef = np.r_[unit_sens[st, m], -unit_sens[st, m], link_sens[st, m]]
            for sign in (1.0, -1.0):
                a_rows.append(sign * coef)
                b_rows.append(limit[st, m] - sign * base[st, m])
                labels.append(f"{names[m]} | {labels_state[st]}")
        a = np.asarray(a_rows)
        n_rows = len(a)
        result = linprog(
            np.r_[objective, np.full(n_rows, penalty)],
            A_ub=np.c_[a, -np.eye(n_rows)],  # x = [up, down, links, slack]
            b_ub=np.asarray(b_rows),
            A_eq=np.r_[np.ones(n_units), -np.ones(n_units), np.zeros(n_links + n_rows)][None, :],
            b_eq=[0.0],
            bounds=bounds + [(0.0, None)] * n_rows,
            method="highs",
        )
        if not result.success:
            empty = np.zeros(n_units)
            return RedispatchResult(
                False,
                empty,
                empty,
                np.nan,
                np.nan,
                np.nan,
                np.nan,
                n_rows,
                [],
                np.zeros(n_links),
                np.nan,
            )
        up, down = result.x[:n_units], result.x[n_units : 2 * n_units]
        set_points = result.x[2 * n_units : n_vars]
        if not n1:
            break
        after = base + unit_sens @ (up - down) + link_sens @ set_points
        violated = (np.abs(after) > limit + 1e-6) & valid
        new = {(int(st), int(m)) for st, m in zip(*np.nonzero(violated), strict=True)} - rows
        if not new:
            break
        rows |= new
    slack = result.x[n_vars:]
    binding = sorted({labels[r] for r in np.flatnonzero(np.abs(result.ineqlin.marginals) > 1e-6)})
    return RedispatchResult(
        success=True,
        up=up,
        down=down,
        cost_eur=float(c @ (up - down)),
        premium_eur=float(premium * (up.sum() + down.sum())),
        volume_mw=float(up.sum()),
        unresolved_mw=float(slack.sum()),
        constraints=n_rows,
        binding=binding,
        links=set_points,
        uncontrollable_mw=uncontrollable,
    )
