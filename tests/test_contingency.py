"""N-1 tools: radial branches, PTDF/LODF, screening, the fast AC solver and transfer margins."""

import copy

import numpy as np
import pandas as pd
import pytest

from n44.acpf import branch_flows as fast_branch_flows
from n44.acpf import grid_from_case, solve
from n44.contingency import (
    branch_table,
    dc_model,
    plant_generation,
    radial_branches,
    run_n1,
    screen_loading,
    shunt_outage_shift,
    transfer_margins,
    transfer_shift,
)
from n44.io.build_net import build_net, set_branch_status
from n44.io.psse_parser import read_raw
from n44.powerflow import dc_branch_flows, run_ac, run_dc
from n44.results import branch_flows
from tests.conftest import FIXTURES

OUTAGES = ["line 5101-5501-1", "transformer 5400-5401-1", "line 3100-3200-1", "line 3249-7100-1"]


@pytest.fixture(scope="module")
def hour():
    case = read_raw(FIXTURES / "N44_20150325_h18_after_PF.raw")
    net = build_net(case)
    assert run_ac(net).converged
    return case, net, branch_table(net), dc_model(net)


def _dc_flows(net) -> pd.Series:
    work = copy.deepcopy(net)
    run_dc(work)
    f = dc_branch_flows(work)
    f.index = [f"{k} {i}-{j}-{c}" for k, i, j, c in zip(f.kind, f.i, f.j, f.ckt, strict=True)]
    return f.p_i_mw


def _pre(net, table) -> dict[str, np.ndarray]:
    flows = branch_flows(net)
    flows.index = [
        f"{k} {i}-{j}-{c}"
        for k, i, j, c in zip(flows.kind, flows.i, flows.j, flows.ckt, strict=True)
    ]
    flows = flows.loc[table.index]
    vm = net.res_bus.vm_pu
    pre = {
        key: flows[col].to_numpy()[None, :]
        for key, col in (
            ("p_i", "p_i_mw"),
            ("p_j", "p_j_mw"),
            ("q_i", "q_i_mvar"),
            ("q_j", "q_j_mvar"),
        )
    }
    pre["v_i"] = vm[table.i].to_numpy()[None, :]
    pre["v_j"] = vm[table.j].to_numpy()[None, :]
    return pre


def test_radial_branches_are_the_hvdc_connections(hour):
    _, _, table, _ = hour
    assert sorted(radial_branches(table)) == [
        "line 3000-3020-1",
        "line 5600-5620-1",
        "line 5603-5610-1",
        "line 7000-7010-1",
        "line 7000-7020-1",
        "line 8500-8600-1",
        "line 8500-8700-1",
        "transformer 3359-3360-1",
    ]


@pytest.mark.parametrize("name", OUTAGES)
def test_lodf_is_exact_in_dc(hour, name):
    """With only the branch switched (end shunts kept), LODF reproduces a DC outage."""
    _, net, table, dc = hour
    base = _dc_flows(net)
    work = copy.deepcopy(net)
    work[table.loc[name, "element"]].loc[table.loc[name, "index"], "in_service"] = False
    post = _dc_flows(work)
    predicted = (base + dc.lodf[name] * base[name]).drop(name)
    assert np.abs(post - predicted.loc[post.index]).max() < 1e-8


def test_ptdf_is_exact_in_dc(hour):
    _, net, _, dc = hour
    base = _dc_flows(net)
    work = copy.deepcopy(net)
    work.load.loc[work.load.index[work.load.bus == 7000][0], "p_mw"] -= 100.0  # +100 MW at 7000
    assert np.abs(_dc_flows(work) - (base + 100.0 * dc.ptdf[7000])).max() < 1e-8


def test_end_shunts_tripping_with_their_line(hour):
    """Line 5101-5501 trips with its end shunts (+22.3 MW / -21.6 MW): LODF plus the shift."""
    _, net, table, dc = hour
    name = "line 5101-5501-1"
    base = _dc_flows(net)
    work = copy.deepcopy(net)
    set_branch_status(work, "line", table.loc[name, "index"], False)
    post = _dc_flows(work)
    shift = shunt_outage_shift(net, dc, [name])[:, 0]
    predicted = base + dc.lodf[name] * base[name] + pd.Series(shift, index=table.index)
    error = np.abs(post - predicted.loc[post.index]).max()
    assert error < 1e-8
    assert np.abs(post - (base + dc.lodf[name] * base[name]).loc[post.index]).max() > 10


def test_fast_solver_matches_pandapower(hour):
    case, net, table, _ = hour
    grid = grid_from_case(case)
    base = solve(grid)
    assert base.converged
    vm = net.res_bus.vm_pu.loc[grid.model.buses].to_numpy()
    va = np.deg2rad(net.res_bus.va_degree.loc[grid.model.buses].to_numpy())
    assert np.abs(base.v - vm * np.exp(1j * va)).max() < 1e-9
    flows, summary = run_n1(net, OUTAGES)
    assert summary.converged.all()
    for name in OUTAGES:
        k = grid.names.index(name)
        sol = solve(grid, outage=k, v0=base.v)
        fast = pd.Series(fast_branch_flows(grid, sol.v, outage=k)[2], index=grid.names)
        slow = flows[flows.outage == name].set_index("branch").loading_a_pct
        assert np.abs(fast.loc[slow.index] - slow).max() < 1e-5


def test_fast_solver_reports_an_islanding_outage(hour):
    case, _, _, _ = hour
    grid = grid_from_case(case)
    assert not solve(grid, outage=grid.names.index("line 3000-3020-1")).converged


def test_screening_tracks_ac(hour):
    """The AC-base, DC-delta estimate follows the AC N-1 closely on this hour."""
    case, net, table, dc = hour
    names = list(table.index)
    outages = [n for n in names if n not in radial_branches(table)]
    positions = np.array([names.index(o) for o in outages])
    estimate = screen_loading(
        _pre(net, table),
        dc.lodf.to_numpy(),
        positions,
        table.rate_a.to_numpy(),
        (table.basis == "current").to_numpy(),
        shunt_outage_shift(net, dc, outages),
    )[0]
    grid = grid_from_case(case)
    base = solve(grid)
    order = [grid.names.index(n) for n in names]
    errors = []
    for row, name in enumerate(outages):
        k = grid.names.index(name)
        sol = solve(grid, outage=k, v0=base.v)
        if sol.converged:
            ac = fast_branch_flows(grid, sol.v, outage=k)[2][order]
            ok = np.isfinite(ac) & (ac > 50)
            errors.append(estimate[row][ok] - ac[ok])
    errors = np.concatenate(errors)
    assert np.median(np.abs(errors)) < 3.0  # percentage points of RATE A


def test_transfer_margin_puts_the_limiting_element_at_its_limit(hour):
    """At the N-0 margin, the limiting branch's active flow reaches its active limit."""
    _, net, table, dc = hour
    names = list(table.index)
    shift = transfer_shift(plant_generation(net), net.bus.zone, ["SE1", "SE2"], ["SE3", "SE4"])
    assert shift.sum() == pytest.approx(0, abs=1e-12)
    assert shift[shift > 0].sum() == pytest.approx(1)
    pre = _pre(net, table)
    margins = transfer_margins(pre, dc, shift, table, [])
    t, m = margins.margin_n0_mw[0], names.index(margins.limiting_n0[0])
    sens = dc.ptdf.loc[names, shift.index].to_numpy() @ shift.to_numpy()
    flow = (pre["p_i"][0, m] - pre["p_j"][0, m]) / 2 + sens[m] * t
    v = min(pre["v_i"][0, m], pre["v_j"][0, m]) if table.basis.iloc[m] == "current" else 1.0
    q = max(abs(pre["q_i"][0, m]), abs(pre["q_j"][0, m]))
    limit = np.sqrt((table.rate_a.iloc[m] * v) ** 2 - q**2)
    assert abs(flow) == pytest.approx(limit, rel=1e-9)
    assert abs(sens[m]) >= 0.05
