"""Load flows in PowerFactory for the validation hours, exported to powerfactory/exports.

Each hour's after_PF RAW file is imported with PowerFactory's own PSS/E importer into a
temporary project, solved with the options of the pandapower runs (``pf_n44.LDF_OPTIONS``)
and exported. Two variants are solved per hour:

- ``imported``: the model exactly as PowerFactory's importer builds it;
- ``exact``: the same model with each transformer's ratio set to the RAW file's WINDV,
  which the importer rounds to the nearest tap position (see docs/validation_log.md).

Hours: the representative hours (config/representative_hours.yaml) plus ``random_hours``
drawn at random with a fixed seed, never the synthetic hour of the spring clock change.

Usage: python powerfactory/run_ldf_batch.py [--hours H ...]
"""

from __future__ import annotations

import argparse
import time

import numpy as np
import pandas as pd
import pf_n44
import yaml

from n44.config import configured_path, load_config, repo_path
from n44.io.dataset import Dataset
from n44.io.psse_parser import parse_raw

VARIANTS = ("imported", "exact")  # in this order: "exact" modifies the imported model


def validation_hours() -> pd.DataFrame:
    """Representative hours plus a seeded random sample of the others."""
    cfg = load_config()["powerfactory"]
    with open(repo_path("config", "representative_hours.yaml"), encoding="utf-8") as fh:
        representative = pd.DataFrame(yaml.safe_load(fh))[["h", "criterion"]]
    calendar = pd.read_parquet(configured_path("processed_dir") / "calendar.parquet")
    pool = calendar.index[~calendar.synthetic].difference(representative.h)
    rng = np.random.default_rng(cfg["seed"])
    drawn = np.sort(rng.choice(pool.to_numpy(), size=cfg["random_hours"], replace=False))
    hours = pd.concat(
        [representative, pd.DataFrame({"h": drawn, "criterion": "random"})], ignore_index=True
    )
    hours["date"] = calendar.date[hours.h].dt.date.astype(str).to_numpy()
    hours["hour"] = calendar.hour[hours.h].to_numpy()
    return hours


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hours", type=int, nargs="*", help="hours to run (default: all)")
    args = parser.parse_args()

    hours = validation_hours()
    if args.hours:
        hours = hours[hours.h.isin(args.hours)]
    raw_dir = configured_path("processed_dir") / "powerfactory" / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    out = repo_path(load_config()["powerfactory"]["exports_dir"])
    out.mkdir(parents=True, exist_ok=True)

    ds = Dataset()
    app = pf_n44.start()
    tables = {"buses": [], "machines": [], "branches": []}
    runs, ratios = [], []
    for row in hours.itertuples():
        text = ds.raw_text(row.h, "after")
        raw_path = raw_dir / f"h{row.h:04d}.raw"
        raw_path.write_text(text, encoding="latin-1", newline="")
        case = parse_raw(text)
        start = time.perf_counter()
        project = pf_n44.import_raw(app, raw_path, f"N44_h{row.h:04d}")
        for variant in VARIANTS:
            if variant == "exact":
                ratios.append(pf_n44.set_exact_ratios(app, case).assign(h=row.h))
            app.ClearOutputWindow()
            converged = pf_n44.run_ldf(app)
            warnings = [line for line in pf_n44.output(app) if line.startswith("warn")]
            runs.append(
                {"h": row.h, "variant": variant, "converged": converged, "warnings": len(warnings)}
            )
            for name, table in pf_n44.export_tables(app).items():
                tables[name].append(table.assign(h=row.h, variant=variant))
        pf_n44.discard(app, project)
        print(f"h={row.h:4d} ({row.criterion}): {time.perf_counter() - start:.1f} s", flush=True)

    hours.to_csv(out / "hours.csv", index=False)
    pd.DataFrame(runs).to_csv(out / "ldf_runs.csv", index=False)
    pd.concat(ratios).to_csv(out / "transformer_ratios.csv", index=False, float_format="%.6f")
    for name, parts in tables.items():
        frame = pd.concat(parts, ignore_index=True)
        frame = frame[["h", "variant", *[c for c in frame.columns if c not in ("h", "variant")]]]
        frame.to_csv(out / f"ldf_{name}.csv", index=False, float_format="%.9g")
    print(f"exports written to {out}")


if __name__ == "__main__":
    main()
