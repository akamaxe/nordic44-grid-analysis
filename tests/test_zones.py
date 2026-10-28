import pandas as pd
import pytest

from n44.io.psse_parser import read_raw
from n44.ybus import area_interchange, branch_flows, build_reference
from n44.zones import (
    ARTIFICIAL_BORDERS,
    CUTS,
    NORDPOOL_BORDERS,
    border_flows,
    bus_zones,
    cut_flows,
    zone_to_zone,
)
from tests.conftest import FIXTURES


@pytest.fixture(scope="module")
def hour():
    case = read_raw(FIXTURES / "N44_20150325_h18_after_PF.raw")
    flows = branch_flows(case, build_reference(case))
    zone = bus_zones(case.bus, case.area)
    load_p = case.load.set_index(["i", "id"]).pl
    return case, flows, zone, load_p


def test_zone_flows_add_up_to_area_interchange(hour):
    case, flows, zone, _ = hour
    pairs = zone_to_zone(flows, zone)  # branches only, as PSS/E's interchange
    export = pd.Series(0.0, index=sorted(set(zone)))
    for (a, b), p in pairs.items():
        export[a] += p
        export[b] -= p
    expected = area_interchange(case, flows)
    expected.index = case.area.set_index("i").arname[expected.index]
    pd.testing.assert_series_equal(
        export.sort_index(), expected.sort_index(), check_names=False, atol=1e-6
    )


def test_load_links_carry_the_exchange_loads(hour):
    _, flows, zone, load_p = hour
    with_links = border_flows(flows, zone, load_p)
    without = border_flows(flows, zone)
    assert with_links["SE3->FI1"] - without["SE3->FI1"] == pytest.approx(load_p[(3020, "1")])
    assert with_links["NO4->FI1"] - without["NO4->FI1"] == pytest.approx(load_p[(6701, "1")])


def test_border_and_cut_definitions(hour):
    _, flows, zone, load_p = hour
    borders = border_flows(flows, zone, load_p)
    assert len(borders) == len(NORDPOOL_BORDERS) + len(ARTIFICIAL_BORDERS)
    cuts = cut_flows(flows, zone)
    # snitt 4 is the SE3-SE4 border itself: SE4 has no other AC tie
    assert cuts["snitt 4 (SE3->SE4)"] == pytest.approx(borders["SE3->SE4"], abs=1e-9)
    # snitt 2 adds the artificial SE1-SE3 tie to the SE2-SE3 border
    assert cuts["snitt 2 (SE2->SE3)"] == pytest.approx(
        borders["SE2->SE3"] + borders["SE1->SE3"], abs=1e-9
    )
    assert set(CUTS) == set(cuts.index)


@pytest.mark.dataset
def test_nordpool_columns_exist():
    from n44.config import configured_path

    exchange = pd.read_parquet(configured_path("processed_dir") / "nordpool_exchange.parquet")
    assert set(NORDPOOL_BORDERS.values()) <= set(exchange.columns)
