"""Solutions keyed by PSS/E identifiers, and the PowerFactory exports (three-way validation)."""

import numpy as np
import pandas as pd
import pytest

from n44.config import load_config, repo_path
from n44.io.build_net import build_net
from n44.io.psse_parser import parse_raw, read_raw
from n44.powerflow import run_ac
from n44.validation import (
    FLOW_COLUMNS,
    compare,
    solution_from_case,
    solution_from_net,
    solution_from_powerfactory,
)
from tests.conftest import FIXTURES

EXPORTS = repo_path(load_config()["powerfactory"]["exports_dir"])


@pytest.fixture(scope="module")
def solved_hour():
    case = read_raw(FIXTURES / "N44_20150325_h18_after_PF.raw")
    net = build_net(case)
    assert run_ac(net).converged
    return case, net


def _powerfactory_like_export(net, case):
    """The pandapower solution written as PowerFactory exports it.

    Every other line is reported from its J end, as PowerFactory does when its bus1 is the
    PSS/E J bus, and transformers are reported HV first.
    """
    sol = solution_from_net(net)
    buses = sol.buses.reset_index()
    gen = net.gen.assign(p_mw=net.res_gen.p_mw, q_mvar=net.res_gen.q_mvar)
    machines = pd.DataFrame(
        {"bus": gen.bus, "id": gen.psse_id, "in_service": 1, "p_mw": gen.p_mw, "q_mvar": gen.q_mvar}
    )
    swing = pd.DataFrame(
        {
            "bus": net.ext_grid.bus,
            "id": "1",
            "in_service": 1,
            "p_mw": net.res_ext_grid.p_mw,
            "q_mvar": net.res_ext_grid.q_mvar,
        }
    )
    flows = sol.branches.reset_index()
    rows = []
    hv = case.bus.set_index("i").baskv
    for k, r in enumerate(flows.itertuples(index=False)):
        swap = (k % 2 == 1) if r.kind == "line" else hv[r.j] > hv[r.i]
        a, b, pa, qa, pb, qb = (r.i, r.j, r.p_i_mw, r.q_i_mvar, r.p_j_mw, r.q_j_mvar)
        if swap:
            a, b, pa, qa, pb, qb = (b, a, pb, qb, pa, qa)
        rows.append((r.kind, a, b, r.ckt, 1, pa, qa, pb, qb, r.loading_a_pct))
    branches = pd.DataFrame(
        rows,
        columns=[
            "kind",
            "bus1",
            "bus2",
            "ckt",
            "in_service",
            "p1_mw",
            "q1_mvar",
            "p2_mw",
            "q2_mvar",
            "loading_pct",
        ],
    )
    return buses, pd.concat([machines, swing], ignore_index=True), branches


def test_powerfactory_export_is_oriented_like_psse(solved_hour):
    case, net = solved_hour
    expected = solution_from_net(net)
    got = solution_from_powerfactory(*_powerfactory_like_export(net, case), case)
    pd.testing.assert_frame_equal(
        got.branches[FLOW_COLUMNS].sort_index(), expected.branches.sort_index(), check_dtype=False
    )
    pd.testing.assert_frame_equal(
        got.plants.sort_index(), expected.plants.sort_index(), check_dtype=False
    )
    assert got.slack_p_mw == pytest.approx(expected.slack_p_mw, abs=1e-9)
    assert got.losses_mw == pytest.approx(expected.losses_mw, abs=1e-9)


def test_compare_against_itself_is_exact(solved_hour):
    case, net = solved_hour
    sol = solution_from_net(net)
    result = compare(sol, sol, case, ("pp", "pp2"))
    assert result.summary["max_dvm_pu"] == 0
    assert result.summary["max_dq_branch_mvar"] == 0
    assert result.summary["within_tolerance"]
    assert {"vm_pp", "vm_pp2", "q_pp2", "limited_pp"} <= (set(result.buses) | set(result.plants))


def test_tolerance_is_absolute_or_relative_per_branch_end(solved_hour):
    case, net = solved_hour
    ref = solution_from_case(case)
    test = solution_from_case(case)
    heavy = test.branches.p_i_mw.abs().idxmax()
    flow = test.branches.loc[heavy, "p_i_mw"]
    test.branches.loc[heavy, "p_i_mw"] = flow * 1.005  # beyond 1 MW, within 1 %
    assert abs(flow) * 0.005 > 1.0
    assert compare(test, ref, case, ("t", "ref")).summary["within_tolerance"]
    test.branches.loc[heavy, "p_i_mw"] = flow * 1.02  # beyond both
    assert not compare(test, ref, case, ("t", "ref")).summary["within_tolerance"]


@pytest.mark.dataset
@pytest.mark.skipif(not (EXPORTS / "ldf_buses.csv").exists(), reason="no PowerFactory exports")
def test_powerfactory_exports_match(dataset):
    """PowerFactory as imported differs only near off-grid taps; with exact ratios it agrees."""
    h = 538
    tables = {}
    for name in ("ldf_buses", "ldf_machines", "ldf_branches"):
        frame = pd.read_csv(EXPORTS / f"{name}.csv")
        tables[name] = frame[frame.h == h].copy()
    tables["ldf_branches"]["ckt"] = tables["ldf_branches"].ckt.astype(str)
    tables["ldf_machines"]["id"] = tables["ldf_machines"].id.astype(str)
    case = parse_raw(dataset.raw_text(h, "after"))
    net = build_net(case)
    assert run_ac(net).converged
    pp_sol = solution_from_net(net)
    results = {}
    for variant in ("imported", "exact"):
        parts = [t[t.variant == variant] for t in tables.values()]
        pf_sol = solution_from_powerfactory(*parts, case)
        results[variant] = compare(pf_sol, pp_sol, case, ("pf", "pp"))
    exact = results["exact"].summary
    assert exact["max_dvm_pu"] < 1e-6 and exact["max_dq_branch_mvar"] < 1e-2
    assert exact["within_tolerance"] and exact["limited_plants_match"]
    imported = results["imported"]
    worst = imported.buses.dvm.abs()
    assert worst.idxmax() == 5601  # behind transformer 5601-6001: WINDV 1.0054, tap +1 = 1.00635
    assert 5e-4 < worst.max() < 1e-3
    assert np.isclose(imported.summary["losses_pf_mw"], exact["losses_pf_mw"], atol=0.1)
