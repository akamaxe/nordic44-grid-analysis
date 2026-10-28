"""Future scenarios, defined in config/scenarios/*.yaml.

A scenario changes every hourly case before it is solved:
- ``add_lines``: new branches, copies of an existing circuit;
- ``hvdc``: point-to-point VSC links. In the AC model each converter is a machine that
  holds its bus voltage within its reactive limit; the redispatch chooses the active
  set-point each hour (``n44.redispatch``, ``links``). The DC model leaves them out: a link
  does not change the AC topology;
- ``add_load``: new demand at given buses, whose energy the generators of the given zones
  produce, in proportion to their output.
"""

from __future__ import annotations

import copy
from functools import cache

import numpy as np
import pandas as pd
import yaml

from n44.config import repo_path
from n44.io.psse_parser import RawCase


@cache
def load_scenario(name: str) -> dict:
    with open(repo_path("config", "scenarios", f"{name}.yaml"), encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def links(scenario: dict) -> list[tuple[int, int, float]]:
    """The scenario's HVDC links as (from bus, to bus, capacity MW)."""
    return [(h["from"], h["to"], float(h["capacity_mw"])) for h in scenario.get("hvdc", [])]


def modify_case(
    case: RawCase,
    scenario: dict,
    load_mw: float | None = None,
    set_points: np.ndarray | None = None,
    wind_mw: float | None = None,
) -> RawCase:
    """A copy of ``case`` with the scenario applied.

    ``load_mw`` and ``wind_mw`` pick one value of the ``add_load`` and ``add_wind`` sweeps.
    New wind is a fixed injection (a negative load at unity power factor). The difference
    between new load and new wind is produced, or displaced, by the generators of the
    ``supplied_by`` zones in proportion to their output. HVDC converters are added only when
    ``set_points`` (MW per link, from the first bus to the second) are given.
    """
    out = copy.copy(case)
    for name in ("bus", "branch", "load", "generator"):
        setattr(out, name, getattr(case, name).copy())

    for line in scenario.get("add_lines", []):
        i, j, ckt = line["copy"]
        source = out.branch[(out.branch.i == i) & (out.branch.j == j) & (out.branch.ckt == ckt)]
        if len(source) != 1:
            raise ValueError(f"circuit {i}-{j}-{ckt} to copy not found")
        new = source.assign(ckt=str(line["ckt"]))
        out.branch = pd.concat([out.branch, new], ignore_index=True)

    spec = scenario.get("add_load")
    if spec and load_mw:
        tan = np.tan(np.arccos(spec["power_factor"]))
        rows = []
        for k, (bus, share) in enumerate(spec["buses"].items()):
            rows.append(
                _load_row(out, int(bus), f"N{k + 1}", load_mw * share, load_mw * share * tan)
            )
        wind = scenario.get("add_wind")
        if wind and wind_mw:
            for k, (bus, share) in enumerate(wind["buses"].items()):
                rows.append(_load_row(out, int(bus), f"W{k + 1}", -wind_mw * share, 0.0))
        out.load = pd.concat([out.load, *rows], ignore_index=True)
        # the generators of the supplying zones produce (or give up) the balance
        balance = load_mw - (wind_mw or 0.0)
        zone = out.bus.set_index("i").area.map(out.area.set_index("i").arname.str.strip())
        gen = out.generator
        supplying = (gen.stat == 1) & gen.i.map(zone).isin(spec["supplied_by"]) & (gen.pg > 0)
        share = gen.pg.where(supplying, 0.0) / gen.pg.where(supplying, 0.0).sum()
        new_pg = np.clip(gen.pg + balance * share, gen.pb.clip(lower=0), gen.pt)
        out.generator = gen.assign(pg=new_pg)

    if set_points is not None:
        rows = []
        for spec, p in zip(scenario.get("hvdc", []), set_points, strict=True):
            for bus, power in ((spec["from"], -p), (spec["to"], p * (1 - spec["losses"]))):
                template = out.generator.iloc[[0]]
                rows.append(
                    template.assign(
                        i=int(bus),
                        id="DC",
                        pg=power,
                        qg=0.0,
                        qt=spec["q_limit_mvar"],
                        qb=-spec["q_limit_mvar"],
                        vs=spec["v_set_pu"],
                        ireg=0,
                        mbase=spec["capacity_mw"],
                        stat=1,
                        pt=spec["capacity_mw"],
                        pb=-spec["capacity_mw"],
                    )
                )
                out.bus.loc[(out.bus.i == bus) & (out.bus.ide == 1), "ide"] = 2
        out.generator = pd.concat([out.generator, *rows], ignore_index=True)
        # a converter at a voltage-controlled bus must share that bus's set-point
        vs = out.generator.groupby("i").vs.transform("first")
        out.generator["vs"] = vs
    return out


def _load_row(case: RawCase, bus: int, load_id: str, p_mw: float, q_mvar: float) -> pd.DataFrame:
    """A load record at ``bus``, with the area, zone and owner of an existing load there."""
    template = case.load[case.load.i == bus]
    template = template.iloc[[0]] if len(template) else case.load.iloc[[0]].assign(i=bus)
    return template.assign(id=load_id, pl=p_mw, ql=q_mvar, status=1, scale=1)
