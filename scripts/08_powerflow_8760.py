"""AC power flow for all 8,760 hours of 2015 (see n44.annual).

Writes results/annual/*.parquet: the per-hour summary and wide tables (rows = hours) of bus
voltages, terminal branch flows and loading, border and cut flows, and losses per zone.
Usage: python scripts/08_powerflow_8760.py [--workers N]
"""

import argparse
import time

from n44.annual import run_year
from n44.config import configured_path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workers", type=int, default=3)
    args = parser.parse_args()
    start = time.perf_counter()
    tables = run_year(workers=args.workers)
    out = configured_path("results_dir") / "annual"
    out.mkdir(parents=True, exist_ok=True)
    for name, frame in tables.items():
        frame.to_parquet(out / f"{name}.parquet")
    s = tables["summary"]
    ok = s[s.converged]
    print(
        f"{len(s)} hours in {time.perf_counter() - start:.0f} s; converged {len(ok)} "
        f"(initialisation: {ok.init.value_counts().to_dict()})"
    )
    print(
        f"Deviation from PSS/E over the year: max |dVm| {ok.max_dvm_pu.max():.2e} pu, "
        f"max |dVa| {ok.max_dva_deg.max():.2e} deg, max |slack dP| "
        f"{(ok.slack_p_mw - ok.slack_p_psse_mw).abs().max():.3f} MW, max plant |dQ| "
        f"{ok.max_dq_plant_mvar.max():.2f} Mvar"
    )
    print(
        f"Hours with a reactive-limited plant: {(ok.n_q_limited > 0).sum()}; with an "
        f"inconsistent limit: {(ok.n_q_inconsistent > 0).sum()}"
    )
    print(
        f"Losses {ok.losses_mw.min():.0f}-{ok.losses_mw.max():.0f} MW; hours with a branch "
        f"at or above 100 % of RATE A: {(ok.n_overloaded > 0).sum()}"
    )


if __name__ == "__main__":
    main()
