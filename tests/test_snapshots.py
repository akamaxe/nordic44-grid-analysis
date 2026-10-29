import numpy as np
import pandas as pd
import pytest

from n44.io.build_net import build_net
from n44.io.psse_parser import read_raw
from n44.powerflow import run_ac
from n44.snapshots import (
    SnapshotStore,
    apply_values,
    calendar,
    clock_changes,
    load_snapshot,
    values_from_case,
)
from tests.conftest import FIXTURES


def _solution(net):
    assert run_ac(net).converged
    return net.res_bus[["vm_pu", "va_degree"]].to_numpy(), net.res_ext_grid.p_mw.sum()


def test_values_applied_to_base_network_reproduce_the_hour():
    hour = read_raw(FIXTURES / "N44_20150325_h18_after_PF.raw")
    expected_v, expected_p = _solution(build_net(hour))
    net = build_net(read_raw(FIXTURES / "N44_BC.raw"))
    apply_values(net, values_from_case(hour))
    got_v, got_p = _solution(net)
    assert np.abs(got_v - expected_v).max() < 1e-9
    assert got_p == pytest.approx(expected_p, abs=1e-6)


def test_calendar_handles_clock_changes():
    cal = calendar(2015)
    assert len(cal) == 8760 and cal.synthetic.sum() == 1
    assert cal.loc[cal.synthetic].index[0] == 2090  # 2015-03-29 02:00 does not exist
    spring = cal[cal.date == "2015-03-29"].utc_time
    assert spring.iloc[1] == pd.Timestamp("2015-03-29 00:00", tz="UTC")  # 01:00 CET
    assert spring.iloc[3] == pd.Timestamp("2015-03-29 01:00", tz="UTC")  # 03:00 CEST
    autumn = cal[cal.date == "2015-10-25"]
    assert autumn.local_time.iloc[2] == autumn.local_time.iloc[3]  # 02:00 twice
    assert autumn.utc_time.iloc[3] - autumn.utc_time.iloc[2] == pd.Timedelta("1h")
    assert autumn.local_time.iloc[23] == pd.Timestamp("2015-10-25 22:00")


def test_other_years_change_clocks_on_their_own_days():
    assert clock_changes(2025, "Europe/Stockholm") == (
        pd.Timestamp("2025-03-30"),
        pd.Timestamp("2025-10-26"),
    )
    cal = calendar(2025)
    assert cal.loc[cal.synthetic].index[0] == 24 * 88 + 2
    assert cal.utc_time.iloc[0] == pd.Timestamp("2024-12-31 23:00", tz="UTC")


@pytest.mark.dataset
def test_load_snapshot_matches_the_raw_file():
    store = SnapshotStore()
    assert all(len(t) == 8760 for t in store.tables.values())
    net = load_snapshot(2010, store)  # 2015-03-25 h18, the fixture hour
    hour = read_raw(FIXTURES / "N44_20150325_h18_after_PF.raw")
    expected_v, expected_p = _solution(build_net(hour))
    got_v, got_p = _solution(net)
    assert np.abs(got_v - expected_v).max() < 1e-9
    assert got_p == pytest.approx(expected_p, abs=1e-6)
