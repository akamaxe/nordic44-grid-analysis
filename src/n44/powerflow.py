"""Power flow runs with the project's options (``powerflow`` in config/base.yaml)."""

from __future__ import annotations

import copy
from dataclasses import dataclass, field

import numpy as np
import pandapower as pp
import pandas as pd

from n44.config import load_config

Q_LIMIT_TOLERANCE_MVAR = 1e-3


def ac_options(**overrides) -> dict:
    """Keyword arguments for ``pandapower.runpp`` from the configuration."""
    cfg = load_config()["powerflow"]
    options = {
        key: cfg[key]
        for key in (
            "algorithm",
            "tolerance_mva",
            "max_iteration",
            "calculate_voltage_angles",
            "trafo_model",
            "trafo_loading",
        )
    }
    options.update(overrides)
    return options


@dataclass
class AcResult:
    converged: bool
    init: str | None = None  # initialisation that converged
    q_limited: list[int] = field(default_factory=list)  # plant buses held at a Q limit
    q_inconsistent: list[int] = field(default_factory=list)


def _plant_limits(net):
    gen = net.gen[net.gen.in_service]
    q = net.res_gen.q_mvar[gen.index].groupby(gen.bus).sum()
    return q, gen.max_q_mvar.groupby(gen.bus).sum(), gen.min_q_mvar.groupby(gen.bus).sum()


def _q_limit_report(net) -> tuple[list[int], list[int]]:
    """Plants at a reactive limit, and those whose voltage contradicts the limit.

    A plant held at Qmax should sit below its voltage setpoint, and one at Qmin above it. If
    not, a solver that can switch plants back to voltage control would have done so.
    """
    q, qmax, qmin = _plant_limits(net)
    at_max = q >= qmax - Q_LIMIT_TOLERANCE_MVAR
    at_min = q <= qmin + Q_LIMIT_TOLERANCE_MVAR
    limited = q.index[at_max | at_min]
    vs = net.gen[net.gen.in_service].groupby("bus").vm_pu.first()
    vm = net.res_bus.vm_pu
    wrong = [
        b
        for b in limited
        if (at_max[b] and vm[b] > vs[b] + 1e-9) or (at_min[b] and vm[b] < vs[b] - 1e-9)
    ]
    return [int(b) for b in limited], [int(b) for b in wrong]


def _solve(net, init: str, enforce_q_lims: bool, options: dict, **init_kwargs) -> None:
    """Newton-Raphson, with reactive limits if requested.

    pandapower's limit loop first converges with every plant on voltage control. It then
    switches all plants beyond a limit to PQ at once, never switches them back, and solves
    again. That makes it the same as solving without limits and then enforcing them from that
    solution: on 200 random hours the two give identical voltages (within 1e-12) and the same
    limited plants.
    """
    pp.runpp(net, init=init, enforce_q_lims=enforce_q_lims, **options, **init_kwargs)


def run_ac(net, enforce_q_lims: bool | None = None, fallback: bool = True) -> AcResult:
    """AC power flow from a flat start, with fallbacks if it does not converge.

    The fallbacks, in order: a DC initialisation, then a start from the PSS/E voltages stored
    in ``net.bus`` (``psse_vm_pu``, ``psse_va_degree``). If none converges the result says
    so, and the net's results are not valid.
    """
    if enforce_q_lims is None:
        enforce_q_lims = load_config()["powerflow"]["enforce_q_lims"]
    options = ac_options()
    attempts = [("flat", {})]
    if fallback:
        attempts.append(("dc", {}))
        if "psse_vm_pu" in net.bus:
            attempts.append(
                (
                    "psse",
                    {
                        "init_vm_pu": net.bus.psse_vm_pu.to_numpy(),
                        "init_va_degree": net.bus.psse_va_degree.to_numpy(),
                    },
                )
            )
    for name, kwargs in attempts:
        init = "auto" if name == "psse" else name
        try:
            _solve(net, init, enforce_q_lims, options, **kwargs)
        except pp.LoadflowNotConverged:
            continue
        limited, wrong = _q_limit_report(net) if enforce_q_lims else ([], [])
        return AcResult(True, name, limited, wrong)
    return AcResult(False)


def run_dc(net) -> None:
    """DC power flow (lossless, flat voltages)."""
    pp.rundcpp(net, calculate_voltage_angles=True)


def dc_branch_flows(net) -> pd.DataFrame:
    """Active flows of a DC solution at the bus-I end, in PSS/E orientation (MW).

    Same row order as ``n44.results.branch_flows`` (in-service lines, then transformers).
    """
    line = net.line[net.line.in_service]
    trafo = net.trafo[net.trafo.in_service]
    p_hv = net.res_trafo.p_hv_mw[trafo.index].to_numpy()
    lines = pd.DataFrame(
        {
            "kind": "line",
            "i": line.psse_i,
            "j": line.psse_j,
            "ckt": line.psse_ckt,
            "p_i_mw": net.res_line.p_from_mw[line.index].to_numpy(),
        }
    )
    trafos = pd.DataFrame(
        {
            "kind": "transformer",
            "i": trafo.psse_i,
            "j": trafo.psse_j,
            "ckt": trafo.psse_ckt,
            "p_i_mw": np.where(trafo.winding1_is_hv, p_hv, -p_hv),
        }
    )
    return pd.concat([lines, trafos], ignore_index=True)


def loss_compensated_dc(net, ac_flows: pd.DataFrame):
    """DC power flow with the AC branch losses added as loads, half at each branch end.

    A plain DC power flow is lossless, so the slack bus picks up all the losses (several
    hundred MW in N44). That shifts flows around the slack and inflates DC errors there. Here
    a copy of ``net`` gets each branch's AC loss (from ``ac_flows``, terminal flows of
    ``n44.results.branch_flows``) as extra load before solving.
    """
    dc = copy.deepcopy(net)
    share = pd.concat([ac_flows.set_index("i").loss_mw, ac_flows.set_index("j").loss_mw]) / 2
    share = share.groupby(level=0).sum()
    pp.create_loads(
        dc,
        buses=share.index.to_numpy(),
        p_mw=share.to_numpy(),
        name=[f"loss {b}" for b in share.index],
    )
    dc.load.loc[dc.load.name.str.startswith("loss "), "kind"] = "loss_compensation"
    run_dc(dc)
    return dc


def losses_mw(net) -> float:
    """Active losses of all branches, line-end shunts included, in MW."""
    total = net.res_line.pl_mw.sum() + net.res_trafo.pl_mw.sum()
    if "parent_line" in net.shunt:
        ends = net.shunt.index[net.shunt.parent_line.notna() & net.shunt.in_service]
        total += net.res_shunt.p_mw[ends].sum()
    return float(total)


def voltage_deviation(net) -> tuple[float, float]:
    """Largest |dVm| (pu) and |dVa| (degrees) against the PSS/E voltages in ``net.bus``."""
    live = net.bus.index[net.bus.in_service]
    dvm = np.abs(net.res_bus.vm_pu[live] - net.bus.psse_vm_pu[live]).max()
    dva = np.abs(net.res_bus.va_degree[live] - net.bus.psse_va_degree[live]).max()
    return float(dvm), float(dva)
