"""Run the whole pipeline, from the Zenodo download to the scenarios.

Steps whose output already exists are skipped, unless --force is given. The download
checks the MD5 of what is already on disk and fetches only what is missing.

Steps run in the order of their dependencies, which is not quite the order of their
numbers: the transfer capability (12) is measured from the secure state of the redispatch
(15).

The PowerFactory comparisons (steps 10, 13 and 17) read the committed exports in
powerfactory/exports. With --powerfactory, PowerFactory itself is run first to refresh them
(needs a licence).

Usage: python scripts/run_pipeline.py [--force] [--workers N] [--powerfactory]
"""

import argparse
import subprocess
import sys
import time
from pathlib import Path

from n44.config import repo_path

# (script, output that marks the step as done; None = always run)
STEPS = (
    ("01_download_data.py", None),
    ("02_inventory.py", "data/processed/inventory_files.csv"),
    ("03_check_psse_consistency.py", "data/processed/psse_consistency.parquet"),
    ("04_build_network.py", "data/processed/n44_base.json"),
    ("05_build_snapshots.py", "data/processed/calendar.parquet"),
    ("06_validate_psse.py", "results/validation/psse_summary.csv"),
    ("07_nordpool_dc_comparison.py", "results/validation/dc_summary.csv"),
    ("08_powerflow_8760.py", "results/annual/summary.parquet"),
    ("09_annual_analysis.py", None),
    ("10_validate_powerfactory.py", "results/validation/powerfactory_triangle.csv"),
    ("11_n1_security.py", "results/n1_ranking.csv"),
    ("13_validate_powerfactory_n1.py", "results/validation/powerfactory_n1_summary.csv"),
    ("14_opf_prices.py", "results/opf_summary.csv"),
    ("15_redispatch.py", "results/redispatch_ac_check.csv"),
    ("12_transfer_margins.py", "results/transfer_margins_summary.csv"),
    ("16_scenarios.py", "results/scenario_comparison.csv"),
    ("17_validate_powerfactory_opf.py", "results/validation/powerfactory_opf_summary.csv"),
)

# PowerFactory batch that refreshes the exports a step reads (with --powerfactory)
POWERFACTORY = {
    "10_": "run_ldf_batch.py",
    "13_": "run_n1_batch.py",
    "17_": "run_opf_batch.py",
}
PARALLEL = ("08_", "14_", "15_", "16_")  # steps that take --workers
RESUMABLE = ("05_", "11_", "15_", "16_")  # steps with their own cache, cleared by --force


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--force", action="store_true", help="re-run steps already done")
    parser.add_argument("--workers", type=int, default=3, help="processes for parallel steps")
    parser.add_argument(
        "--powerfactory", action="store_true", help="re-run PowerFactory before its comparisons"
    )
    args = parser.parse_args()
    scripts = Path(__file__).parent
    for script, output in STEPS:
        batch = POWERFACTORY.get(script[:3])
        if args.powerfactory and batch:
            print(f"[run ] powerfactory/{batch}", flush=True)
            subprocess.run([sys.executable, str(repo_path("powerfactory", batch))], check=True)
            output = None
        if output and not args.force and repo_path(output).exists():
            print(f"[skip] {script} ({output} exists)")
            continue
        command = [sys.executable, str(scripts / script)]
        if script.startswith(PARALLEL):
            command += ["--workers", str(args.workers)]
        if script.startswith(RESUMABLE) and args.force:
            command.append("--force")
        print(f"[run ] {script}", flush=True)
        start = time.perf_counter()
        subprocess.run(command, check=True)
        print(f"[done] {script} in {time.perf_counter() - start:.0f} s", flush=True)


if __name__ == "__main__":
    main()
