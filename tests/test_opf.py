"""DC-OPF and nodal prices, and the redispatch that makes a dispatch secure."""

import copy

import numpy as np
import pandapower as pp
import pytest

from n44.contingency import branch_table, dc_model, radial_branches, shunt_outage_shift
from n44.io.build_net import build_net
from n44.io.psse_parser import read_raw
from n44.opf import dc_opf, demand_vector, machine_table
from n44.powerflow import run_ac
from n44.redispatch import effective_limits, redispatch
from n44.results import branch_flows
from tests.conftest import FIXTURES


@pytest.fixture(scope="module")
def hour():
    net = build_net(read_raw(FIXTURES / "N44_20150325_h18_after_PF.raw"))
    assert run_ac(net).converged
    return net, branch_table(net), dc_model(net), machine_table(net)


def test_machine_table_covers_every_generator(hour):
    net, _, _, units = hour
    assert len(units) == len(net.gen) + len(net.ext_grid)
    assert set(units.technology) <= {"nuclear", "hydro", "thermal"}
    assert units.cost.notna().all()


def test_dc_opf_matches_pandapower(hour):
    net, table, dc, units = hour
    own = dc_opf(dc, units, demand_vector(net), table.rate_a)
    assert own.success
    work = copy.deepcopy(net)
    work.line["max_loading_percent"] = 100.0
    work.trafo["max_loading_percent"] = 100.0
    work.gen["controllable"] = True
    work.load["controllable"] = False
    for row in units.itertuples():
        pp.create_poly_cost(work, row.index, row.element, cp1_eur_per_mw=row.cost)
    pp.rundcopp(work)
    assert own.cost == pytest.approx(work.res_cost, rel=1e-7)
    lam = work.res_bus.lam_p.loc[own.lmp.index]
    assert np.abs(lam - own.lmp).max() < 1e-5
    assert own.flows.abs().le(table.rate_a + 1e-6).all()


def test_nodal_price_is_the_marginal_cost_of_demand(hour):
    net, table, dc, units = hour
    demand = demand_vector(net)
    base = dc_opf(dc, units, demand, table.rate_a)
    for bus in (3100, 5101, 7000):
        bumped = demand.copy()
        bumped[bus] += 0.1
        again = dc_opf(dc, units, bumped, table.rate_a)
        assert (again.cost - base.cost) / 0.1 == pytest.approx(base.lmp[bus], abs=1e-3)
    assert base.lmp[dc.slack] == pytest.approx(base.energy, abs=1e-9)


def _hour_flows(net, table) -> dict[str, np.ndarray]:
    flows = branch_flows(net)
    flows.index = [
        f"{k} {i}-{j}-{c}"
        for k, i, j, c in zip(flows.kind, flows.i, flows.j, flows.ckt, strict=True)
    ]
    flows = flows.loc[table.index]
    vm = net.res_bus.vm_pu
    return {
        "p_i": flows.p_i_mw.to_numpy(),
        "p_j": flows.p_j_mw.to_numpy(),
        "q_i": flows.q_i_mvar.to_numpy(),
        "q_j": flows.q_j_mvar.to_numpy(),
        "v_i": vm[table.i].to_numpy(),
        "v_j": vm[table.j].to_numpy(),
    }


def test_redispatch_removes_intact_overloads(hour):
    net, table, dc, units = hour
    pre = _hour_flows(net, table)
    dispatch = np.r_[net.res_gen.p_mw.to_numpy(), net.res_ext_grid.p_mw.to_numpy()]
    result = redispatch(pre, dc, table, units, dispatch, [], n1=False)
    assert result.success and result.unresolved_mw == pytest.approx(0, abs=1e-6)
    delta = result.up - result.down
    assert delta.sum() == pytest.approx(0, abs=1e-6)
    new_flow = (pre["p_i"] - pre["p_j"]) / 2 + dc.ptdf.loc[
        table.index, units.bus
    ].to_numpy() @ delta
    limit = effective_limits(pre, table.rate_a.to_numpy(), (table.basis == "current").to_numpy())
    assert np.all(np.abs(new_flow) <= limit + 1e-6)
    assert np.all(dispatch + delta <= units.p_max_mw.to_numpy() + 1e-6)


def test_redispatch_n1_respects_every_outage(hour):
    net, table, dc, units = hour
    names = list(table.index)
    outages = [n for n in names if n not in radial_branches(table)]
    shift = shunt_outage_shift(net, dc, outages)
    pre = _hour_flows(net, table)
    dispatch = np.r_[net.res_gen.p_mw.to_numpy(), net.res_ext_grid.p_mw.to_numpy()]
    result = redispatch(pre, dc, table, units, dispatch, outages, shift, n1=True)
    assert result.success
    delta = result.up - result.down
    sens = dc.ptdf.loc[names, units.bus].to_numpy()
    lodf = dc.lodf.loc[names, names].to_numpy()
    flow = (pre["p_i"] - pre["p_j"]) / 2 + sens @ delta
    limit_b = effective_limits(pre, table.rate_b.to_numpy(), (table.basis == "current").to_numpy())
    for k, name in enumerate(outages):
        pos = names.index(name)
        post = flow + lodf[:, pos] * flow[pos] + shift[:, k]
        post[pos] = 0.0
        assert np.all(np.abs(post) <= limit_b + 1e-4 + result.unresolved_mw)


def test_no_redispatch_without_constraints(hour):
    net, table, dc, units = hour
    loose = table.assign(rate_a=table.rate_a * 10, rate_b=table.rate_b * 10)
    dispatch = np.r_[net.res_gen.p_mw.to_numpy(), net.res_ext_grid.p_mw.to_numpy()]
    result = redispatch(_hour_flows(net, table), dc, loose, units, dispatch, [], n1=False)
    assert result.volume_mw == pytest.approx(0, abs=1e-6)
