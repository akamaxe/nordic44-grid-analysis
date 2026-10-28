"""Reference model checks against PSS/E's own solutions (validation level A)."""

import numpy as np
import pytest

from n44.io.psse_parser import read_raw
from n44.ybus import area_interchange, branch_flows, build_reference, power_mismatch
from tests.conftest import FIXTURES


@pytest.fixture(scope="module")
def base_case():
    return read_raw(FIXTURES / "N44_BC.raw")


@pytest.fixture(scope="module")
def solved_hour():
    return read_raw(FIXTURES / "N44_20150325_h18_after_PF.raw")


def test_admittance_matrix_shape(base_case):
    model = build_reference(base_case)
    diag = np.abs(np.diag(model.ybus))
    assert np.allclose(model.ybus, model.ybus.T)  # no phase shifters
    assert model.buses[diag.argmax()] == 6001
    assert diag.max() == pytest.approx(314.9, abs=0.1)


def test_solved_hour_balances_at_every_bus(solved_hour):
    mismatch = power_mismatch(solved_hour)
    assert (mismatch.ds_mva <= mismatch.bound_mva).all()
    assert mismatch.dp_mw.abs().max() < 0.5
    assert mismatch.dq_mvar.abs().max() < 2.0


def test_base_case_is_not_solved(base_case):
    # PQ buses balance at the stored voltages, generator buses do not: generation was
    # rescaled per area after the last solution (validation log, entry 1)
    mismatch = power_mismatch(base_case)
    gen = base_case.generator.groupby("i").pg.sum()
    pq = mismatch.drop(index=gen.index)
    assert pq.dp_mw.abs().max() == pytest.approx(0.172, abs=1e-3)
    assert pq.dq_mvar.abs().max() == pytest.approx(0.514, abs=1e-3)
    assert mismatch.dq_mvar.abs().max() < 0.6
    ratio = (mismatch.dp_mw[gen.index] / gen).rename("ratio").to_frame()
    ratio["area"] = base_case.bus.set_index("i").area[gen.index]
    spread = ratio.groupby("area").ratio.agg(lambda r: r.max() - r.min())
    assert spread.max() < 1e-3  # one scaling factor per area
    assert mismatch.dp_mw.sum() == pytest.approx(-342.1, abs=0.5)


def test_negative_control_line_shunts(base_case):
    # Leaving out the per-end line shunts must break the balance at bus 5501
    broken = read_raw(FIXTURES / "N44_BC.raw")
    broken.branch[["gi", "bi", "gj", "bj"]] = 0.0
    assert abs(power_mismatch(broken).dq_mvar[5501]) == pytest.approx(991.5, abs=1.0)


def test_negative_control_tap_side(base_case):
    # Putting the off-nominal ratio on winding 2 instead of winding 1 must break the balance
    broken = read_raw(FIXTURES / "N44_BC.raw")
    tr = broken.transformer
    tr["windv2"], tr["windv1"] = tr["windv1"].copy(), 1.0
    worst = power_mismatch(broken).dq_mvar.abs()
    assert worst.idxmax() == 6001 and worst.max() > 2000


def test_terminal_flows_include_line_shunts(base_case):
    flows = branch_flows(base_case)
    line = flows[(flows.i == 5101) & (flows.j == 5501)]
    assert line.loading_a_pct.item() == pytest.approx(58.8, abs=0.1)


def test_metered_interchange_matches_pdes(solved_hour):
    export = area_interchange(solved_hour, branch_flows(solved_hour))
    pdes = solved_hour.area.set_index("i").pdes
    assert export.sum() == pytest.approx(0.0, abs=1e-6)
    assert (export - pdes).abs().max() < 3.0
