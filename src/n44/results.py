"""pandapower results in the conventions of the reference model (``n44.ybus``).

Branch flows are terminal flows in PSS/E orientation (bus I to bus J). A line's flow at each
end includes the shunt of that end, and loading follows PSS/E's rating units: current for
lines (NXFRAT > 0), MVA for transformers (XFRRAT <= 0).
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from n44.io.build_net import line_end_shunts


def _end_shunt_power(net, lines: pd.Index, end: str) -> np.ndarray:
    """Complex power drawn by the shunt at one end of each line (0 where there is none)."""
    shunts = line_end_shunts(net)
    shunts = shunts[shunts.parent_end == end]
    res = net.res_shunt.loc[shunts.index]
    power = pd.Series(
        res.p_mw.to_numpy() + 1j * res.q_mvar.to_numpy(),
        index=shunts.parent_line.astype(int).to_numpy(),
    )
    return power.groupby(level=0).sum().reindex(lines, fill_value=0).to_numpy()


def branch_flows(net) -> pd.DataFrame:
    """In-service branch flows with the columns of ``n44.ybus.branch_flows``."""
    vm = net.res_bus.vm_pu
    line = net.line[net.line.in_service]
    res = net.res_line.loc[line.index]
    si = res.p_from_mw.to_numpy() + 1j * res.q_from_mvar.to_numpy()
    sj = res.p_to_mw.to_numpy() + 1j * res.q_to_mvar.to_numpy()
    si = si + _end_shunt_power(net, line.index, "from")
    sj = sj + _end_shunt_power(net, line.index, "to")
    vi, vj = vm[line.from_bus].to_numpy(), vm[line.to_bus].to_numpy()
    rate = line.rate_a_mva.to_numpy()
    with np.errstate(divide="ignore", invalid="ignore"):
        loading = np.maximum(np.abs(si) / vi, np.abs(sj) / vj) / rate * 100
    lines = pd.DataFrame(
        {
            "kind": "line",
            "i": line.psse_i,
            "j": line.psse_j,
            "ckt": line.psse_ckt,
            "metered": line.psse_metered,
            "p_i_mw": si.real,
            "q_i_mvar": si.imag,
            "p_j_mw": sj.real,
            "q_j_mvar": sj.imag,
            "loading_a_pct": np.where(rate > 0, loading, np.nan),
        }
    )

    trafo = net.trafo[net.trafo.in_service]
    res = net.res_trafo.loc[trafo.index]
    s_hv = res.p_hv_mw.to_numpy() + 1j * res.q_hv_mvar.to_numpy()
    s_lv = res.p_lv_mw.to_numpy() + 1j * res.q_lv_mvar.to_numpy()
    w1 = trafo.winding1_is_hv.to_numpy()
    si, sj = np.where(w1, s_hv, s_lv), np.where(w1, s_lv, s_hv)
    rate = trafo.rate_a_mva.to_numpy()
    with np.errstate(divide="ignore", invalid="ignore"):
        loading = np.maximum(np.abs(si), np.abs(sj)) / rate * 100
    trafos = pd.DataFrame(
        {
            "kind": "transformer",
            "i": trafo.psse_i,
            "j": trafo.psse_j,
            "ckt": trafo.psse_ckt,
            "metered": trafo.psse_metered,
            "p_i_mw": si.real,
            "q_i_mvar": si.imag,
            "p_j_mw": sj.real,
            "q_j_mvar": sj.imag,
            "loading_a_pct": np.where(rate > 0, loading, np.nan),
        }
    )
    flows = pd.concat([lines, trafos], ignore_index=True)
    flows.insert(9, "loss_mw", flows.p_i_mw + flows.p_j_mw)
    return flows


def plant_reactive_power(net) -> pd.Series:
    """Reactive output per generator bus (all machines of a plant together), in Mvar."""
    gen = net.res_gen.q_mvar.groupby(net.gen.bus).sum()
    ext = net.res_ext_grid.q_mvar.groupby(net.ext_grid.bus).sum()
    return pd.concat([gen, ext]).groupby(level=0).sum()
