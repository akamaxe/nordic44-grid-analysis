"""Scenario changes to an hourly case (n44.scenarios) and the cut crossing used with them."""

import numpy as np
import pytest

from n44.acpf import grid_from_case, solve
from n44.contingency import branch_table, cut_crossing
from n44.io.build_net import build_net
from n44.io.psse_parser import read_raw
from n44.scenarios import links, load_scenario, modify_case
from tests.conftest import FIXTURES


@pytest.fixture(scope="module")
def case():
    return read_raw(FIXTURES / "N44_20150325_h18_after_PF.raw")


def _demand(case) -> float:
    return case.load[case.load.status == 1].pl.sum()


def _generation(case) -> float:
    return case.generator[case.generator.stat == 1].pg.sum()


def test_new_circuit_is_a_copy_and_leaves_the_case_untouched(case):
    before = len(case.branch)
    out = modify_case(case, load_scenario("ac_line_north"))
    assert len(case.branch) == before
    new = out.branch[(out.branch.i == 3100) & (out.branch.j == 3249) & (out.branch.ckt == "2")]
    source = case.branch[(case.branch.i == 3100) & (case.branch.j == 3249)]
    assert len(new) == 1
    for column in ("r", "x", "b", "ratea", "rateb"):
        assert new[column].iloc[0] == source[column].iloc[0]


def test_new_load_is_produced_by_the_supplying_zones(case):
    scenario = load_scenario("north_load")
    out = modify_case(case, scenario, load_mw=3000.0)
    assert _demand(out) - _demand(case) == pytest.approx(3000.0)
    assert _generation(out) - _generation(case) == pytest.approx(3000.0, abs=1e-6)
    added = out.load[out.load.id.str.startswith("N")]
    assert set(added.i) == {3115, 3249}
    zone = out.bus.set_index("i").area.map(out.area.set_index("i").arname.str.strip())
    changed = out.generator.pg.to_numpy() != case.generator.pg.to_numpy()
    assert set(out.generator.i[changed].map(zone)) <= set(scenario["add_load"]["supplied_by"])


def test_wind_displaces_generation_in_the_supplying_zones(case):
    out = modify_case(case, load_scenario("north_wind"), load_mw=5000.0, wind_mw=4000.0)
    assert _demand(out) - _demand(case) == pytest.approx(1000.0)
    assert _generation(out) - _generation(case) == pytest.approx(1000.0, abs=1e-6)


def test_hvdc_converters_carry_the_set_point_and_solve(case):
    scenario = load_scenario("hvdc_north")
    ((a, b, capacity),) = links(scenario)
    out = modify_case(case, scenario, set_points=np.array([800.0]))
    converters = out.generator[out.generator.id == "DC"].set_index("i")
    assert converters.pg[a] == pytest.approx(-800.0)
    assert converters.pg[b] == pytest.approx(800.0 * (1 - scenario["hvdc"][0]["losses"]))
    assert converters.pt.max() == capacity
    assert solve(grid_from_case(out)).converged


def test_cut_crossing_signs_branches_by_direction(case):
    net = build_net(case)
    table = branch_table(net)
    crossing = cut_crossing(table, net.bus.zone, ("SE1", "SE2"), ("SE3", "SE4"))
    names = list(table.index)
    assert crossing[names.index("line 3100-3359-2")] == 1.0  # Hjälta (SE2) to Ringhals (SE3)
    assert crossing[names.index("line 3000-3245-1")] == -1.0  # Forsmark (SE3) to SE2
    assert crossing[names.index("line 3100-3249-1")] == 0.0  # inside SE2
