"""Hourly snapshots: what changes from hour to hour, stored as Parquet tables.

The static data of the model never changes in 2015: the level-A check found one parameter
signature, one status signature and one set of voltage setpoints over the 8,760 hours. Each
hour is therefore described by a few tables of values, with one row per snapshot ``h`` and
one column per element:

- bus voltages solved by PSS/E;
- load P and Q, and machine P and Q;
- transformer turns ratios chosen by PSS/E;
- area interchange targets.

Element statuses and voltage setpoints are stored as well, so a different year could not
silently change topology.

``load_snapshot(h)`` applies hour ``h`` to the base pandapower network
(data/processed/n44_base.json). In loops, ``apply_values`` updates a working network in place.
"""

from __future__ import annotations

from dataclasses import dataclass, fields
from functools import cache
from pathlib import Path

import numpy as np
import pandapower as pp
import pandas as pd

from n44.config import configured_path, load_config
from n44.io.build_net import set_branch_status
from n44.io.psse_parser import RawCase

CLOCK_SKIPPED = pd.Timestamp("2015-03-29")  # 02:00-03:00 does not exist
CLOCK_REPEATED = pd.Timestamp("2015-10-25")  # 02:00-03:00 occurs twice


def _key(*parts) -> list[str]:
    return ["_".join(map(str, p)) for p in zip(*parts, strict=True)]


def _branch_key(i, j, ckt) -> list[str]:
    return ["-".join(map(str, p)) for p in zip(i, j, ckt, strict=True)]


@dataclass
class SnapshotValues:
    """Values of one hour, indexed by PSS/E keys: bus number, "bus_id" or "i-j-ckt"."""

    bus_vm: pd.Series
    bus_va: pd.Series
    load_p: pd.Series
    load_q: pd.Series
    load_status: pd.Series
    gen_p: pd.Series
    gen_q: pd.Series
    gen_vs: pd.Series
    gen_status: pd.Series
    branch_status: pd.Series
    trafo_windv1: pd.Series
    trafo_windv2: pd.Series
    trafo_ang1: pd.Series
    trafo_status: pd.Series
    area_pdes: pd.Series


TABLES = tuple(f.name for f in fields(SnapshotValues))


def values_from_case(case: RawCase) -> SnapshotValues:
    bus, load, gen = case.bus, case.load, case.generator
    br, tr = case.branch, case.transformer
    load_key, gen_key = _key(load.i, load.id), _key(gen.i, gen.id)
    br_key, tr_key = _branch_key(br.i, br.j, br.ckt), _branch_key(tr.i, tr.j, tr.ckt)

    def series(values, index):
        return pd.Series(np.asarray(values), index=pd.Index(index))

    return SnapshotValues(
        bus_vm=series(bus.vm, bus.i),
        bus_va=series(bus.va, bus.i),
        load_p=series(load.pl, load_key),
        load_q=series(load.ql, load_key),
        load_status=series(load.status, load_key),
        gen_p=series(gen.pg, gen_key),
        gen_q=series(gen.qg, gen_key),
        gen_vs=series(gen.vs, gen_key),
        gen_status=series(gen.stat, gen_key),
        branch_status=series(br.st, br_key),
        trafo_windv1=series(tr.windv1, tr_key),
        trafo_windv2=series(tr.windv2, tr_key),
        trafo_ang1=series(tr.ang1, tr_key),
        trafo_status=series(tr.stat, tr_key),
        area_pdes=series(case.area.pdes, case.area.arname),
    )


def apply_values(net, values: SnapshotValues) -> None:
    """Write one hour's values into a network built by ``build_net`` (in place)."""
    load = net.load
    net.load["p_mw"] = values.load_p[load.name].to_numpy()
    net.load["q_mvar"] = values.load_q[load.name].to_numpy()
    net.load["in_service"] = values.load_status[load.name].to_numpy() == 1

    gen = net.gen
    net.gen["p_mw"] = values.gen_p[gen.name].to_numpy()
    net.gen["vm_pu"] = values.gen_vs[gen.name].to_numpy()
    net.gen["in_service"] = values.gen_status[gen.name].to_numpy() == 1
    net.gen["psse_qg_mvar"] = values.gen_q[gen.name].to_numpy()

    for k in net.ext_grid.index:
        units = [f"{net.ext_grid.bus[k]}_{m}" for m in net.ext_grid.psse_machines[k].split(",")]
        net.ext_grid.loc[k, "vm_pu"] = values.gen_vs[units].iloc[0]
        net.ext_grid.loc[k, "va_degree"] = values.bus_va[net.ext_grid.bus[k]]
        net.ext_grid.loc[k, "psse_pg_mw"] = values.gen_p[units].sum()
        net.ext_grid.loc[k, "psse_qg_mvar"] = values.gen_q[units].sum()

    trafo = net.trafo
    vb = net.bus.vn_kv
    vi, vj = vb[trafo.psse_i].to_numpy(), vb[trafo.psse_j].to_numpy()
    t1 = values.trafo_windv1[trafo.name].to_numpy()
    t2 = values.trafo_windv2[trafo.name].to_numpy()
    w1_hv = trafo.winding1_is_hv.to_numpy()
    net.trafo["vn_hv_kv"] = np.where(w1_hv, t1 * vi, t2 * vj)
    net.trafo["vn_lv_kv"] = np.where(w1_hv, t2 * vj, t1 * vi)
    ang = values.trafo_ang1[trafo.name].to_numpy()
    net.trafo["shift_degree"] = np.where(w1_hv, ang, -ang)

    line_on = values.branch_status[net.line.name].to_numpy() == 1
    set_branch_status(net, "line", net.line.index[line_on], True)
    set_branch_status(net, "line", net.line.index[~line_on], False)
    net.trafo["in_service"] = values.trafo_status[trafo.name].to_numpy() == 1

    net.bus["psse_vm_pu"] = values.bus_vm[net.bus.index].to_numpy()
    net.bus["psse_va_degree"] = values.bus_va[net.bus.index].to_numpy()


def calendar(year: int | None = None) -> pd.DataFrame:
    """One row per snapshot h: date, hour, local time and UTC time.

    The hour in a file name is the Nord Pool row index of the day (the toolbox took the
    first 24 rows), which shapes the two clock-change days:

    - On 2015-03-29, h2 is the skipped hour 02:00-03:00. Nord Pool interpolated it, so
      ``synthetic`` is True and the UTC time is NaT.
    - On 2015-10-25, h2 is the first 02:00 (summer time) and h3 the second (winter time).
      Hours 4-23 are local 03:00-22:00, and 23:00-24:00 has no snapshot.
    """
    year = year or load_config()["dataset"]["year"]
    tz = load_config()["dataset"]["timezone"]
    days = pd.date_range(f"{year}-01-01", f"{year}-12-31", freq="D")
    cal = pd.DataFrame({"date": np.repeat(days, 24), "hour": np.tile(np.arange(24), len(days))})
    cal.index = pd.RangeIndex(len(cal), name="h")
    clock_hour = cal.hour.copy()
    repeated = cal.date == CLOCK_REPEATED
    clock_hour[repeated & (cal.hour >= 3)] -= 1
    cal["local_time"] = cal.date + pd.to_timedelta(clock_hour, unit="h")
    cal["synthetic"] = (cal.date == CLOCK_SKIPPED) & (cal.hour == 2)
    # first 02:00 of the repeated day is summer time (DST), the second winter time
    ambiguous = np.where(repeated & (cal.hour == 2), True, False)
    local = cal.local_time.dt.tz_localize(tz, ambiguous=ambiguous, nonexistent="NaT")
    cal["utc_time"] = local.dt.tz_convert("UTC")
    cal["month"] = cal.date.dt.month
    return cal


def build_tables(texts) -> dict[str, pd.DataFrame]:
    """Snapshot tables from an iterable of (h, after_PF text)."""
    from n44.io.psse_parser import parse_raw

    rows = {name: {} for name in TABLES}
    for h, text in texts:
        values = values_from_case(parse_raw(text))
        for name in TABLES:
            rows[name][h] = getattr(values, name)
    tables = {}
    for name in TABLES:
        frame = pd.DataFrame.from_dict(rows[name], orient="index").sort_index()
        frame.index.name = "h"
        frame.columns = frame.columns.astype(str)
        tables[name] = frame
    return tables


def snapshot_dir() -> Path:
    return configured_path("processed_dir") / "snapshots"


def save_tables(tables: dict[str, pd.DataFrame], directory: Path | None = None) -> None:
    directory = directory or snapshot_dir()
    directory.mkdir(parents=True, exist_ok=True)
    for name, frame in tables.items():
        frame.to_parquet(directory / f"{name}.parquet")


class SnapshotStore:
    """Read access to the snapshot tables written by scripts/05_build_snapshots.py."""

    def __init__(self, directory: Path | None = None):
        self.directory = directory or snapshot_dir()
        missing = [n for n in TABLES if not (self.directory / f"{n}.parquet").exists()]
        if missing:
            raise FileNotFoundError(
                f"snapshot tables missing in {self.directory}: {missing}; "
                "run scripts/05_build_snapshots.py"
            )
        self.tables = {n: pd.read_parquet(self.directory / f"{n}.parquet") for n in TABLES}
        for name in ("bus_vm", "bus_va"):
            self.tables[name].columns = self.tables[name].columns.astype(int)

    def values(self, h: int) -> SnapshotValues:
        return SnapshotValues(**{name: frame.loc[h] for name, frame in self.tables.items()})


@cache
def _base_net_json() -> str:
    return (configured_path("processed_dir") / "n44_base.json").read_text(encoding="utf-8")


def base_net():
    """A fresh copy of the base network (data/processed/n44_base.json)."""
    return pp.from_json_string(_base_net_json())


def load_snapshot(h: int, store: SnapshotStore | None = None, net=None):
    """The base network with the values of snapshot ``h`` applied.

    Pass ``net`` to update a working network in place instead of loading a fresh copy.
    """
    store = store or SnapshotStore()
    net = base_net() if net is None else net
    apply_values(net, store.values(h))
    return net
