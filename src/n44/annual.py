"""Annual run: an AC power flow for every hour of 2015, on the snapshot values.

Each hour applies its snapshot values to a working copy of the base network and solves it
from a flat start, with the controls PSS/E settled on held fixed: taps, area-slack outputs
and voltage setpoints. The run therefore reproduces PSS/E's solution (the deviation is
recorded per hour) and adds what the RAW files do not contain: terminal branch flows and
loading, border and cut flows, and losses per zone. It adds no independent physics.

The hours are split into chunks solved in parallel processes. Every process loads the base
network and the snapshot tables once. Another folder of snapshot tables (another year, say)
can be given with ``directory``.
"""

from __future__ import annotations

import copy
from concurrent.futures import ProcessPoolExecutor
from functools import partial
from pathlib import Path

import numpy as np
import pandas as pd

from n44.config import configured_path
from n44.powerflow import dc_branch_flows, run_ac, run_dc
from n44.results import branch_flows, plant_reactive_power
from n44.snapshots import SnapshotStore, apply_values, base_net
from n44.zones import ZONES, border_flows, cut_flows

WIDE_TABLES = (
    "vm_pu",
    "va_degree",
    "p_i_mw",
    "q_i_mvar",
    "p_j_mw",
    "q_j_mvar",
    "loading_pct",
    "borders_mw",
    "cuts_mw",
    "zone_losses_mw",
    "borders_dc_mw",
)


def branch_names(flows: pd.DataFrame) -> list[str]:
    return [
        f"{k} {i}-{j}-{c}"
        for k, i, j, c in zip(flows.kind, flows.i, flows.j, flows.ckt, strict=True)
    ]


def zone_losses(flows: pd.DataFrame, zone: pd.Series) -> pd.Series:
    """Losses per zone: a branch inside a zone counts fully, a tie half on each side."""
    zi, zj = flows.i.map(zone), flows.j.map(zone)
    half = flows.loss_mw / 2
    losses = pd.concat([half.groupby(zi).sum(), half.groupby(zj).sum()]).groupby(level=0).sum()
    return losses.reindex(list(ZONES), fill_value=0.0)


def _psse_plant_q(values) -> pd.Series:
    live = values.gen_status == 1
    bus = values.gen_q.index.str.split("_").str[0].astype(int)
    return values.gen_q[live].groupby(bus[live]).sum()


class _WideTables:
    """Rows of the wide result tables, kept as plain arrays to save memory."""

    def __init__(self):
        self.columns: dict[str, list[str]] = {}
        self.rows: dict[str, dict[int, np.ndarray]] = {name: {} for name in WIDE_TABLES}

    def add(self, table: str, h: int, values, names) -> None:
        self.columns.setdefault(table, [str(n) for n in names])
        self.rows[table][h] = np.asarray(values, dtype=float)

    def frames(self) -> dict[str, pd.DataFrame]:
        out = {}
        for table, rows in self.rows.items():
            frame = pd.DataFrame(
                np.vstack(list(rows.values())) if rows else None,
                index=pd.Index(list(rows), name="h"),
                columns=self.columns.get(table),
            )
            out[table] = frame
        return out


def solve_hours(hours, directory: Path | None = None) -> dict[str, pd.DataFrame]:
    """Solve the given hours; returns the summary table and the wide result tables."""
    store, net = SnapshotStore(directory), base_net()
    zone = net.bus.zone
    summary, wide = [], _WideTables()
    for h in map(int, hours):
        values = store.values(h)
        apply_values(net, values)
        result = run_ac(net)
        record = {"h": h, "converged": result.converged, "init": result.init}
        load_p = net.load.set_index(["bus", "psse_id"]).p_mw
        if not result.converged:
            dc = copy.deepcopy(net)
            run_dc(dc)
            flows = dc_branch_flows(dc).assign(metered=lambda f: f.i, p_j_mw=lambda f: -f.p_i_mw)
            borders = border_flows(flows, zone, load_p)
            wide.add("borders_dc_mw", h, borders.to_numpy(), borders.index)
            summary.append(record)
            continue
        flows = branch_flows(net)
        names = branch_names(flows)
        for column, table in (
            ("p_i_mw", "p_i_mw"),
            ("q_i_mvar", "q_i_mvar"),
            ("p_j_mw", "p_j_mw"),
            ("q_j_mvar", "q_j_mvar"),
            ("loading_a_pct", "loading_pct"),
        ):
            wide.add(table, h, flows[column].to_numpy(), names)
        wide.add("vm_pu", h, net.res_bus.vm_pu.to_numpy(), net.res_bus.index)
        wide.add("va_degree", h, net.res_bus.va_degree.to_numpy(), net.res_bus.index)
        for table, series in (
            ("borders_mw", border_flows(flows, zone, load_p)),
            ("cuts_mw", cut_flows(flows, zone)),
            ("zone_losses_mw", zone_losses(flows, zone)),
        ):
            wide.add(table, h, series.to_numpy(), series.index)
        dq = (plant_reactive_power(net) - _psse_plant_q(values)).abs()
        loading = flows.loading_a_pct
        record.update(
            losses_mw=float(flows.loss_mw.sum()),
            slack_p_mw=float(net.res_ext_grid.p_mw.sum()),
            slack_p_psse_mw=float(net.ext_grid.psse_pg_mw.sum()),
            max_dvm_pu=float((net.res_bus.vm_pu - net.bus.psse_vm_pu).abs().max()),
            max_dva_deg=float((net.res_bus.va_degree - net.bus.psse_va_degree).abs().max()),
            max_dq_plant_mvar=float(dq.max()),
            n_q_limited=len(result.q_limited),
            q_limited=",".join(map(str, result.q_limited)),
            n_q_inconsistent=len(result.q_inconsistent),
            max_loading_pct=float(loading.max()),
            max_loading_branch=names[int(np.nanargmax(loading.to_numpy()))],
            n_overloaded=int((loading >= 100).sum()),
        )
        summary.append(record)
    return {"summary": pd.DataFrame(summary).set_index("h"), **wide.frames()}


def run_year(
    workers: int = 3, hours=range(8760), directory: Path | None = None
) -> dict[str, pd.DataFrame]:
    """Solve all hours in ``workers`` processes and merge the results."""
    chunks = [c for c in np.array_split(np.asarray(list(hours)), workers * 4) if len(c)]
    with ProcessPoolExecutor(max_workers=workers) as pool:
        parts = list(pool.map(partial(solve_hours, directory=directory), chunks))
    return {name: pd.concat([p[name] for p in parts]).sort_index() for name in parts[0]}


def pre_contingency_flows(names: list[str], table: pd.DataFrame) -> dict[str, np.ndarray]:
    """The annual run's AC flows as (hours x branches) arrays for N-1 screening.

    ``names`` and ``table`` come from ``n44.contingency.branch_table``. Returns p_i, p_j,
    q_i, q_j (MW, Mvar) in PSS/E orientation and the voltages v_i, v_j at both ends (p.u.).
    """
    folder = configured_path("results_dir") / "annual"
    pre = {
        key: pd.read_parquet(folder / f"{file}.parquet")[names].to_numpy()
        for key, file in (
            ("p_i", "p_i_mw"),
            ("p_j", "p_j_mw"),
            ("q_i", "q_i_mvar"),
            ("q_j", "q_j_mvar"),
        )
    }
    vm = pd.read_parquet(folder / "vm_pu.parquet")
    vm.columns = vm.columns.astype(int)
    pre["v_i"], pre["v_j"] = vm[table.i].to_numpy(), vm[table.j].to_numpy()
    return pre
