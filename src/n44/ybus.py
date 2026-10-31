"""Reference network model built straight from the PSS/E data.

This module follows the PSS/E 33 equations and does not use pandapower. It therefore checks
two different things:

- the reading of the data, through the power balance at PSS/E's own solution;
- the pandapower network built from the same file, which must have the same admittance
  matrix.

Values are per unit on SBASE and on the bus base voltages. Every branch is a two-port with
admittances ``yff, yft, ytf, ytt`` (from = bus I, to = bus J):

- Non-transformer branch: series admittance ``y = 1/(R + jX)``, half the charging ``B/2``
  at each end, and the line shunts ``GI + jBI`` at bus I and ``GJ + jBJ`` at bus J.
- Two-winding transformer (CW = CZ = CM = 1, no magnetizing admittance): ratio
  ``t1 = WINDV1 e^(j ANG1)`` at bus I, series ``y = 1/(R1-2 + jX1-2)`` and ratio ``t2 = WINDV2``
  at bus J. This gives ``yff = y/|t1|^2``, ``yft = -y/(conj(t1) t2)``, ``ytf = -y/(t1 t2)`` and
  ``ytt = y/t2^2``.

Flows computed from these two-ports are *terminal* flows, so they include the line shunts.
Loading follows PSS/E. With NXFRAT > 0, line ratings are current expressed as MVA, so loading
is ``|I| * SBASE / RATE``. With XFRRAT <= 0, transformer ratings are MVA, so loading is
``|S| / RATE``. Both are taken at the more loaded end.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from n44.io.psse_parser import RawCase

# Largest voltage error from rounding in a RAW file: VM has 5 decimals and VA has 4 decimals
# in degrees.
ROUNDING_DV = 5e-6 + np.deg2rad(5e-5)
# PSS/E default convergence tolerance, MVA
PSSE_TOLERANCE_MVA = 0.1


def check_supported(case: RawCase) -> None:
    """Raise if the case holds data this reference model does not represent."""
    if not case.transformer3.empty:
        raise NotImplementedError("three-winding transformers are not supported")
    tr = case.transformer
    odd = tr[
        (tr.cw != 1)
        | (tr.cz != 1)
        | (tr.cm != 1)
        | (tr.mag1 != 0)
        | (tr.mag2 != 0)
        | (tr.nomv1 != 0)
        | (tr.nomv2 != 0)
    ]
    if not odd.empty:
        raise NotImplementedError(
            "only transformers with CW = CZ = CM = 1, NOMV = 0 and no magnetizing admittance "
            f"are supported; found {odd[['i', 'j', 'ckt']].values.tolist()}"
        )
    if (case.load[["ip", "iq", "yp", "yq"]] != 0).to_numpy().any():
        raise NotImplementedError("only constant-power loads are supported")


def branch_table(case: RawCase) -> pd.DataFrame:
    """All branches as two-ports: non-transformer branches first, then transformers.

    Columns: ``kind``, ``i``, ``j``, ``ckt``, ``status``, ``metered`` (bus number of the
    metered end), ``rate_a``, ``rate_b``, ``rate_c``, ``rating_basis`` ("current" or
    "power"), ``yff``, ``yft``, ``ytf``, ``ytt``.
    """
    br = case.branch
    y = 1.0 / (br.r.to_numpy() + 1j * br.x.to_numpy())
    half_b = 0.5j * br.b.to_numpy()
    lines = pd.DataFrame(
        {
            "kind": "line",
            "i": br.i,
            "j": br.j,
            "ckt": br.ckt,
            "status": br.st,
            "metered": np.where(br.met == 2, br.j, br.i),
            "rate_a": br.ratea,
            "rate_b": br.rateb,
            "rate_c": br.ratec,
            "rating_basis": "current" if case.nxfrat > 0 else "power",
            "yff": y + half_b + (br.gi.to_numpy() + 1j * br.bi.to_numpy()),
            "yft": -y,
            "ytf": -y,
            "ytt": y + half_b + (br.gj.to_numpy() + 1j * br.bj.to_numpy()),
        }
    )
    tr = case.transformer
    y = 1.0 / (tr.r1_2.to_numpy() + 1j * tr.x1_2.to_numpy())
    t1 = tr.windv1.to_numpy() * np.exp(1j * np.deg2rad(tr.ang1.to_numpy()))
    t2 = tr.windv2.to_numpy()
    trafos = pd.DataFrame(
        {
            "kind": "transformer",
            "i": tr.i,
            "j": tr.j,
            "ckt": tr.ckt,
            "status": tr.stat,
            # NMETR names the *non*-metered winding, unlike MET for other branches
            "metered": np.where(tr.nmetr == 1, tr.j, tr.i),
            "rate_a": tr.rata1,
            "rate_b": tr.ratb1,
            "rate_c": tr.ratc1,
            "rating_basis": "current" if case.xfrrat > 0 else "power",
            "yff": y / np.abs(t1) ** 2,
            "yft": -y / (np.conj(t1) * t2),
            "ytf": -y / (t1 * t2),
            "ytt": y / t2**2,
        }
    )
    return pd.concat([lines, trafos], ignore_index=True)


@dataclass
class ReferenceModel:
    """Admittance matrix of a case, with buses in ascending number order."""

    buses: np.ndarray  # bus numbers
    ybus: np.ndarray  # dense complex matrix, per unit
    branches: pd.DataFrame  # branch_table(case)
    sbase: float

    def positions(self, numbers) -> np.ndarray:
        return np.searchsorted(self.buses, np.asarray(numbers))


def build_reference(case: RawCase) -> ReferenceModel:
    check_supported(case)
    buses = np.sort(case.bus.i.to_numpy())
    branches = branch_table(case)
    model = ReferenceModel(buses, np.zeros((len(buses),) * 2, complex), branches, case.sbase)
    live = branches[branches.status == 1]
    f, t = model.positions(live.i), model.positions(live.j)
    for rows, cols, values in (
        (f, f, live.yff),
        (f, t, live.yft),
        (t, f, live.ytf),
        (t, t, live.ytt),
    ):
        np.add.at(model.ybus, (rows, cols), values.to_numpy())
    fs = case.fixed_shunt[case.fixed_shunt.status == 1]
    np.add.at(
        model.ybus,
        (model.positions(fs.i),) * 2,
        (fs.gl.to_numpy() + 1j * fs.bl.to_numpy()) / case.sbase,
    )
    ss = case.switched_shunt[case.switched_shunt.stat == 1]
    np.add.at(model.ybus, (model.positions(ss.i),) * 2, 1j * ss.binit.to_numpy() / case.sbase)
    return model


def bus_voltages(case: RawCase, model: ReferenceModel) -> np.ndarray:
    """Complex bus voltages stored in the case (the PSS/E solution for after_PF files)."""
    bus = case.bus.set_index("i").loc[model.buses]
    return bus.vm.to_numpy() * np.exp(1j * np.deg2rad(bus.va.to_numpy()))


def specified_injections(case: RawCase, model: ReferenceModel) -> np.ndarray:
    """Net injection per bus from in-service machines and loads, in MW + j Mvar."""
    s = np.zeros(len(model.buses), complex)
    gen = case.generator[case.generator.stat == 1]
    np.add.at(s, model.positions(gen.i), gen.pg.to_numpy() + 1j * gen.qg.to_numpy())
    load = case.load[case.load.status == 1]
    np.subtract.at(s, model.positions(load.i), load.pl.to_numpy() + 1j * load.ql.to_numpy())
    return s


def power_mismatch(case: RawCase, model: ReferenceModel | None = None) -> pd.DataFrame:
    """Power balance at the voltages stored in the case, per bus.

    ``bound_mva`` is the largest mismatch the rounding of the stored voltages and the PSS/E
    convergence tolerance can explain. In a correctly read, solved case every bus satisfies
    ``ds_mva <= bound_mva``.
    """
    model = model or build_reference(case)
    v = bus_voltages(case, model)
    current = model.ybus @ v
    mismatch = specified_injections(case, model) - v * np.conj(current) * case.sbase
    bound = PSSE_TOLERANCE_MVA + ROUNDING_DV * case.sbase * (
        np.abs(model.ybus).sum(axis=1) + np.abs(current)
    )
    return pd.DataFrame(
        {
            "dp_mw": mismatch.real,
            "dq_mvar": mismatch.imag,
            "ds_mva": np.abs(mismatch),
            "bound_mva": bound,
        },
        index=pd.Index(model.buses, name="bus"),
    )


def branch_flows(
    case: RawCase, model: ReferenceModel | None = None, voltages: np.ndarray | None = None
) -> pd.DataFrame:
    """Terminal flows of the in-service branches, in MW and Mvar, with PSS/E-style loading.

    Uses the voltages stored in the case unless ``voltages`` (ordered as ``model.buses``) are
    given.
    """
    model = model or build_reference(case)
    v = bus_voltages(case, model) if voltages is None else voltages
    br = model.branches[model.branches.status == 1].reset_index(drop=True)
    vi, vj = v[model.positions(br.i)], v[model.positions(br.j)]
    ii = br.yff.to_numpy() * vi + br.yft.to_numpy() * vj
    ij = br.ytf.to_numpy() * vi + br.ytt.to_numpy() * vj
    si, sj = vi * np.conj(ii) * case.sbase, vj * np.conj(ij) * case.sbase
    rate = br.rate_a.to_numpy()
    with np.errstate(divide="ignore", invalid="ignore"):
        by_current = np.maximum(np.abs(ii), np.abs(ij)) * case.sbase / rate * 100
        by_power = np.maximum(np.abs(si), np.abs(sj)) / rate * 100
    loading = np.where(br.rating_basis == "current", by_current, by_power)
    return pd.DataFrame(
        {
            "kind": br.kind,
            "i": br.i,
            "j": br.j,
            "ckt": br.ckt,
            "metered": br.metered,
            "p_i_mw": si.real,
            "q_i_mvar": si.imag,
            "p_j_mw": sj.real,
            "q_j_mvar": sj.imag,
            "loss_mw": si.real + sj.real,
            "loading_a_pct": np.where(rate > 0, loading, np.nan),
        }
    )


def area_interchange(case: RawCase, flows: pd.DataFrame) -> pd.Series:
    """Net export of each PSS/E area, measured as PSS/E does, in MW.

    Each tie branch counts with its flow at the metered end, so its losses fall on the area at
    the other end. Loads assigned to an area other than their bus's area count as
    interchange, as in PSS/E's "tie lines and loads" mode.
    """
    area = case.bus.set_index("i").area
    export = pd.Series(0.0, index=pd.Index(sorted(case.area.i), name="area"))
    ai, aj = flows.i.map(area).to_numpy(), flows.j.map(area).to_numpy()
    tie = ai != aj
    metered_i = (flows.metered == flows.i).to_numpy()
    p = np.where(metered_i, flows.p_i_mw, flows.p_j_mw)
    sending = np.where(metered_i, ai, aj)
    receiving = np.where(metered_i, aj, ai)
    np.add.at(export.values, export.index.get_indexer(sending[tie]), p[tie])
    np.subtract.at(export.values, export.index.get_indexer(receiving[tie]), p[tie])
    load = case.load[case.load.status == 1]
    away = load[load.area.to_numpy() != load.i.map(area).to_numpy()]
    np.add.at(export.values, export.index.get_indexer(away.i.map(area)), away.pl.to_numpy())
    np.subtract.at(export.values, export.index.get_indexer(away.area), away.pl.to_numpy())
    return export
