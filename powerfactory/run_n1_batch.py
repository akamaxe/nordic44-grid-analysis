"""PowerFactory's contingency analysis (ComSimoutage) on critical hours, into powerfactory/exports.

Each hour is imported with PowerFactory's PSS/E importer, given the exact transformer ratios
(validation log, entry 6) and analysed with one ComOutage per line and transformer, except
the radial HVDC connections. Recording limits and filters are opened so that every branch
and bus is recorded for every contingency.

Exports, with PowerFactory's orientation (``bus1``/``bus2``, HV/LV for transformers):
- n1_branches.csv.gz: post-contingency P and Q at both ends and PowerFactory's loading;
- n1_buses.csv.gz: post-contingency voltages;
- n1_cases.csv: one row per contingency with PowerFactory's convergence flag.

Usage: python powerfactory/run_n1_batch.py [--hours H ...]
"""

from __future__ import annotations

import argparse
import time

import pandas as pd
import pf_n44

from n44.config import configured_path, load_config, repo_path
from n44.io.dataset import Dataset
from n44.io.psse_parser import parse_raw

# Critical hours (see scripts/11_n1_security.py): the highest snitt-2 transfer, the highest
# branch loading, the winter peak, and two hours in which an outage has no AC solution.
HOURS = (5370, 6949, 538, 175, 5600)


def contingency_analysis(app) -> dict[str, pd.DataFrame]:
    """Run ComSimoutage on the active study case and read its result file."""
    study = app.GetActiveStudyCase()
    for old in study.GetContents("N44 N-1.ComSimoutage"):
        old.Delete()
    command = study.CreateObject("ComSimoutage", "N44 N-1")
    outages = pf_n44.add_contingencies(app, command)
    return pf_n44.contingency_results(app, command, outages)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hours", type=int, nargs="*", default=list(HOURS))
    args = parser.parse_args()
    raw_dir = configured_path("processed_dir") / "powerfactory" / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    out = repo_path(load_config()["powerfactory"]["exports_dir"])
    ds = Dataset()
    app = pf_n44.start()
    tables = {"cases": [], "branches": [], "buses": []}
    for h in args.hours:
        start = time.perf_counter()
        text = ds.raw_text(h, "after")
        raw_path = raw_dir / f"h{h:04d}.raw"
        raw_path.write_text(text, encoding="latin-1", newline="")
        project = pf_n44.import_raw(app, raw_path, f"N44_h{h:04d}")
        pf_n44.set_exact_ratios(app, parse_raw(text))
        if not pf_n44.run_ldf(app):
            raise RuntimeError(f"base case of h={h} does not converge in PowerFactory")
        for name, table in contingency_analysis(app).items():
            tables[name].append(table.assign(h=h))
        pf_n44.discard(app, project)
        print(f"h={h}: {time.perf_counter() - start:.1f} s", flush=True)
    for name, parts in tables.items():
        frame = pd.concat(parts, ignore_index=True)
        frame = frame[["h", *[c for c in frame.columns if c != "h"]]]
        target = out / (f"n1_{name}.csv" if name == "cases" else f"n1_{name}.csv.gz")
        frame.to_csv(target, index=False, float_format="%.7g")
    print(f"exports written to {out}")


if __name__ == "__main__":
    main()
