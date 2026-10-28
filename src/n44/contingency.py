"""N-1 security: contingencies, PTDF/LODF, screening, AC contingency runs, transfer margins.

**Contingencies.** The outage of each line and transformer. A branch whose outage isolates a
bus (a single-circuit bridge of the network graph) is left out. In N44 those are the radial
connections of the eight HVDC terminal buses: losing one means losing the link's power, a
frequency question rather than a flow one.

**Monitoring.** Loading in PSS/E's convention (``n44.results.branch_flows``): terminal current
for lines, MVA for transformers. The intact network is checked against RATE A and
post-contingency states against RATE B, the short-term emergency rating.

**DC sensitivities.** PTDF and LODF come from pandapower's DC model (``makePTDF``,
``makeLODF``). They are mapped from pandapower's internal branch order to the branch names
of the annual results ("line 3000-3115-1") and oriented from bus I to bus J.

**Screening.** Each hour's post-contingency flows are estimated from its AC flows plus the
LODF change in active power, holding each end's reactive flow and voltage at their
pre-contingency values ("AC base, DC delta").

**AC contingency runs.** Each outage is solved from the pre-contingency voltages, and a line
goes out together with its end shunts. ``run_n1`` does it with pandapower
(``set_branch_status``; pandapower's own ``run_contingency`` would leave the shunts in), at
about 0.1 s per outage. ``ac_n1_sweep`` does it with the reference-model solver of
``n44.acpf`` in about 0.5 ms, which makes a full AC N-1 of every hour of the year possible;
the two agree to the solvers' tolerance.

**Transfer margins.** The extra transfer across a cut that the network can take before some
element exceeds its limit, in the intact network (RATE A) or after any contingency (RATE B).
Computed with PTDF/LODF, as in flow-based capacity calculation.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, replace

import networkx as nx
import numpy as np
import pandapower as pp
import pandas as pd
from pandapower.pypower.idx_brch import F_BUS, T_BUS
from pandapower.pypower.makeLODF import makeLODF
from pandapower.pypower.makePTDF import makePTDF

from n44 import acpf
from n44.config import load_config
from n44.io.build_net import line_end_shunts, set_branch_status
from n44.powerflow import _q_limit_report, ac_options
from n44.results import branch_flows

PTDF_THRESHOLD = 0.05  # critical elements of a transfer: |sensitivity| of at least 5 %


def branch_table(net) -> pd.DataFrame:
    """Lines and transformers with their names, PSS/E keys, ratings and rating basis.

    Indexed by name ("line 3000-3115-1", "transformer 5101-5100-1"), in pandapower's
    internal branch order: lines first, then transformers, each in table order.
    """
    parts = []
    for element, kind, table in (("line", "line", net.line), ("trafo", "transformer", net.trafo)):
        parts.append(
            pd.DataFrame(
                {
                    "element": element,
                    "index": table.index,
                    "kind": kind,
                    "i": table.psse_i.to_numpy(),
                    "j": table.psse_j.to_numpy(),
                    "ckt": table.psse_ckt.to_numpy(),
                    "rate_a": table.rate_a_mva.to_numpy(),
                    "rate_b": table.rate_b_mva.to_numpy(),
                    "basis": "current" if kind == "line" else "power",
                }
            )
        )
    out = pd.concat(parts, ignore_index=True)
    out.index = [
        f"{k} {i}-{j}-{c}" for k, i, j, c in zip(out.kind, out.i, out.j, out.ckt, strict=True)
    ]
    out.index.name = "branch"
    return out


def radial_branches(table: pd.DataFrame) -> list[str]:
    """Branches whose outage splits the network: bridges with a single circuit."""
    graph = nx.MultiGraph()
    graph.add_edges_from(zip(table.i, table.j, strict=True))
    bridges = {frozenset(edge) for edge in nx.bridges(nx.Graph(graph))}
    return [
        name
        for name, i, j in zip(table.index, table.i, table.j, strict=True)
        if frozenset((i, j)) in bridges and graph.number_of_edges(i, j) == 1
    ]


@dataclass
class DcModel:
    """PTDF (branch x bus, MW per MW injected against the slack) and LODF (monitored x outaged).

    Flows are oriented from PSS/E bus I to bus J. LODF columns of radial branches are not
    defined (the outage splits the network) and are NaN.
    """

    ptdf: pd.DataFrame
    lodf: pd.DataFrame
    slack: int


def dc_model(net) -> DcModel:
    """PTDF and LODF of the net's DC model, mapped to branch names and PSS/E bus numbers."""
    work = copy.deepcopy(net)
    pp.rundcpp(work, calculate_voltage_angles=True)
    ppc, lookup = work._ppc, work._pd2ppc_lookups
    table = branch_table(work)
    rows = np.r_[
        np.arange(*lookup["branch"]["line"])[: len(work.line)],
        np.arange(*lookup["branch"]["trafo"])[: len(work.trafo)],
    ]
    bus_row = pd.Series(lookup["bus"][work.bus.index], index=work.bus.index)
    branch = ppc["branch"][rows]
    # ppc runs lines from_bus -> to_bus and transformers hv -> lv; PSS/E runs I -> J
    from_bus = np.r_[work.line.from_bus.to_numpy(), work.trafo.hv_bus.to_numpy()]
    to_bus = np.r_[work.line.to_bus.to_numpy(), work.trafo.lv_bus.to_numpy()]
    assert (branch[:, F_BUS].real == bus_row[from_bus].to_numpy()).all()
    assert (branch[:, T_BUS].real == bus_row[to_bus].to_numpy()).all()
    sign = np.where(from_bus == table.i.to_numpy(), 1.0, -1.0)
    assert (np.where(sign > 0, to_bus, from_bus) == table.j.to_numpy()).all()

    slack_bus = int(work.ext_grid.bus.iloc[0])
    ptdf = makePTDF(ppc["baseMVA"], ppc["bus"], ppc["branch"], int(bus_row[slack_bus]))
    lodf = makeLODF(ppc["branch"], ptdf)[np.ix_(rows, rows)]
    ptdf = ptdf[rows][:, bus_row.to_numpy()]
    ptdf = pd.DataFrame(sign[:, None] * ptdf, index=table.index, columns=work.bus.index)
    lodf = sign[:, None] * lodf * sign[None, :]
    lodf = pd.DataFrame(lodf, index=table.index, columns=table.index)
    radial = radial_branches(table)
    lodf[radial] = np.nan
    return DcModel(ptdf=ptdf, lodf=lodf, slack=slack_bus)


def shunt_outage_shift(net, dc: DcModel, outages: list[str]) -> np.ndarray:
    """Active flow change (branches x outages, MW) from the end shunts that trip with a line.

    A line's end shunts go out with it. Their conductance draws P = G·V², so the injections
    change and LODF, which assumes fixed injections, misses it. In N44 this matters for line
    5101-5501, whose end shunts draw +22.3 MW at one end and -21.6 MW at the other. The
    removed consumption acts on the network without the line, so the shift uses the
    post-outage PTDF, PTDF + LODF[:, k] x PTDF[k, :], at 1 p.u. voltage.
    """
    table = branch_table(net)
    shunts = line_end_shunts(net)
    ptdf = dc.ptdf.loc[table.index]
    shift = np.zeros((len(table), len(outages)))
    for k, name in enumerate(outages):
        if table.loc[name, "element"] != "line":
            continue
        ends = shunts[shunts.parent_line == table.loc[name, "index"]]
        lodf = dc.lodf.loc[table.index, name].to_numpy()
        for bus, p_mw in zip(ends.bus, ends.p_mw, strict=True):
            post_outage = ptdf[bus].to_numpy() + lodf * ptdf.loc[name, bus]
            shift[:, k] += post_outage * p_mw
    return shift


def screen_loading(
    pre: dict[str, np.ndarray],
    lodf: np.ndarray,
    outages: np.ndarray,
    rate: np.ndarray,
    current_based: np.ndarray,
    shift: np.ndarray | None = None,
) -> np.ndarray:
    """Post-contingency loading estimated from AC pre-contingency flows and the LODF.

    ``pre`` holds (hours, branches) arrays p_i, p_j, q_i, q_j (MW, Mvar) and v_i, v_j (p.u.),
    in PSS/E orientation; ``outages`` are column positions in ``lodf`` (branches x branches).
    The DC change of each branch's active flow is added at both ends, while reactive flows
    and voltages keep their pre-contingency values. ``shift`` (branches x outages) adds the
    effect of injections that trip with a branch (``shunt_outage_shift``). Returns (hours,
    outages, branches) in % of ``rate``, NaN for the outaged branch itself.
    """
    series = (pre["p_i"] - pre["p_j"]) / 2  # active flow without losses, I -> J
    delta = series[:, outages][:, :, None] * lodf[:, outages].T[None, :, :]
    if shift is not None:
        delta = delta + shift.T[None, :, :]
    p_i = pre["p_i"][:, None, :] + delta
    p_j = pre["p_j"][:, None, :] - delta
    s_i = np.hypot(p_i, pre["q_i"][:, None, :])
    s_j = np.hypot(p_j, pre["q_j"][:, None, :])
    by_current = np.maximum(s_i / pre["v_i"][:, None, :], s_j / pre["v_j"][:, None, :])
    worst = np.where(current_based, by_current, np.maximum(s_i, s_j))
    loading = worst / rate * 100
    loading[:, np.arange(len(outages)), outages] = np.nan
    return loading


def _series_loading(net) -> pd.Series:
    """Loading in PowerFactory's convention: series current of lines, current of transformers.

    Lines exclude their end shunts (separate elements in pandapower); transformers use
    |S|/V against the MVA rating, at the more loaded end (validation log, entry 7).
    """
    line = net.line[net.line.in_service]
    line_pct = net.res_line.loading_percent[line.index]
    trafo = net.trafo[net.trafo.in_service]
    vm = net.res_bus.vm_pu
    s_hv = np.hypot(net.res_trafo.p_hv_mw, net.res_trafo.q_hv_mvar)[trafo.index]
    s_lv = np.hypot(net.res_trafo.p_lv_mw, net.res_trafo.q_lv_mvar)[trafo.index]
    trafo_pct = (
        np.maximum(s_hv / vm[trafo.hv_bus].to_numpy(), s_lv / vm[trafo.lv_bus].to_numpy())
        / trafo.rate_a_mva
        * 100
    )
    names = [
        f"line {i}-{j}-{c}" for i, j, c in zip(line.psse_i, line.psse_j, line.psse_ckt, strict=True)
    ]
    names += [
        f"transformer {i}-{j}-{c}"
        for i, j, c in zip(trafo.psse_i, trafo.psse_j, trafo.psse_ckt, strict=True)
    ]
    return pd.Series(np.r_[line_pct.to_numpy(), trafo_pct.to_numpy()], index=names)


def _solve_from(net, vm: np.ndarray, va: np.ndarray, options: dict, q_lims: bool) -> bool:
    """AC power flow started from given voltages, then from flat; True if one converges."""
    for kwargs in ({"init": "auto", "init_vm_pu": vm, "init_va_degree": va}, {"init": "flat"}):
        try:
            pp.runpp(net, enforce_q_lims=q_lims, **options, **kwargs)
            return True
        except pp.LoadflowNotConverged:
            continue
    return False


def run_n1(net, outages: list[str]) -> tuple[pd.DataFrame, pd.DataFrame]:
    """AC N-1 of a net whose base case is already solved.

    Returns the post-contingency flows of every branch for every outage (long table: outage,
    branch, terminal flows, ``loading_a_pct`` in PSS/E's convention against RATE A, and
    ``loading_series_pct`` in PowerFactory's) and one summary row per outage (converged,
    lowest and highest voltage). The net is left with its base-case results.
    """
    table = branch_table(net)
    vm, va = net.res_bus.vm_pu.to_numpy().copy(), net.res_bus.va_degree.to_numpy().copy()
    options = ac_options()
    q_lims = load_config()["powerflow"]["enforce_q_lims"]
    flows, summary = [], []
    for name in outages:
        element, index = table.loc[name, ["element", "index"]]
        set_branch_status(net, element, index, False)
        converged = _solve_from(net, vm, va, options, q_lims)
        if converged and q_lims and _q_limit_report(net)[1]:
            # pandapower's limit loop does not check that its first, all-PV pass converged;
            # from a diverged pass it can reach a "solution" with plants held at a limit
            # their voltage contradicts (validation log, entry 9). That is no solution.
            converged = False
        row = {"outage": name, "converged": converged}
        if converged:
            post = branch_flows(net)
            post.index = [
                f"{k} {i}-{j}-{c}"
                for k, i, j, c in zip(post.kind, post.i, post.j, post.ckt, strict=True)
            ]
            post["loading_series_pct"] = _series_loading(net)
            post = post[
                ["p_i_mw", "q_i_mvar", "p_j_mw", "q_j_mvar", "loading_a_pct", "loading_series_pct"]
            ]
            flows.append(post.rename_axis("branch").reset_index().assign(outage=name))
            live = net.res_bus.vm_pu[net.bus.in_service]
            row.update(vm_min=live.min(), vm_min_bus=int(live.idxmin()), vm_max=live.max())
        summary.append(row)
        set_branch_status(net, element, index, True)
    if not _solve_from(net, vm, va, options, q_lims):  # restore the base-case results
        raise RuntimeError("base case no longer converges after the contingency runs")
    columns = ["outage", "branch"]
    flows = pd.concat(flows, ignore_index=True)
    flows = flows[columns + [c for c in flows.columns if c not in columns]]
    return flows, pd.DataFrame(summary)


def transfer_shift(
    generation: pd.Series, zone: pd.Series, sending: list[str], receiving: list[str]
) -> pd.Series:
    """Injection change per bus for 1 MW of transfer from one set of zones to another.

    Generation shift keys are proportional to ``generation`` (MW per bus, for instance the
    annual mean): buses of the sending zones go up by 1 MW in total and those of the
    receiving zones down by 1 MW. A balanced shift moves the same flows whatever the slack.
    """
    gen = generation[generation > 0]
    gen_zone = zone.reindex(gen.index)
    shift = pd.Series(0.0, index=zone.index)
    for zones, sign in ((sending, 1.0), (receiving, -1.0)):
        part = gen[gen_zone.isin(zones)]
        shift.loc[part.index] += sign * part / part.sum()
    return shift


def cut_crossing(table: pd.DataFrame, zone: pd.Series, north, south) -> np.ndarray:
    """+1 / -1 for branches crossing a cut north to south / south to north, else 0."""
    zi, zj = table.i.map(zone), table.j.map(zone)
    forward = (zi.isin(north) & zj.isin(south)).to_numpy()
    backward = (zi.isin(south) & zj.isin(north)).to_numpy()
    return forward.astype(float) - backward.astype(float)


def plant_generation(net) -> pd.Series:
    """Active output per bus of a solved net (all machines, the swing bus included)."""
    parts = [
        net.res_gen.p_mw.groupby(net.gen.bus).sum(),
        net.res_ext_grid.p_mw.groupby(net.ext_grid.bus).sum(),
    ]
    return pd.concat(parts).groupby(level=0).sum()


def transfer_sensitivity(
    dc: DcModel, shift: pd.Series, table: pd.DataFrame, outages: list[str]
) -> np.ndarray:
    """Sensitivity of each branch to the transfer, intact and after each outage.

    Returns (branches, 1 + outages), MW of I->J flow per MW of transfer.
    """
    names = list(table.index)
    out_pos = np.array([names.index(o) for o in outages], dtype=int)
    s0 = dc.ptdf.loc[names, shift.index].to_numpy() @ shift.to_numpy()
    lodf = dc.lodf.loc[names, names].to_numpy()
    return np.c_[s0, s0[:, None] + lodf[:, out_pos] * s0[out_pos][None, :]]


def transfer_margins(
    pre: dict[str, np.ndarray],
    dc: DcModel,
    shift: pd.Series,
    table: pd.DataFrame,
    outages: list[str],
    threshold: float = PTDF_THRESHOLD,
    outage_shift: np.ndarray | None = None,
) -> pd.DataFrame:
    """Extra transfer (MW) before the first limit, in N-0 (RATE A) and N-1 (RATE B), per hour.

    A post-contingency flow is the AC pre-contingency flow plus the LODF change; the transfer
    adds sensitivity x T to it. Only elements whose sensitivity to the transfer is at least
    ``threshold`` in that state count, as for critical network elements in flow-based
    capacity calculation. Limits are on active power, with each end's reactive flow and
    voltage held at their pre-contingency values. A negative margin means a critical
    element is already beyond its limit. ``outage_shift`` (branches x outages) adds the
    injections that trip with a branch (``shunt_outage_shift``). Also returns the limiting
    element and outage.
    """
    names = list(table.index)
    out_pos = np.array([names.index(o) for o in outages], dtype=int)
    lodf = dc.lodf.loc[names, names].to_numpy()
    # sensitivity and active flow in each state: column 0 intact, then one per outage
    sens = transfer_sensitivity(dc, shift, table, outages)  # (br, 1+out)
    series = (pre["p_i"] - pre["p_j"]) / 2  # (hours, br)
    flow = np.concatenate(
        [
            series[:, :, None],
            series[:, :, None] + lodf[None, :, out_pos] * series[:, None, out_pos],
        ],
        axis=2,
    )  # (hours, br, 1+out)
    if outage_shift is not None:
        flow[:, :, 1:] += outage_shift[None, :, :]
    rate = np.c_[
        table.rate_a.to_numpy(), np.repeat(table.rate_b.to_numpy()[:, None], len(outages), 1)
    ]
    v = np.minimum(pre["v_i"], pre["v_j"])
    q = np.maximum(np.abs(pre["q_i"]), np.abs(pre["q_j"]))
    scale = np.where(table.basis.to_numpy() == "current", v, 1.0)  # (hours, br)
    limit = np.sqrt(np.maximum((rate[None] * scale[:, :, None]) ** 2 - q[:, :, None] ** 2, 0))
    with np.errstate(divide="ignore", invalid="ignore"):
        room = np.where(sens[None] > 0, limit - flow, limit + flow) / np.abs(sens[None])
    critical = np.abs(sens) >= threshold
    critical[out_pos, np.arange(1, len(outages) + 1)] = False  # an outaged branch carries nothing
    room = np.where(critical[None], room, np.inf)
    states = ["intact", *outages]
    out = {}
    for label, cols in (("n0", slice(0, 1)), ("n1", slice(1, None))):
        block = room[:, :, cols]
        if block.shape[2] == 0:
            continue
        flat = block.reshape(len(block), -1)
        pos = np.nanargmin(flat, axis=1)
        out[f"margin_{label}_mw"] = flat[np.arange(len(flat)), pos]
        br, st = np.unravel_index(pos, block.shape[1:])
        out[f"limiting_{label}"] = np.array(names)[br]
        if label == "n1":
            out["limiting_outage"] = np.array(states[1:])[st]
    return pd.DataFrame(out)


def ac_n1_sweep(grid: acpf.Grid, outages: list[int], max_iter: int = 20) -> dict[str, np.ndarray]:
    """AC N-1 of one hour with the reference-model solver (``n44.acpf``).

    ``outages`` are branch positions in ``grid.names``. Returns the base-case loading
    (branches), the post-contingency loading (outages x branches, % of RATE A, NaN for the
    outaged branch and for outages that do not converge), the lowest post-contingency voltage
    and its bus, and a convergence flag per outage. An outage is solved from the base-case
    voltages, then from a flat start if that fails.
    """
    base = acpf.solve(grid)
    if not base.converged:
        raise RuntimeError("base case does not converge")
    n_out, n_br = len(outages), len(grid.names)
    out = {
        "base_loading": acpf.branch_flows(grid, base.v)[2],
        "loading": np.full((n_out, n_br), np.nan),
        "vm_min": np.full(n_out, np.nan),
        "vm_min_bus": np.full(n_out, -1),
        "converged": np.zeros(n_out, bool),
    }
    for row, k in enumerate(outages):
        sol = acpf.solve(grid, outage=k, v0=base.v, max_iter=max_iter)
        if not sol.converged:
            sol = acpf.solve(grid, outage=k, max_iter=max_iter)
        if not sol.converged:
            continue
        vm = np.abs(sol.v)
        out["loading"][row] = acpf.branch_flows(grid, sol.v, outage=k)[2]
        out["vm_min"][row] = vm.min()
        out["vm_min_bus"][row] = grid.model.buses[vm.argmin()]
        out["converged"][row] = True
    return out


def ac_transfer_margin(
    grid: acpf.Grid,
    shift: pd.Series,
    outages: list[int],
    critical: np.ndarray,
    rate_ratio: np.ndarray,
    span: float = 6000.0,
    step: float = 250.0,
    tolerance: float = 10.0,
) -> dict[str, float]:
    """Transfer (MW) at which the first AC limit is reached, intact (N-0) and N-1.

    The transfer adds ``shift`` x T to the bus injections; the swing bus balances losses.
    Monitored are the ``critical`` elements of each state (branches x (1 + outages), as in
    ``transfer_margins``): N-0 against RATE A, N-1 against RATE B (``rate_ratio`` = RATE A /
    RATE B per branch). A state without AC solution counts as a violation.

    From T = 0 the search walks up in growing steps to the first insecure transfer and then
    bisects to ``tolerance``. If T = 0 is already insecure it walks down in ``step``s for a
    secure point; if there is none within -2,000 MW the margin is NaN (the hour is insecure
    for reasons the transfer does not control).
    """
    injection = shift.reindex(grid.model.buses).fillna(0.0).to_numpy() / grid.model.sbase

    def secure(transfer: float, n1: bool) -> bool:
        moved = replace(grid, s_spec=grid.s_spec + injection * transfer)
        base = acpf.solve(moved)
        if not base.converged:
            return False
        loading = acpf.branch_flows(moved, base.v)[2]
        if np.any(loading[critical[:, 0]] > 100):
            return False
        if not n1:
            return True
        for column, k in enumerate(outages, start=1):
            watch = critical[:, column]
            if not watch.any():
                continue
            sol = acpf.solve(moved, outage=k, v0=base.v, max_iter=20)
            if not sol.converged:
                return False
            if np.any(
                acpf.branch_flows(moved, sol.v, outage=k)[2][watch] * rate_ratio[watch] > 100
            ):
                return False
        return True

    out = {}
    for label, n1 in (("n0", False), ("n1", True)):
        if secure(0.0, n1):
            low, high, jump = 0.0, None, step
            while low + jump <= span:
                if secure(low + jump, n1):
                    low, jump = low + jump, jump * 2
                else:
                    high = low + jump
                    break
            if high is None:
                out[f"ac_margin_{label}_mw"] = np.inf
                continue
        else:
            high, low = 0.0, None
            for t in np.arange(-step, -2000.0 - 1e-9, -step):
                if secure(float(t), n1):
                    low = float(t)
                    break
                high = float(t)
            if low is None:
                out[f"ac_margin_{label}_mw"] = np.nan
                continue
        while high - low > tolerance:
            mid = (low + high) / 2
            low, high = (mid, high) if secure(mid, n1) else (low, mid)
        out[f"ac_margin_{label}_mw"] = low
    return out
