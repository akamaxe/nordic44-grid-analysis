"""pandapower network built from a PSS/E revision-33 case.

The conversion is exact for the Nordic 44 data, and docs/methods.md derives it.

- **Network:** ``net.sn_mva = SBASE``, so per-unit values match PSS/E's. The bus index is the
  PSS/E bus number and ``vn_kv`` is BASKV.
- **Lines:** 1 km long, with R, X and B converted to ohm and nF through Zb = BASKV^2/SBASE.
  ``max_i_ka = RATEA / (sqrt(3) BASKV)``. Line shunts at each end become pandapower shunts
  tied to their line through ``parent_line`` and ``parent_end``. Change line statuses with
  ``set_branch_status``, which switches those shunts too.
- **Two-winding transformers:** ``vn_hv_kv`` and ``vn_lv_kv`` are WINDV times BASKV of each
  winding, and ``sn_mva`` is RATA1. ``vk_percent`` and ``vkr_percent`` are R1-2 and X1-2
  rescaled to ``sn_mva``. ``shift_degree`` is ANG1, with the sign flipped when winding 1 is
  the low-voltage side. There is no tap changer and no magnetizing branch. pandapower refers
  the impedance to the LV side through (vn_lv_kv / Vb_lv)^2 and places the ratio on the HV
  side. This reproduces the PSS/E t1:1 - Z - 1:t2 model whichever side winding 1 is on.
- **Machines:** the swing bus becomes one ``ext_grid`` aggregating its in-service machines.
  Every other machine is a ``gen``.
- **Loads:** constant power. ``kind`` is "exchange" for the 13 loads that stand for
  exchanges and "demand" for the rest.

PSS/E identifiers are kept in ``psse_*`` columns, and branch ratings in ``rate_*_mva``.
"""

from __future__ import annotations

import numpy as np
import pandapower as pp
import pandas as pd

from n44.io.psse_parser import RawCase
from n44.ybus import check_supported
from n44.zones import EXCHANGE_LOAD_KEYS, bus_zones

ZERO_IMPEDANCE_PU = 1e-4  # PSS/E default threshold (THRSHZ) for zero-impedance lines
UNRATED_MAX_I_KA = 99_999.0


def _check_case(case: RawCase) -> None:
    check_supported(case)
    if (case.bus.ide == 3).sum() != 1:
        raise ValueError("the case must have exactly one swing bus")
    ide = case.bus.set_index("i").ide
    live = case.generator[case.generator.stat == 1]
    if not live.i.map(ide).isin([2, 3]).all():
        raise ValueError("in-service machines must sit at PV (2) or swing (3) buses")
    remote = live[(live.ireg != 0) & (live.ireg != live.i)]
    if not remote.empty:
        raise NotImplementedError(f"remote voltage control (IREG) at buses {set(remote.i)}")
    if (live.groupby("i").vs.nunique() > 1).any():
        raise ValueError("machines at the same bus have different voltage setpoints")
    vb = case.bus.set_index("i").baskv
    br = case.branch
    if not np.array_equal(vb[br.i].to_numpy(), vb[br.j].to_numpy()):
        raise NotImplementedError("non-transformer branches between different base voltages")
    for name, z in (
        ("branch", np.hypot(br.r, br.x)),
        ("transformer", np.hypot(case.transformer.r1_2, case.transformer.x1_2)),
    ):
        if (z < ZERO_IMPEDANCE_PU).any():
            raise NotImplementedError(f"zero-impedance {name} records")


def build_net(case: RawCase) -> pp.pandapowerNet:
    """pandapower network equivalent to ``case`` (see the module docstring)."""
    _check_case(case)
    net = pp.create_empty_network(name=case.title[1], f_hz=case.basfrq, sn_mva=case.sbase)
    _add_buses(net, case)
    _add_loads(net, case)
    _add_shunts(net, case)
    _add_machines(net, case)
    _add_lines(net, case)
    _add_transformers(net, case)
    return net


def _add_buses(net, case: RawCase) -> None:
    bus = case.bus
    zone = bus_zones(bus, case.area)
    pp.create_buses(
        net,
        len(bus),
        vn_kv=bus.baskv.to_numpy(),
        index=bus.i.to_numpy(),
        name=bus.name.to_numpy(),
        zone=zone[bus.i].to_numpy(),
        in_service=(bus.ide != 4).to_numpy(),
        max_vm_pu=bus.nvhi.to_numpy(),
        min_vm_pu=bus.nvlo.to_numpy(),
    )
    net.bus["psse_area"] = bus.area.to_numpy()
    net.bus["psse_vm_pu"] = bus.vm.to_numpy()
    net.bus["psse_va_degree"] = bus.va.to_numpy()


def _add_loads(net, case: RawCase) -> None:
    load = case.load
    keys = list(zip(load.i, load.id, strict=True))
    pp.create_loads(
        net,
        buses=load.i.to_numpy(),
        p_mw=load.pl.to_numpy(),
        q_mvar=load.ql.to_numpy(),
        name=[f"{bus}_{load_id}" for bus, load_id in keys],
        in_service=(load.status == 1).to_numpy(),
    )
    net.load["psse_id"] = load.id.to_numpy()
    net.load["psse_scale"] = load.scale.to_numpy()
    net.load["kind"] = ["exchange" if key in EXCHANGE_LOAD_KEYS else "demand" for key in keys]


def _add_shunts(net, case: RawCase) -> None:
    fixed = case.fixed_shunt
    if not fixed.empty:
        idx = pp.create_shunts(
            net,
            buses=fixed.i.to_numpy(),
            q_mvar=-fixed.bl.to_numpy(),
            p_mw=fixed.gl.to_numpy(),
            in_service=(fixed.status == 1).to_numpy(),
        )
        net.shunt.loc[idx, "kind"] = "fixed"
    switched = case.switched_shunt
    if not switched.empty:  # held at BINIT: shunt switching is not modelled
        idx = pp.create_shunts(
            net,
            buses=switched.i.to_numpy(),
            q_mvar=-switched.binit.to_numpy(),
            in_service=(switched.stat == 1).to_numpy(),
        )
        net.shunt.loc[idx, "kind"] = "switched"


def _add_machines(net, case: RawCase) -> None:
    gen, bus = case.generator, case.bus.set_index("i")
    swing = int(case.bus.i[case.bus.ide == 3].iloc[0])
    units = gen[(gen.i == swing) & (gen.stat == 1)]
    if units.empty:
        raise ValueError(f"no machine in service at the swing bus {swing}")
    pp.create_ext_grid(
        net,
        bus=swing,
        vm_pu=float(units.vs.iloc[0]),
        va_degree=float(bus.va[swing]),
        name=str(swing),
        min_p_mw=units.pb.sum(),
        max_p_mw=units.pt.sum(),
        min_q_mvar=units.qb.sum(),
        max_q_mvar=units.qt.sum(),
    )
    net.ext_grid["psse_machines"] = ",".join(units.id)
    net.ext_grid["psse_pg_mw"] = units.pg.sum()
    net.ext_grid["psse_qg_mvar"] = units.qg.sum()
    others = gen[gen.i != swing]
    pp.create_gens(
        net,
        buses=others.i.to_numpy(),
        p_mw=others.pg.to_numpy(),
        vm_pu=others.vs.to_numpy(),
        sn_mva=others.mbase.to_numpy(),
        name=[f"{b}_{m}" for b, m in zip(others.i, others.id, strict=True)],
        max_q_mvar=others.qt.to_numpy(),
        min_q_mvar=others.qb.to_numpy(),
        max_p_mw=others.pt.to_numpy(),
        min_p_mw=others.pb.to_numpy(),
        in_service=(others.stat == 1).to_numpy(),
    )
    net.gen["psse_id"] = others.id.to_numpy()
    net.gen["psse_qg_mvar"] = others.qg.to_numpy()


def _add_lines(net, case: RawCase) -> None:
    br = case.branch
    vb = case.bus.set_index("i").baskv[br.i].to_numpy()
    zb = vb**2 / case.sbase
    rate = br.ratea.to_numpy()
    idx = pp.create_lines_from_parameters(
        net,
        from_buses=br.i.to_numpy(),
        to_buses=br.j.to_numpy(),
        length_km=1.0,
        r_ohm_per_km=br.r.to_numpy() * zb,
        x_ohm_per_km=br.x.to_numpy() * zb,
        c_nf_per_km=br.b.to_numpy() / zb / (2 * np.pi * case.basfrq) * 1e9,
        max_i_ka=np.where(rate > 0, rate / (np.sqrt(3) * vb), UNRATED_MAX_I_KA),
        name=[f"{i}-{j}-{c}" for i, j, c in zip(br.i, br.j, br.ckt, strict=True)],
        in_service=(br.st == 1).to_numpy(),
        max_loading_percent=100.0,
    )
    idx = np.asarray(idx)
    net.line["psse_i"], net.line["psse_j"] = br.i.to_numpy(), br.j.to_numpy()
    net.line["psse_ckt"] = br.ckt.to_numpy()
    net.line["psse_metered"] = np.where(br.met == 2, br.j, br.i)
    for column in ("rate_a", "rate_b", "rate_c"):
        net.line[f"{column}_mva"] = br[column.replace("_", "")].to_numpy()
    for end, g, b, at in (("from", br.gi, br.bi, br.i), ("to", br.gj, br.bj, br.j)):
        has = ((g != 0) | (b != 0)).to_numpy()
        if not has.any():
            continue
        shunts = pp.create_shunts(
            net,
            buses=at.to_numpy()[has],
            q_mvar=-b.to_numpy()[has] * case.sbase,
            p_mw=g.to_numpy()[has] * case.sbase,
            in_service=(br.st == 1).to_numpy()[has],
            name=[f"{n} {end}" for n in net.line.name.loc[idx[has]]],
        )
        net.shunt.loc[shunts, "kind"] = "line_end"
        net.shunt.loc[shunts, "parent_line"] = idx[has]
        net.shunt.loc[shunts, "parent_end"] = end


def _add_transformers(net, case: RawCase) -> None:
    tr = case.transformer
    vb = case.bus.set_index("i").baskv
    vi, vj = vb[tr.i].to_numpy(), vb[tr.j].to_numpy()
    t1, t2 = tr.windv1.to_numpy(), tr.windv2.to_numpy()
    w1_hv = vi >= vj
    sn = np.where(tr.rata1 > 0, tr.rata1, tr.sbase1_2)
    scale = 100.0 * sn / case.sbase
    idx = pp.create_transformers_from_parameters(
        net,
        hv_buses=np.where(w1_hv, tr.i, tr.j),
        lv_buses=np.where(w1_hv, tr.j, tr.i),
        sn_mva=sn,
        vn_hv_kv=np.where(w1_hv, t1 * vi, t2 * vj),
        vn_lv_kv=np.where(w1_hv, t2 * vj, t1 * vi),
        vkr_percent=tr.r1_2.to_numpy() * scale,
        vk_percent=np.hypot(tr.r1_2.to_numpy(), tr.x1_2.to_numpy()) * scale,
        pfe_kw=0.0,
        i0_percent=0.0,
        shift_degree=np.where(w1_hv, tr.ang1, -tr.ang1),
        name=[f"{i}-{j}-{c}" for i, j, c in zip(tr.i, tr.j, tr.ckt, strict=True)],
        in_service=(tr.stat == 1).to_numpy(),
        max_loading_percent=100.0,
    )
    net.trafo["psse_i"], net.trafo["psse_j"] = tr.i.to_numpy(), tr.j.to_numpy()
    net.trafo["psse_ckt"] = tr.ckt.to_numpy()
    # NMETR names the *non*-metered winding, unlike MET for other branches
    net.trafo["psse_metered"] = np.where(tr.nmetr == 1, tr.j, tr.i)
    net.trafo["winding1_is_hv"] = w1_hv
    for column, source in (("rate_a", "rata1"), ("rate_b", "ratb1"), ("rate_c", "ratc1")):
        net.trafo[f"{column}_mva"] = tr[source].to_numpy()
    assert len(idx) == len(tr)


def set_branch_status(net, element: str, index, in_service: bool) -> None:
    """Switch lines or transformers in or out of service, with the shunts of their line ends."""
    index = np.atleast_1d(index)
    net[element].loc[index, "in_service"] = in_service
    if element == "line" and "parent_line" in net.shunt:
        net.shunt.loc[net.shunt.parent_line.isin(index), "in_service"] = in_service


def line_end_shunts(net) -> pd.DataFrame:
    """Shunts that belong to line ends, with their parent line and end."""
    if "parent_line" not in net.shunt:
        return net.shunt.iloc[0:0]
    return net.shunt[net.shunt.parent_line.notna()]
