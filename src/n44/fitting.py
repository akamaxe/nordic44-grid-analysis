"""Hourly snapshots fitted to zonal market data, the way the dataset's toolbox built them.

The toolbox (``nordic44/n44.py``) needs PSS/E. This module repeats its steps with the solver
of ``n44.acpf``, so that any year with zonal data can be turned into snapshots
(data/README.md describes the original):

1. Each exchange load is set to the exchange on its link (load P = export), with Q at a
   fixed power factor: 0.99 for Skagerrak (bus 5610) and 0.95 for the others.
2. The desired interchange (PDES) of each area is its export on the borders that have
   branches. The two links carried by loads (SE3-FI, NO4-FI) are inside the areas already.
3. In each area the scalable loads are scaled by one factor to the zone's consumption, Q
   with P, and the machines in service by one factor to the zone's production.
4. The power flow runs with area interchange control and tap stepping. The machines at
   each area-slack bus absorb the difference until the area's interchange, measured as
   PSS/E measures it (``n44.ybus.area_interchange``), matches its PDES. The swing bus (3300
   in SE3) takes the losses, so the interchange of SE3 follows from the others. Each
   voltage-controlling transformer moves its ratio in steps of (RMA - RMI) / (NTP - 1)
   until its controlled bus is within VMI-VMA, or the ratio reaches a limit.
5. Each day starts from N44_BC, solved the same way at its own PDES, and each hour scales
   the solved previous hour. With ``chain=False`` each hour scales the day's starting state
   instead, so that what the area-slack buses absorb in one hour is not carried into the
   next. Data whose zone balances disagree with their flows need this (see fit_hours).

The interchange tolerance is 1 MW, tighter than PSS/E's 10 MW, and the taps move by the
number of steps the voltage error asks for, so where PSS/E stopped inside a tolerance or a
voltage band the two can settle on slightly different states.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass

import numpy as np
import pandas as pd

from n44 import acpf
from n44.io.psse_parser import RawCase
from n44.zones import (
    EXCHANGE_LOADS,
    EXTERNAL_LINKS,
    LOAD_LINKS,
    NEW_LINKS,
    NORDPOOL_BORDERS,
    ZONES,
)

EXCHANGE_TAN = float(np.tan(np.arccos(0.95)))  # Q/P of the exchange loads
EXCHANGE_TAN_OVERRIDE = {"5610_1": float(np.tan(np.arccos(0.99)))}  # Skagerrak
TOLERANCE_MW = 1.0
MAX_ROUNDS = 30


@dataclass
class ZoneInputs:
    """Hourly zonal data, one row per hour h (MW)."""

    consumption: pd.DataFrame  # columns: zones
    production: pd.DataFrame  # columns: zones
    exchange_loads: pd.DataFrame  # columns: "bus_id" of the exchange loads, load P
    pdes: pd.DataFrame  # columns: zones, export on the borders with branches

    def hour(self, h: int) -> tuple[pd.Series, pd.Series, pd.Series, pd.Series]:
        return (
            self.consumption.loc[h],
            self.production.loc[h],
            self.exchange_loads.loc[h],
            self.pdes.loc[h],
        )


def _nordpool_name(zone: str) -> str:
    return "FI" if zone == "FI1" else zone


def nordpool_inputs(
    consumption: pd.DataFrame, production: pd.DataFrame, exchange: pd.DataFrame
) -> ZoneInputs:
    """Inputs from the Nord Pool tables of the dataset, as the toolbox read them.

    Each area's PDES uses the exchange columns of its own country's file: they disagree in
    48 hours on SE1-FI, and the Finnish PDES follows the Finnish file.
    """
    loads = pd.DataFrame({f"{e.bus}_{e.load_id}": -exchange[e.nordpool] for e in EXCHANGE_LOADS})
    pdes = pd.DataFrame(0.0, index=exchange.index, columns=list(ZONES))
    for (a, b), column in NORDPOOL_BORDERS.items():
        if (a, b) in LOAD_LINKS:
            continue
        pdes[a] -= exchange[column]
        own = f"{_nordpool_name(b)}_{_nordpool_name(a)}"
        pdes[b] -= exchange[own] if own in exchange else -exchange[column]
    rename = {_nordpool_name(z): z for z in ZONES}
    return ZoneInputs(
        consumption[list(rename)].rename(columns=rename),
        production[list(rename)].rename(columns=rename),
        loads,
        pdes,
    )


def flow_inputs(consumption: pd.DataFrame, production: pd.DataFrame, flows: pd.DataFrame):
    """Inputs from zonal load and production and net border flows ("A->B" columns, MW).

    ``flows`` covers the borders between modelled zones and the links in
    ``zones.EXTERNAL_LINKS`` and ``zones.NEW_LINKS``. Several links on one exchange load add
    up, and the export on a link built after 2015 is added to its zone's consumption.
    """
    loads = pd.DataFrame(
        0.0, index=flows.index, columns=[f"{e.bus}_{e.load_id}" for e in EXCHANGE_LOADS]
    )
    for (zone, neighbour), (bus, load_id) in EXTERNAL_LINKS.items():
        loads[f"{bus}_{load_id}"] += flows[f"{zone}->{neighbour}"]
    for (a, b), (bus, load_id) in LOAD_LINKS.items():
        sending = f"{bus}_{load_id}"
        receiving = next(
            f"{e.bus}_{e.load_id}" for e in EXCHANGE_LOADS if e.link == f"{a}-{b}" and e.zone == b
        )
        loads[sending] += flows[f"{a}->{b}"]
        loads[receiving] -= flows[f"{a}->{b}"]
    pdes = pd.DataFrame(0.0, index=flows.index, columns=list(ZONES))
    for a, b in NORDPOOL_BORDERS:
        if (a, b) not in LOAD_LINKS:
            pdes[a] += flows[f"{a}->{b}"]
            pdes[b] -= flows[f"{a}->{b}"]
    consumption = consumption[list(ZONES)].copy()
    for zone, neighbour in NEW_LINKS:
        consumption[zone] += flows[f"{zone}->{neighbour}"]
    return ZoneInputs(consumption, production[list(ZONES)], loads, pdes)


def _zones(case: RawCase) -> pd.Series:
    """Zone of each bus number."""
    names = case.area.set_index("i").arname.str.strip()
    return case.bus.set_index("i").area.map(names)


def scale(
    case: RawCase,
    consumption: pd.Series,
    production: pd.Series,
    exchange_loads: pd.Series,
    pdes: pd.Series,
) -> RawCase:
    """Steps 1 to 3: a copy of ``case`` with exchange loads, PDES, loads and machines set."""
    load, gen = case.load, case.generator
    zone = _zones(case)
    key = (load.i.astype(str) + "_" + load.id.astype(str).str.strip()).to_numpy()
    pl, ql = load.pl.to_numpy(float).copy(), load.ql.to_numpy(float).copy()
    for name, p in exchange_loads.items():
        row = key == name
        pl[row] = p
        ql[row] = p * EXCHANGE_TAN_OVERRIDE.get(name, EXCHANGE_TAN)
    load_zone, gen_zone = load.i.map(zone).to_numpy(), gen.i.map(zone).to_numpy()
    scalable = ((load.scale == 1) & (load.status == 1)).to_numpy()
    on = (gen.stat == 1).to_numpy()
    pg, pt = gen.pg.to_numpy(float).copy(), gen.pt.to_numpy(float)
    for z in ZONES:
        rows = scalable & (load_zone == z)
        factor = consumption[z] / pl[rows].sum()
        pl[rows] *= factor
        ql[rows] *= factor
        units = on & (gen_zone == z)
        total = pg[units].sum()
        if abs(total) > 1e-6:
            pg[units] *= production[z] / total
        else:  # nothing to scale from: share by capacity
            pg[units] = production[z] * pt[units] / pt[units].sum()
    out = copy.copy(case)
    out.load, out.generator = load.assign(pl=pl, ql=ql), gen.assign(pg=pg)
    out.area = case.area.assign(pdes=case.area.arname.str.strip().map(pdes).to_numpy())
    return out


@dataclass
class FitResult:
    case: RawCase  # solved: machines, voltages and PDES of the hour
    converged: bool
    rounds: int  # power flows solved by the interchange control
    slack_shift_mw: pd.Series  # what each area-slack bus absorbed, by zone
    interchange_error_mw: float  # largest |interchange - PDES| left


def _tie_arrays(case: RawCase, grid: acpf.Grid):
    """Areas of the sending (metered) and receiving end of each branch, and tie mask."""
    br = grid.model.branches
    area = case.bus.set_index("i").area
    ai, aj = br.i.map(area).to_numpy(), br.j.map(area).to_numpy()
    metered_i = (br.metered == br.i).to_numpy()
    return metered_i, np.where(metered_i, ai, aj), np.where(metered_i, aj, ai), ai != aj


def _tap_steps(case: RawCase, grid: acpf.Grid, v: np.ndarray) -> pd.Series:
    """New WINDV1 of the transformers whose controlled bus is outside VMI-VMA."""
    tr = case.transformer
    control = tr[(tr.cod1 == 1) & (tr.stat == 1)]
    vm = np.abs(v[grid.model.positions(control.cont1.abs().to_numpy())])
    step = (control.rma1 - control.rmi1) / (control.ntp1 - 1)
    # a higher ratio at winding 1 lowers the voltage beyond it and raises it on its own side
    sense = np.where(control.cont1.abs() == control.i, 1.0, -1.0) * np.sign(control.cont1)
    error = np.where(
        vm > control.vma1, control.vma1 - vm, np.where(vm < control.vmi1, control.vmi1 - vm, 0.0)
    )
    steps = np.sign(error) * np.maximum(1, np.round(np.abs(error) / step))
    ratio = (control.windv1 + sense * steps * step).clip(control.rmi1, control.rma1)
    moved = (error != 0) & (np.abs(ratio - control.windv1) > 1e-9)
    return ratio[moved]


def solve_interchange(case: RawCase, v0: np.ndarray | None = None) -> tuple[FitResult, np.ndarray]:
    """Step 4: power flow with area interchange control and tap stepping on a scaled case.

    Returns the result and the complex voltages, to warm-start the next hour.
    """
    case = copy.copy(case)
    case.transformer = case.transformer.copy()
    grid = acpf.grid_from_case(case)
    sbase = grid.model.sbase
    areas = case.area.set_index("i")
    metered_i, sending, receiving, tie = _tie_arrays(case, grid)
    index = {a: k for k, a in enumerate(areas.index)}
    send_pos = np.array([index[a] for a in sending])
    recv_pos = np.array([index[a] for a in receiving])
    swing_bus = int(grid.model.buses[grid.slack])
    controlled = [a for a in areas.index if int(areas.isw[a]) != swing_bus]
    slack_pos = {a: int(grid.model.positions([int(areas.isw[a])])[0]) for a in controlled}
    pdes = areas.pdes.to_numpy(float)
    scaled_s = grid.s_spec.copy()  # injections of the scaled case; taps do not change them
    shift = dict.fromkeys(controlled, 0.0)
    v, rounds, error = v0, 0, np.inf
    while rounds < MAX_ROUNDS:
        rounds += 1
        sol = acpf.solve(grid, v0=v)
        if not sol.converged:
            return FitResult(case, False, rounds, pd.Series(shift), float("nan")), v0
        v = sol.v
        si, sj, _ = acpf.branch_flows(grid, v)
        p = np.where(metered_i, si.real, sj.real)
        export = np.zeros(len(areas))
        live = tie & ~np.isnan(p)
        np.add.at(export, send_pos[live], p[live])
        np.subtract.at(export, recv_pos[live], p[live])
        mismatch = export - pdes
        error = max(abs(mismatch[index[a]]) for a in controlled)
        taps = _tap_steps(case, grid, v)
        if error < TOLERANCE_MW and taps.empty:
            break
        if not taps.empty:
            case.transformer.loc[taps.index, "windv1"] = taps
            grid = acpf.grid_from_case(case)
        for a in controlled:
            shift[a] -= mismatch[index[a]]
            grid.s_spec[slack_pos[a]] = scaled_s[slack_pos[a]] + shift[a] / sbase
    solved = _write_solution(case, grid, v, shift)
    names = areas.arname.str.strip()
    result = FitResult(
        solved, error < TOLERANCE_MW, rounds, pd.Series(shift).rename(names), float(error)
    )
    return result, v


def _write_solution(case: RawCase, grid: acpf.Grid, v: np.ndarray, shift: dict) -> RawCase:
    """The case with the solved voltages, slack outputs and machine Q written in."""
    model, gen = grid.model, case.generator
    areas = case.area.set_index("i")
    bus_of = gen.i.to_numpy()
    on = (gen.stat == 1).to_numpy()
    pg, qg = gen.pg.to_numpy(float).copy(), gen.qg.to_numpy(float).copy()
    for a, mw in shift.items():
        units = on & (bus_of == int(areas.isw[a]))
        pg[units] += mw / units.sum()
    s_calc = v * np.conj(acpf.admittance(grid) @ v) * model.sbase
    load = case.load[case.load.status == 1]
    s_load = np.zeros(len(model.buses), complex)
    np.add.at(
        s_load, model.positions(load.i.to_numpy()), load.pl.to_numpy() + 1j * load.ql.to_numpy()
    )
    s_bus = s_calc + s_load  # what the machines of each bus produce
    count = np.zeros(len(model.buses))
    np.add.at(count, model.positions(bus_of[on]), 1.0)
    pos = model.positions(bus_of)
    share = np.where(on, s_bus[pos] / np.maximum(count[pos], 1), 0.0)
    swing = on & (pos == grid.slack)
    pg[swing] = share[swing].real
    qg[on] = share[on].imag
    out = copy.copy(case)
    out.generator = gen.assign(pg=pg, qg=qg)
    bus_pos = model.positions(case.bus.i.to_numpy())
    out.bus = case.bus.assign(vm=np.abs(v[bus_pos]), va=np.rad2deg(np.angle(v[bus_pos])))
    return out


def fit_hours(base: RawCase, inputs: ZoneInputs, hours, calendar: pd.DataFrame, chain: bool = True):
    """Steps 1 to 5 for ``hours`` (consecutive within each day), yielding (h, scaled, result).

    Each day starts again from ``base`` solved at its own PDES. Within a day each hour scales
    the solved previous one, as the toolbox did, or with ``chain=False`` the day's starting
    state. Chained, the area-slack corrections of one hour pass into the next through the
    proportional scaling. With consistent data they stay small, but where a zone's balance
    and its flows disagree by hundreds of megawatts they pile up, an area-slack machine runs
    negative, and the scaling then drives the zone's other machines far beyond their limits.
    The voltages of the previous hour remain the starting point of the power flow.
    """
    start, v_start = solve_interchange(base)
    if not start.converged:
        raise RuntimeError("the base case does not solve")
    case, v, day = start.case, v_start, None
    for h in map(int, hours):
        date = calendar.date[h]
        if date != day:
            case, v, day = start.case, v_start, date
        scaled = scale(case if chain else start.case, *inputs.hour(h))
        result, v_new = solve_interchange(scaled, v)
        if result.converged:
            case, v = result.case, v_new
        else:  # the next hour scales this one's unsolved state
            case = scaled
        yield h, scaled, result


def fit_job(job) -> tuple[dict, list[dict]]:
    """Fit a set of whole days in one process: the snapshot values and a row per hour.

    ``job`` is (base case RAW text, ZoneInputs, hours, year, chain).
    """
    from n44.io.psse_parser import parse_raw
    from n44.snapshots import calendar, values_from_case
    from n44.ybus import branch_flows
    from n44.zones import border_flows, bus_zones, cut_flows

    base_text, inputs, hours, year, chain = job
    base = parse_raw(base_text)
    zone = bus_zones(base.bus, base.area)
    values, rows = {}, []
    for h, _, result in fit_hours(base, inputs, hours, calendar(year), chain):
        row = {
            "h": h,
            "converged": result.converged,
            "rounds": result.rounds,
            "interchange_error_mw": result.interchange_error_mw,
            **{f"slack {z}": mw for z, mw in result.slack_shift_mw.items()},
        }
        if result.converged:
            case = result.case
            flows = branch_flows(case)
            load_p = case.load.set_index(["i", "id"]).pl
            gen = case.generator[case.generator.stat == 1]
            row.update(
                {
                    **{f"cut {k}": p for k, p in cut_flows(flows, zone).items()},
                    **{f"border {k}": p for k, p in border_flows(flows, zone, load_p).items()},
                    "losses_mw": float(flows.loss_mw.sum()),
                    "units_above_pmax": int((gen.pg > gen.pt + 0.5).sum()),
                    "units_below_zero": int((gen.pg < -0.5).sum()),
                    "vm_min": float(case.bus.vm.min()),
                    "vm_max": float(case.bus.vm.max()),
                }
            )
            values[h] = values_from_case(case)
        rows.append(row)
    return values, rows


def fit_year(base_text: str, inputs: ZoneInputs, year: int, workers: int = 3, chain: bool = True):
    """Fit every hour of ``year``; returns the snapshot tables and the table of hours."""
    from concurrent.futures import ProcessPoolExecutor

    from n44.snapshots import calendar, tables_from_values

    cal = calendar(year)
    days = np.array_split(np.unique(cal.date), workers * 6)
    jobs = [(base_text, inputs, cal.index[cal.date.isin(d)].to_numpy(), year, chain) for d in days]
    values, rows = {}, []
    with ProcessPoolExecutor(max_workers=workers) as pool:
        for part_values, part_rows in pool.map(fit_job, jobs):
            values.update(part_values)
            rows += part_rows
    return tables_from_values(values), pd.DataFrame(rows).set_index("h").sort_index()
