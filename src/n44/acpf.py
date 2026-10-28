"""Newton-Raphson AC power flow on the PSS/E reference model, built for N-1 sweeps.

pandapower needs about 90 ms per contingency on N44, nearly all of it spent converting
tables; the numerics of a 44-bus case take well under a millisecond. This solver works
directly on the admittance matrix of ``n44.ybus``, which is identical to pandapower's
(validation level B):

- an outaged branch is removed with a rank-2 update of Ybus, so a line goes out together
  with its line shunts, as in PSS/E and in ``n44.io.build_net.set_branch_status``;
- reactive limits are applied as pandapower applies them: solve with every plant on voltage
  control, move all plants beyond a limit to PQ at that limit, solve again, and repeat. A
  plant is never switched back, and the swing bus has no reactive limit.

It is checked against pandapower in tests/test_acpf.py and, case by case, against
pandapower's contingency runs in scripts/11_n1_contingency.py.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from n44.config import load_config
from n44.io.psse_parser import RawCase
from n44.ybus import ReferenceModel, build_reference

DIVERGED = 100.0  # mismatch in p.u. (100 GW at SBASE = 1000 MVA): stop iterating


@dataclass
class Grid:
    """One hour of the reference model, ready for repeated solves (per unit on SBASE)."""

    model: ReferenceModel
    names: list[str]  # branch names, "line 3000-3115-1", in model.branches order
    slack: int  # position of the swing bus
    pv: np.ndarray  # positions of buses whose machines control the voltage
    pq: np.ndarray
    s_spec: np.ndarray  # active generation minus load (P + jQ); Q of PV and slack unused
    q_load: np.ndarray
    q_max: np.ndarray  # plant limits per bus
    q_min: np.ndarray
    v_start: np.ndarray  # flat start with setpoints at PV and swing buses
    # branch arrays, model.branches order
    f: np.ndarray  # positions of bus I and bus J
    t: np.ndarray
    y: np.ndarray  # (branches, 4): yff, yft, ytf, ytt
    rate_a: np.ndarray
    current_based: np.ndarray  # loading by current (lines) or by MVA (transformers)
    live: np.ndarray  # in service in the case
    y_end: np.ndarray  # (branches, 2): line shunts GI + jBI at bus I, GJ + jBJ at bus J


@dataclass
class AcSolution:
    converged: bool
    v: np.ndarray | None = None  # complex bus voltages, model.buses order
    limited: list[int] = field(default_factory=list)  # buses moved to PQ at a reactive limit
    iterations: int = 0
    # limited plants whose voltage contradicts their limit (above set-point at Qmax, below at
    # Qmin): a solver that could switch plants back to voltage control would have done so
    inconsistent: list[int] = field(default_factory=list)


def grid_from_case(case: RawCase) -> Grid:
    model = build_reference(case)
    n, pos = len(model.buses), model.positions
    gen = case.generator[case.generator.stat == 1]
    load = case.load[case.load.status == 1]
    s = np.zeros(n, complex)
    np.add.at(s, pos(gen.i), gen.pg.to_numpy())
    np.subtract.at(s, pos(load.i), load.pl.to_numpy() + 1j * load.ql.to_numpy())
    q_load = np.zeros(n)
    np.add.at(q_load, pos(load.i), load.ql.to_numpy())
    q_max, q_min = np.zeros(n), np.zeros(n)
    np.add.at(q_max, pos(gen.i), gen.qt.to_numpy())
    np.add.at(q_min, pos(gen.i), gen.qb.to_numpy())

    bus = case.bus.set_index("i").loc[model.buses]
    ide = bus.ide.to_numpy()
    controlled = np.isin(model.buses, gen.i.to_numpy())
    slack = int(np.flatnonzero(ide == 3)[0])
    pv = np.flatnonzero((ide == 2) & controlled)
    pq = np.flatnonzero(~np.isin(np.arange(n), np.r_[pv, slack]))
    v_set = gen.groupby("i").vs.first()
    vm = np.ones(n)
    vm[np.r_[pv, slack]] = v_set[model.buses[np.r_[pv, slack]]].to_numpy()
    va = np.zeros(n)
    va[slack] = np.deg2rad(bus.va.to_numpy()[slack])
    br = model.branches
    names = [f"{k} {i}-{j}-{c}" for k, i, j, c in zip(br.kind, br.i, br.j, br.ckt, strict=True)]
    sbase = case.sbase
    return Grid(
        model=model,
        names=names,
        slack=slack,
        pv=pv,
        pq=pq,
        s_spec=s / sbase,
        q_load=q_load / sbase,
        q_max=q_max / sbase,
        q_min=q_min / sbase,
        v_start=vm * np.exp(1j * va),
        f=pos(br.i),
        t=pos(br.j),
        y=br[["yff", "yft", "ytf", "ytt"]].to_numpy(),
        rate_a=br.rate_a.to_numpy(),
        current_based=(br.rating_basis == "current").to_numpy(),
        live=(br.status == 1).to_numpy(),
        y_end=np.r_[
            np.c_[
                case.branch.gi.to_numpy() + 1j * case.branch.bi.to_numpy(),
                case.branch.gj.to_numpy() + 1j * case.branch.bj.to_numpy(),
            ],
            np.zeros((len(case.transformer), 2), complex),
        ],
    )


def admittance(grid: Grid, outage: int | None = None) -> np.ndarray:
    """Ybus of the grid, without branch ``outage`` (a row of ``grid.model.branches``)."""
    ybus = grid.model.ybus
    if outage is None:
        return ybus
    f, t = grid.f[outage], grid.t[outage]
    yff, yft, ytf, ytt = grid.y[outage]
    ybus = ybus.copy()
    ybus[f, f] -= yff
    ybus[f, t] -= yft
    ybus[t, f] -= ytf
    ybus[t, t] -= ytt
    return ybus


def _newton(ybus, v, s_spec, pv, pq, tol, max_iter) -> tuple[np.ndarray, bool, int]:
    """Polar Newton-Raphson (MATPOWER's formulation) with a dense Jacobian."""
    pvpq = np.r_[pv, pq]
    npvpq = len(pvpq)
    vm, va = np.abs(v), np.angle(v)
    for iteration in range(max_iter + 1):
        current = ybus @ v
        mismatch = v * np.conj(current) - s_spec
        f = np.r_[mismatch.real[pvpq], mismatch.imag[pq]]
        worst = np.max(np.abs(f))
        if worst < tol:
            return v, True, iteration
        if not np.isfinite(worst) or worst > DIVERGED:
            return v, False, iteration
        if iteration == max_iter:
            break
        vnorm = v / vm
        ds_dvm = v[:, None] * np.conj(ybus * vnorm[None, :])
        ds_dvm[np.diag_indices_from(ds_dvm)] += np.conj(current) * vnorm
        ds_dva = -1j * v[:, None] * np.conj(ybus * v[None, :])
        ds_dva[np.diag_indices_from(ds_dva)] += 1j * v * np.conj(current)
        jac = np.block(
            [
                [ds_dva.real[np.ix_(pvpq, pvpq)], ds_dvm.real[np.ix_(pvpq, pq)]],
                [ds_dva.imag[np.ix_(pq, pvpq)], ds_dvm.imag[np.ix_(pq, pq)]],
            ]
        )
        try:
            dx = np.linalg.solve(jac, -f)
        except np.linalg.LinAlgError:  # e.g. an outage that isolates a bus
            return v, False, iteration
        va[pvpq] += dx[:npvpq]
        vm[pq] += dx[npvpq:]
        v = vm * np.exp(1j * va)
    return v, False, max_iter


def solve(
    grid: Grid,
    outage: int | None = None,
    v0: np.ndarray | None = None,
    enforce_q_lims: bool | None = None,
    max_iter: int | None = None,
) -> AcSolution:
    """AC power flow, from ``v0`` (default: flat start with setpoints) and with reactive limits.

    Setpoint magnitudes at PV and swing buses, and the swing angle, are always taken from the
    grid, whatever ``v0`` holds.
    """
    cfg = load_config()["powerflow"]
    q_lims = cfg["enforce_q_lims"] if enforce_q_lims is None else enforce_q_lims
    tol = cfg["tolerance_mva"] / grid.model.sbase
    max_iter = max_iter or cfg["max_iteration"]
    ybus = admittance(grid, outage)
    v = grid.v_start.copy() if v0 is None else v0.astype(complex)
    fixed = np.r_[grid.pv, grid.slack]
    v[fixed] = np.abs(grid.v_start[fixed]) * np.exp(1j * np.angle(v[fixed]))
    v[grid.slack] = grid.v_start[grid.slack]
    s = grid.s_spec.copy()
    pv, pq, limited, total = grid.pv.copy(), grid.pq.copy(), [], 0
    while True:
        v, converged, iterations = _newton(ybus, v, s, pv, pq, tol, max_iter)
        total += iterations
        if not converged:
            return AcSolution(False, iterations=total)
        if not q_lims:
            break
        q_gen = np.imag(v * np.conj(ybus @ v))[pv] + grid.q_load[pv]
        over, under = q_gen > grid.q_max[pv], q_gen < grid.q_min[pv]
        if not (over | under).any():
            break
        hit = pv[over | under]
        q_fixed = np.where(over, grid.q_max[pv], grid.q_min[pv])[over | under]
        s[hit] = s[hit].real + 1j * (q_fixed - grid.q_load[hit])
        pv, pq = pv[~(over | under)], np.r_[pq, hit]
        limited += [int(b) for b in grid.model.buses[hit]]
    return AcSolution(True, v, limited, total, _inconsistent(grid, v, limited, ybus))


def _inconsistent(grid: Grid, v: np.ndarray, limited: list[int], ybus: np.ndarray) -> list[int]:
    """Limited plants held at Qmax above their voltage set-point, or at Qmin below it."""
    if not limited:
        return []
    pos = grid.model.positions(limited)
    q_gen = np.imag(v * np.conj(ybus @ v))[pos] + grid.q_load[pos]
    at_max = q_gen >= grid.q_max[pos] - 1e-9
    vm, v_set = np.abs(v[pos]), np.abs(grid.v_start[pos])
    wrong = (at_max & (vm > v_set + 1e-9)) | (~at_max & (vm < v_set - 1e-9))
    return [int(b) for b, w in zip(limited, wrong, strict=True) if w]


def branch_flows(
    grid: Grid, v: np.ndarray, outage: int | None = None
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Terminal flows at both ends (MVA, complex) and PSS/E loading on RATE A, per branch.

    Arrays in ``grid.names`` order; branches out of service, and ``outage``, are NaN.
    """
    vi, vj = v[grid.f], v[grid.t]
    ii = grid.y[:, 0] * vi + grid.y[:, 1] * vj
    ij = grid.y[:, 2] * vi + grid.y[:, 3] * vj
    sbase = grid.model.sbase
    si, sj = vi * np.conj(ii) * sbase, vj * np.conj(ij) * sbase
    by_current = np.maximum(np.abs(ii), np.abs(ij)) * sbase
    worst = np.where(grid.current_based, by_current, np.maximum(np.abs(si), np.abs(sj)))
    with np.errstate(divide="ignore", invalid="ignore"):
        loading = np.where(grid.rate_a > 0, worst / grid.rate_a * 100, np.nan)
    dead = ~grid.live
    if outage is not None:
        dead = dead.copy()
        dead[outage] = True
    si[dead], sj[dead], loading[dead] = np.nan, np.nan, np.nan
    return si, sj, loading


def branch_results(grid: Grid, v: np.ndarray, outage: int | None = None) -> pd.DataFrame:
    """``branch_flows`` as a table indexed by branch name (MW, Mvar, % of RATE A)."""
    si, sj, loading = branch_flows(grid, v, outage)
    return pd.DataFrame(
        {
            "p_i_mw": si.real,
            "q_i_mvar": si.imag,
            "p_j_mw": sj.real,
            "q_j_mvar": sj.imag,
            "loading_a_pct": loading,
        },
        index=pd.Index(grid.names, name="branch"),
    )


def series_loading(grid: Grid, v: np.ndarray, outage: int | None = None) -> np.ndarray:
    """Loading in PowerFactory's convention, % of RATE A, per branch (validation log, entry 7).

    Lines: current at the terminals without the line shunts (GI + jBI, GJ + jBJ), which
    PowerFactory models as separate compensation. Transformers: current instead of MVA.
    """
    vi, vj = v[grid.f], v[grid.t]
    ii = grid.y[:, 0] * vi + grid.y[:, 1] * vj - grid.y_end[:, 0] * vi
    ij = grid.y[:, 2] * vi + grid.y[:, 3] * vj - grid.y_end[:, 1] * vj
    with np.errstate(divide="ignore", invalid="ignore"):
        loading = np.maximum(np.abs(ii), np.abs(ij)) * grid.model.sbase / grid.rate_a * 100
    dead = ~grid.live
    if outage is not None:
        dead = dead.copy()
        dead[outage] = True
    loading[dead] = np.nan
    return loading
