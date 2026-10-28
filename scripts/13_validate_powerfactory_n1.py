"""PowerFactory's contingency analysis against this project's AC N-1.

Reads the exports of powerfactory/run_n1_batch.py and solves the same hours and outages with
the reference-model solver (``n44.acpf``). For every hour and outage it compares:
- whether an AC solution exists;
- post-contingency terminal flows and voltages;
- loading in PowerFactory's own convention (``c:loading``);
- the ranking of the outages by their worst induced overload in PSS/E's convention.

Writes results/validation/powerfactory_n1_summary.csv.
"""

import numpy as np
import pandas as pd

from n44.acpf import branch_flows, grid_from_case, series_loading, solve
from n44.config import configured_path, load_config, repo_path
from n44.io.dataset import Dataset
from n44.io.psse_parser import parse_raw
from n44.validation import branch_orientation

TOP = 10  # outages compared in the ranking


def _names(table: pd.DataFrame, prefix: str, case, orient: dict) -> list[str]:
    """PSS/E-style branch names ("line I-J-CKT") for PowerFactory's (kind, bus1, bus2, ckt)."""
    names = []
    for kind, a, b, ckt in zip(
        table[f"{prefix}kind"],
        table[f"{prefix}bus1"],
        table[f"{prefix}bus2"],
        table[f"{prefix}ckt"].astype(str),
        strict=True,
    ):
        (kind_, i, j, ckt_), _ = orient[(kind, a, b, ckt)]
        names.append(f"{kind_} {i}-{j}-{ckt_}")
    return names


def worst_induced(loading: pd.DataFrame, base: pd.Series, ratio: pd.Series) -> pd.Series:
    """Per outage, the highest post-contingency loading (% of RATE B) on a branch within
    RATE B before the outage; NaN if the outage induces no overload candidate."""
    to_b = loading * ratio
    within = (base * ratio) <= 100
    return to_b.loc[:, within].max(axis=1)


def main() -> None:
    folder = repo_path(load_config()["powerfactory"]["exports_dir"])
    cases = pd.read_csv(folder / "n1_cases.csv")
    branches = pd.read_csv(folder / "n1_branches.csv.gz")
    buses = pd.read_csv(folder / "n1_buses.csv.gz")
    ds = Dataset()
    rows = []
    for h in sorted(cases.h.unique()):
        case = parse_raw(ds.raw_text(h, "after"))
        orient = branch_orientation(case)
        grid = grid_from_case(case)
        base = solve(grid)
        ratio = pd.Series(grid.rate_a / grid.model.branches.rate_b.to_numpy(), index=grid.names)
        base_loading = pd.Series(branch_flows(grid, base.v)[2], index=grid.names)
        hc = cases[cases.h == h].copy()
        hc["outage"] = _names(hc, "outage_", case, orient)
        hb = branches[branches.h == h].copy()
        hb["outage"] = _names(hb, "outage_", case, orient)
        hb["branch"] = _names(hb, "", case, orient)
        hv = buses[buses.h == h].copy()
        hv["outage"] = _names(hv, "outage_", case, orient)

        both = 0
        conv_agree = 0
        dp = dq = dv = dl_pf = 0.0
        ours_worst, theirs_worst = {}, {}
        for outage in hc.outage:
            k = grid.names.index(outage)
            sol = solve(grid, outage=k, v0=base.v, max_iter=20)
            if not sol.converged:
                sol = solve(grid, outage=k, max_iter=20)
            pf_conv = bool(hc.loc[hc.outage == outage, "converged"].iloc[0])
            conv_agree += sol.converged == pf_conv
            if not (sol.converged and pf_conv):
                continue
            both += 1
            si, sj, loading = branch_flows(grid, sol.v, outage=k)
            ours = pd.DataFrame(
                {
                    "p_i": si.real,
                    "q_i": si.imag,
                    "p_j": sj.real,
                    "q_j": sj.imag,
                    "loading": loading,
                    "series": series_loading(grid, sol.v, outage=k),
                },
                index=grid.names,
            )
            theirs = hb[(hb.outage == outage) & hb.p1_mw.notna()].set_index("branch")
            theirs = theirs.drop(index=outage, errors="ignore")
            # orient PowerFactory's flows I -> J
            swap = [orient[(r.kind, r.bus1, r.bus2, str(r.ckt))][1] for r in theirs.itertuples()]
            swap = np.array(swap)
            p_i = np.where(swap, theirs.p2_mw, theirs.p1_mw)
            q_i = np.where(swap, theirs.q2_mvar, theirs.q1_mvar)
            p_j = np.where(swap, theirs.p1_mw, theirs.p2_mw)
            q_j = np.where(swap, theirs.q1_mvar, theirs.q2_mvar)
            mine = ours.loc[theirs.index]
            dp = max(dp, np.abs(np.r_[p_i - mine.p_i, p_j - mine.p_j]).max())
            dq = max(dq, np.abs(np.r_[q_i - mine.q_i, q_j - mine.q_j]).max())
            dl_pf = max(dl_pf, np.abs(theirs.loading_pf_pct - mine.series).max())
            v_pf = hv[hv.outage == outage].set_index("bus").vm_pu
            v_ours = pd.Series(np.abs(sol.v), index=grid.model.buses)
            dv = max(dv, np.abs(v_pf - v_ours.loc[v_pf.index]).max())
            # loading in PSS/E's convention, recomputed from PowerFactory's flows and voltages
            vm = v_pf
            br = grid.model.branches.set_index(pd.Index(grid.names)).loc[theirs.index]
            s_i, s_j = np.hypot(p_i, q_i), np.hypot(p_j, q_j)
            by_current = np.maximum(s_i / vm.loc[br.i].to_numpy(), s_j / vm.loc[br.j].to_numpy())
            worst = np.where(br.rating_basis == "current", by_current, np.maximum(s_i, s_j))
            pf_loading = pd.Series(worst / br.rate_a.to_numpy() * 100, index=theirs.index)
            ours_worst[outage] = worst_induced(
                mine.loading.to_frame().T, base_loading.loc[theirs.index], ratio.loc[theirs.index]
            ).iloc[0]
            theirs_worst[outage] = worst_induced(
                pf_loading.to_frame().T, base_loading.loc[theirs.index], ratio.loc[theirs.index]
            ).iloc[0]
        ours_rank = pd.Series(ours_worst).sort_values(ascending=False).head(TOP).index
        theirs_rank = pd.Series(theirs_worst).sort_values(ascending=False).head(TOP).index
        rows.append(
            {
                "h": h,
                "outages": len(hc),
                "no_solution_powerfactory": int((~hc.converged).sum()),
                "convergence_agrees": conv_agree,
                "compared": both,
                "max_dp_mw": dp,
                "max_dq_mvar": dq,
                "max_dvm_pu": dv,
                "max_dloading_pf_convention_pct": dl_pf,
                f"top{TOP}_same_set": len(set(ours_rank) & set(theirs_rank)),
                f"top{TOP}_same_order": bool(list(ours_rank) == list(theirs_rank)),
            }
        )
        print(rows[-1], flush=True)
    out = configured_path("results_dir") / "validation"
    pd.DataFrame(rows).to_csv(out / "powerfactory_n1_summary.csv", index=False, float_format="%.3g")


if __name__ == "__main__":
    main()
