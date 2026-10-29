"""PowerFactory's DC optimal power flow (ComOpf, linear programming) on a few hours.

Each hour is imported with PowerFactory's PSS/E importer. Every machine gets the linear cost
of config/costs.yaml as a two-point operating cost table (the LP uses the average slope) and
active-power control; branch limits are the normal ratings (RATE A). The OPF minimises
costs and computes the locational marginal prices.

Exports to powerfactory/exports:
- opf_buses.csv: LMP and system lambda per terminal;
- opf_machines.csv: dispatch per machine;
- opf_runs.csv: solver status and warnings.

Note: start PowerFactory before importing scipy. Its HiGHS libraries share DLL names with
PowerFactory's, and once loaded they make the engine fail with ExitError 1001 ("cannot load
DLL"). This script therefore reads the costs with yaml, not through n44.opf.

Usage: python powerfactory/run_opf_batch.py [--hours H ...]
"""

from __future__ import annotations

import argparse

import pandas as pd
import pf_n44
import yaml

from n44.config import configured_path, load_config, repo_path
from n44.io.dataset import Dataset

HOURS = (538, 5370, 3657)  # winter peak, highest snitt-2 transfer, summer peak


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hours", type=int, nargs="*", default=list(HOURS))
    args = parser.parse_args()
    with open(repo_path("config", "costs.yaml"), encoding="utf-8") as fh:
        costs = yaml.safe_load(fh)
    raw_dir = configured_path("processed_dir") / "powerfactory" / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    out = repo_path(load_config()["powerfactory"]["exports_dir"])
    ds = Dataset()
    app = pf_n44.start()
    buses, machines, runs = [], [], []
    for h in args.hours:
        raw_path = raw_dir / f"h{h:04d}.raw"
        raw_path.write_text(ds.raw_text(h, "after"), encoding="latin-1", newline="")
        project = pf_n44.import_raw(app, raw_path, f"tmp_h{h:04d}")
        pf_n44.set_costs(app, costs)
        opf = pf_n44.set_opf_options(app)
        app.ClearOutputWindow()
        ok = opf.Execute() == 0
        lines = pf_n44.output(app)
        warnings = [line for line in lines if line.startswith(("warn", "err"))]
        runs.append({"h": h, "success": ok, "warnings": " | ".join(w[:160] for w in warnings)})
        for term in app.GetCalcRelevantObjects("*.ElmTerm"):
            buses.append(
                {
                    "h": h,
                    "bus": int(term.loc_name.split()[0]),
                    "lmp": pf_n44._get(term, "m:LMP"),
                    "system_lambda": pf_n44._get(term, "m:SysLambda"),
                }
            )
        for sym in app.GetCalcRelevantObjects("*.ElmSym"):
            machines.append(
                {
                    "h": h,
                    "bus": pf_n44._bus(sym.bus1),
                    "id": sym.loc_name.split("_", 2)[2],
                    "p_mw": pf_n44._get(sym, "m:P:bus1"),
                }
            )
        pf_n44.discard(app, project)
        print(f"h={h}: success={ok}; {warnings[:2]}", flush=True)
    pd.DataFrame(buses).to_csv(out / "opf_buses.csv", index=False, float_format="%.6g")
    pd.DataFrame(machines).to_csv(out / "opf_machines.csv", index=False, float_format="%.6g")
    pd.DataFrame(runs).to_csv(out / "opf_runs.csv", index=False)
    print(f"exports written to {out}")


if __name__ == "__main__":
    main()
