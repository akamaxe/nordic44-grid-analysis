import pytest

from n44.annual import branch_names, zone_losses
from n44.io.psse_parser import read_raw
from n44.ybus import branch_flows, build_reference
from n44.zones import ZONES, bus_zones
from tests.conftest import FIXTURES


@pytest.fixture(scope="module")
def flows_and_zones():
    case = read_raw(FIXTURES / "N44_20150325_h18_after_PF.raw")
    return branch_flows(case, build_reference(case)), bus_zones(case.bus, case.area)


def test_zone_losses_add_up(flows_and_zones):
    flows, zone = flows_and_zones
    losses = zone_losses(flows, zone)
    assert list(losses.index) == list(ZONES)
    assert losses.sum() == pytest.approx(flows.loss_mw.sum())


def test_branch_names_are_unique(flows_and_zones):
    flows, _ = flows_and_zones
    names = branch_names(flows)
    assert len(set(names)) == len(names) == 79
    assert names[0] == "line 3000-3020-1" and names[-1].startswith("transformer ")
