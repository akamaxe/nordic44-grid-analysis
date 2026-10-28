"""pandapower network against the reference model (validation level B) and PSS/E (level C)."""

import numpy as np
import pandapower as pp
import pytest

from n44.io.build_net import build_net, set_branch_status
from n44.io.psse_parser import read_raw
from n44.powerflow import ac_options, losses_mw, run_ac, voltage_deviation
from n44.results import branch_flows as pp_branch_flows
from n44.results import plant_reactive_power
from n44.ybus import branch_flows, build_reference
from tests.conftest import FIXTURES


@pytest.fixture(scope="module")
def base_case():
    return read_raw(FIXTURES / "N44_BC.raw")


@pytest.fixture(scope="module")
def solved_base(base_case):
    net = build_net(base_case)
    pp.runpp(net, init="flat", **ac_options())
    return net


def test_element_counts(solved_base):
    net = solved_base
    sizes = {k: len(net[k]) for k in ("bus", "load", "shunt", "gen", "ext_grid", "line", "trafo")}
    # 6 swing machines become one ext_grid; 4 lines carry shunts at both ends
    assert sizes == {
        "bus": 44,
        "load": 48,
        "shunt": 8,
        "gen": 74,
        "ext_grid": 1,
        "line": 67,
        "trafo": 12,
    }
    assert (net.load.kind == "exchange").sum() == 13
    assert list(net.bus.index[:3]) == [3000, 3020, 3100]  # PSS/E bus numbers


def test_conversions(solved_base):
    net = solved_base
    line = net.line[(net.line.psse_i == 3000) & (net.line.psse_j == 3115)]
    assert line.max_i_ka.item() == pytest.approx(2.74929, abs=1e-5)
    trafo = net.trafo.set_index("psse_i")
    # Tap on the HV winding (5101 is winding 1, 420 kV) and on the LV winding (5300, 300 kV)
    hv = trafo.loc[5101, ["vn_hv_kv", "vn_lv_kv", "vk_percent", "vkr_percent", "sn_mva"]]
    assert hv.to_numpy() == pytest.approx([425.355, 300.0, 6.10210, 0.16, 2000.0], abs=1e-5)
    lv = trafo.loc[5300, ["vn_hv_kv", "vn_lv_kv", "vk_percent", "vkr_percent", "sn_mva"]]
    assert lv.to_numpy() == pytest.approx([420.0, 298.095, 24.40839, 0.64, 4000.0], abs=1e-5)


def test_admittance_matrix_matches_reference(base_case, solved_base):
    net, ref = solved_base, build_reference(base_case)
    rows = net._pd2ppc_lookups["bus"][ref.buses]
    y_pp = net._ppc["internal"]["Ybus"].toarray()[np.ix_(rows, rows)]
    assert np.abs(y_pp - ref.ybus).max() < 1e-10
    # The bus mapping is right if pandapower's own bus balance follows from Y and V. Y holds the
    # shunts, while res_bus counts them as demand, so they are added back.
    v = (net.res_bus.vm_pu * np.exp(1j * np.deg2rad(net.res_bus.va_degree)))[ref.buses]
    s = v.to_numpy() * np.conj(y_pp @ v.to_numpy()) * net.sn_mva
    shunt = (net.res_shunt.p_mw + 1j * net.res_shunt.q_mvar).groupby(net.shunt.bus).sum()
    s_pp = -(net.res_bus.p_mw + 1j * net.res_bus.q_mvar)
    s_pp += shunt.reindex(net.bus.index, fill_value=0)
    assert np.abs(s - s_pp[ref.buses].to_numpy()).max() < 1e-6


def test_base_case_solution(solved_base):
    # Same values as an independent Newton-Raphson written in numpy from the PSS/E equations
    net = solved_base
    assert net.res_ext_grid.p_mw.sum() == pytest.approx(4547.213, abs=1e-3)
    assert net.res_ext_grid.q_mvar.sum() == pytest.approx(166.861, abs=1e-3)
    assert losses_mw(net) == pytest.approx(342.603, abs=1e-3)
    vm = net.res_bus.vm_pu
    assert (vm.idxmin(), vm.min()) == (5101, pytest.approx(0.99354, abs=1e-5))
    assert (vm.idxmax(), vm.max()) == (3100, pytest.approx(1.03804, abs=1e-5))


def test_terminal_flows_match_reference(base_case, solved_base):
    net, ref = solved_base, build_reference(base_case)
    v = (net.res_bus.vm_pu * np.exp(1j * np.deg2rad(net.res_bus.va_degree)))[ref.buses]
    expected = branch_flows(base_case, ref, v.to_numpy())
    got = pp_branch_flows(net)
    for column in ("p_i_mw", "q_i_mvar", "p_j_mw", "q_j_mvar", "loading_a_pct"):
        assert np.abs(got[column].to_numpy() - expected[column].to_numpy()).max() < 1e-6, column


def test_set_branch_status_switches_line_end_shunts(base_case):
    net = build_net(base_case)
    line = net.line.index[(net.line.psse_i == 5101) & (net.line.psse_j == 5501)].item()
    set_branch_status(net, "line", line, False)
    assert not net.shunt.in_service[net.shunt.parent_line == line].any()
    set_branch_status(net, "line", line, True)
    assert net.shunt.in_service[net.shunt.parent_line == line].all()


def test_solved_hour_matches_psse():
    case = read_raw(FIXTURES / "N44_20150325_h18_after_PF.raw")
    net = build_net(case)
    result = run_ac(net)
    assert result.converged and result.init == "flat"
    assert result.q_limited == [7100] and result.q_inconsistent == []
    dvm, dva = voltage_deviation(net)
    assert dvm < 2e-5 and dva < 5e-4
    assert net.res_ext_grid.p_mw.sum() == pytest.approx(net.ext_grid.psse_pg_mw.sum(), abs=0.1)
    q_psse = case.generator[case.generator.stat == 1].groupby("i").qg.sum()
    assert (plant_reactive_power(net) - q_psse).abs().max() < 0.5
