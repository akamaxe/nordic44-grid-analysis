import numpy as np
import pandas as pd
import pytest

from n44.fitting import EXCHANGE_TAN, flow_inputs, scale, solve_interchange
from n44.io.psse_parser import read_raw
from n44.snapshots import case_from_values, values_from_case
from n44.ybus import area_interchange, branch_flows
from n44.zones import (
    EXCHANGE_LOADS,
    EXTERNAL_LINKS,
    NEW_LINKS,
    NORDPOOL_BORDERS,
    ZONES,
    bus_zones,
)
from tests.conftest import FIXTURES


@pytest.fixture(scope="module")
def base():
    return read_raw(FIXTURES / "N44_BC.raw")


def test_scaling_meets_the_zonal_targets(base):
    consumption = pd.Series({z: 1000.0 + 100 * k for k, z in enumerate(ZONES)})
    production = pd.Series({z: 2000.0 - 50 * k for k, z in enumerate(ZONES)})
    loads = pd.Series({f"{e.bus}_{e.load_id}": 100.0 for e in EXCHANGE_LOADS})
    pdes = pd.Series({z: float(k) for k, z in enumerate(ZONES)})
    out = scale(base, consumption, production, loads, pdes)
    zone = bus_zones(out.bus, out.area)
    load, gen = out.load, out.generator
    scalable = load[load.scale == 1]
    assert scalable.pl.groupby(scalable.i.map(zone)).sum().to_dict() == pytest.approx(
        consumption.to_dict()
    )
    on = gen[gen.stat == 1]
    assert on.pg.groupby(on.i.map(zone)).sum().to_dict() == pytest.approx(production.to_dict())
    # the load mix inside a zone and its power factor stay as they were
    ratio = scalable.pl / base.load.pl[scalable.index]
    assert ratio.groupby(scalable.i.map(zone)).std().max() < 1e-12
    assert np.allclose(
        scalable.ql / scalable.pl, base.load.ql[scalable.index] / base.load.pl[scalable.index]
    )
    exchange = load[load.scale == 0].set_index(load[load.scale == 0].i)
    assert exchange.pl.eq(100.0).all()
    assert exchange.ql[3020] == pytest.approx(100 * EXCHANGE_TAN)
    assert exchange.ql[5610] == pytest.approx(100 * np.tan(np.arccos(0.99)))  # Skagerrak
    assert out.area.set_index(out.area.arname.str.strip()).pdes.to_dict() == pdes.to_dict()


def test_interchange_control_reproduces_psse():
    """PSS/E solved the before_PF case into the after_PF case of the same hour."""
    before = read_raw(FIXTURES / "N44_20150325_h18_before_PF.raw")
    after = read_raw(FIXTURES / "N44_20150325_h18_after_PF.raw")
    result, _ = solve_interchange(before)
    assert result.converged and result.rounds < 10
    export = area_interchange(result.case, branch_flows(result.case))
    pdes = result.case.area.set_index("i").pdes
    swing_area = 23  # SE3, whose interchange follows from the others
    error = (export - pdes).drop(swing_area).abs()
    assert error.max() < 1.0
    # PSS/E stops within its 10 MW interchange tolerance, so its slack outputs may differ
    # by about that much
    assert np.abs(result.case.generator.pg - after.generator.pg).max() < 10.0
    assert np.abs(result.case.bus.vm - after.bus.vm).max() < 1e-3
    assert np.allclose(result.case.transformer.windv1, after.transformer.windv1)


def test_border_flows_become_exchange_loads_and_pdes():
    index = pd.RangeIndex(2)
    columns = [f"{a}->{b}" for a, b in [*NORDPOOL_BORDERS, *EXTERNAL_LINKS, *NEW_LINKS]]
    flows = pd.DataFrame(0.0, index=index, columns=columns)
    flows["NO2->DE"], flows["NO2->NL"], flows["SE3->FI1"], flows["SE2->SE3"] = 300, 200, 500, 1000
    zonal = pd.DataFrame(1000.0, index=index, columns=list(ZONES))
    inputs = flow_inputs(zonal, zonal, flows)
    loads = inputs.exchange_loads.loc[0]
    assert loads["5620_1"] == 200  # NorNed on the Feda load
    assert inputs.consumption.loc[0, "NO2"] == 1300  # NordLink, built in 2020, as demand
    assert loads["3020_1"] == 500 and loads["7000_6"] == -500  # Fenno-Skan on both sides
    assert inputs.pdes.loc[0, "SE2"] == 1000 and inputs.pdes.loc[0, "SE3"] == -1000
    assert inputs.pdes.loc[0, "FI1"] == 0  # the load link is not a tie


def test_values_round_trip_through_a_case(base):
    hour = read_raw(FIXTURES / "N44_20150325_h18_after_PF.raw")
    values = values_from_case(hour)
    again = values_from_case(case_from_values(base, values))
    for name in values.__dataclass_fields__:
        assert np.allclose(
            getattr(again, name).to_numpy(float), getattr(values, name).to_numpy(float)
        )
