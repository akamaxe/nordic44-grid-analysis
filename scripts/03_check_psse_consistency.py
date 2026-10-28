"""Validation level A on all 8,760 hours: is every PSS/E solution read correctly?

For each hour, the after_PF case is checked with the reference model of ``n44.ybus``, before
any pandapower network is built:

- the power balance at PSS/E's voltages, bus by bus, against the bound set by the file's
  rounding;
- the static model parameters and the element statuses, through two signatures that must not
  change;
- generation, load and metered interchange per area, against PSSE_in_out.xlsx and the PDES;
- what changed between before_PF and after_PF: taps, and which machines changed output;
- the Rate A overload flag of PSSE_in_out, against the loading computed here.

Writes data/processed/psse_consistency.parquet (one row per hour) and prints a summary.
"""

import hashlib

import pandas as pd

from n44.config import configured_path
from n44.io.dataset import Dataset, read_member
from n44.io.nordpool import AREAS, read_psse_in_out
from n44.io.psse_parser import RawCase, parse_raw
from n44.ybus import area_interchange, branch_flows, build_reference, power_mismatch

STATIC_FIELDS = {
    "bus": ["i", "baskv", "area"],
    "branch": [
        "i",
        "j",
        "ckt",
        "r",
        "x",
        "b",
        "ratea",
        "rateb",
        "ratec",
        "gi",
        "bi",
        "gj",
        "bj",
        "met",
    ],
    "transformer": [
        "i",
        "j",
        "ckt",
        "cw",
        "cz",
        "cm",
        "r1_2",
        "x1_2",
        "sbase1_2",
        "mag1",
        "mag2",
        "nmetr",
        "rata1",
        "ratb1",
        "ratc1",
        "cod1",
        "cont1",
        "rma1",
        "rmi1",
        "vma1",
        "vmi1",
        "ntp1",
    ],
    "generator": ["i", "id", "qt", "qb", "pt", "pb", "mbase", "ireg"],
    "load": ["i", "id", "area", "scale"],
}
STATUS_FIELDS = {
    "bus": ["i", "ide"],
    "branch": ["st"],
    "transformer": ["stat"],
    "generator": ["stat"],
    "load": ["status"],
}
SETPOINT_FIELDS = {"generator": ["vs"]}


def signature(case: RawCase, fields: dict[str, list[str]]) -> str:
    digest = hashlib.sha1()
    for table, columns in fields.items():
        values = pd.util.hash_pandas_object(getattr(case, table)[columns], index=False)
        digest.update(values.to_numpy().tobytes())
    return digest.hexdigest()[:12]


def psse_summaries(ds: Dataset) -> tuple[pd.DataFrame, pd.DataFrame]:
    """PSSE_in_out of every day, with the hour index h added."""
    areas, flags = [], []
    files = ds.excel_files()
    for _, row in files[files["kind"] == "PSSE_in_out"].iterrows():
        a, f = read_psse_in_out(read_member(row["archive"], row["member"]))
        offset = (pd.Timestamp(row["date"]).dayofyear - 1) * 24
        a["h"], f["h"] = a["hour"] + offset, f["hour"] + offset
        areas.append(a)
        flags.append(f)
    return pd.concat(areas, ignore_index=True), pd.concat(flags, ignore_index=True)


def check_hour(after: RawCase, before: RawCase) -> dict:
    model = build_reference(after)
    mismatch = power_mismatch(after, model)
    flows = branch_flows(after, model)
    export = area_interchange(after, flows)
    bus_area = after.bus.set_index("i").area
    gen = after.generator[after.generator.stat == 1]
    load = after.load[after.load.status == 1]
    gen_by_area = gen.groupby(gen.i.map(bus_area)).pg.sum()
    load_by_area = load.groupby(load.i.map(bus_area)).pl.sum()
    pdes = after.area.set_index("i").pdes
    isw = set(after.area.isw)
    pg_changed = after.generator.pg.to_numpy() != before.generator.pg.to_numpy()
    changed_buses = set(after.generator.i[pg_changed])
    worst = mismatch.ds_mva.idxmax()
    swing = after.bus.i[after.bus.ide == 3].iloc[0]
    record = {
        "max_dp_mw": mismatch.dp_mw.abs().max(),
        "max_dq_mvar": mismatch.dq_mvar.abs().max(),
        "worst_bus": int(worst),
        "buses_over_bound": int((mismatch.ds_mva > mismatch.bound_mva).sum()),
        "max_ratio_to_bound": (mismatch.ds_mva / mismatch.bound_mva).max(),
        "static_sig": signature(after, STATIC_FIELDS),
        "status_sig": signature(after, STATUS_FIELDS),
        "setpoint_sig": signature(after, SETPOINT_FIELDS),
        "taps_changed": int((after.transformer.windv1 != before.transformer.windv1).sum()),
        "pg_changed_outside_isw": len(changed_buses - isw),
        "max_loading_pct": flows.loading_a_pct.max(),
        "losses_mw": flows.loss_mw.sum(),
        "swing_pg_mw": gen.loc[gen.i == swing, "pg"].sum(),
        "max_interchange_vs_pdes_mw": (export - pdes).abs().max(),
    }
    for number, name in zip(sorted(after.area.i), AREAS, strict=True):
        record[f"export_{name}"] = export[number]
        record[f"pdes_{name}"] = pdes[number]
        record[f"gen_{name}"] = gen_by_area.get(number, 0.0)
        record[f"load_{name}"] = load_by_area.get(number, 0.0)
    return record


def main() -> None:
    ds = Dataset()
    records = []
    for h in range(8760):
        after, before = parse_raw(ds.raw_text(h, "after")), parse_raw(ds.raw_text(h, "before"))
        records.append({"h": h, **check_hour(after, before)})
        if h % 1000 == 0:
            print(f"hour {h}", flush=True)
    result = pd.DataFrame(records).set_index("h")

    areas, flags = psse_summaries(ds)
    wide = areas.pivot(index="h", columns="area")
    for name in AREAS:
        result[f"psse_exchange_{name}"] = wide[("psse_exchange", name)]
        result[f"psse_production_{name}"] = wide[("psse_production", name)]
        result[f"psse_consumption_{name}"] = wide[("psse_consumption", name)]
    flags = flags.set_index("h")
    result["psse_converged"] = flags["Convergence"]
    result["psse_branch_overload"] = flags["branch_overload"]

    out = configured_path("processed_dir") / "psse_consistency.parquet"
    result.to_parquet(out)
    summarise(result)


def summarise(r: pd.DataFrame) -> None:
    def spread(column_prefix_a, column_prefix_b):
        return max(
            (r[f"{column_prefix_a}_{a}"] - r[f"{column_prefix_b}_{a}"]).abs().max() for a in AREAS
        )

    print(f"\nHours checked: {len(r)}; PSS/E converged in all: {bool(r.psse_converged.all())}")
    print(
        f"Power balance at PSS/E voltages: max |dP| = {r.max_dp_mw.max():.3f} MW, "
        f"max |dQ| = {r.max_dq_mvar.max():.3f} Mvar; hours with a bus over its rounding "
        f"bound: {(r.buses_over_bound > 0).sum()} (worst ratio {r.max_ratio_to_bound.max():.2f})"
    )
    print(
        "  per-hour max |dQ| percentiles (Mvar):",
        r.max_dq_mvar.quantile([0.5, 0.9, 0.99, 1.0]).round(3).to_dict(),
    )
    print(
        f"Static parameter signatures: {r.static_sig.nunique()}; status signatures: "
        f"{r.status_sig.nunique()}; generator setpoint signatures: {r.setpoint_sig.nunique()}"
    )
    print(
        f"Hours with taps changed by PSS/E: {(r.taps_changed > 0).sum()}; hours where a "
        f"machine outside the area-slack buses changed output: "
        f"{(r.pg_changed_outside_isw > 0).sum()}"
    )
    print(
        f"Area generation vs PSSE_in_out: max |diff| = {spread('gen', 'psse_production'):.2f} "
        f"MW; area load: {spread('load', 'psse_consumption'):.2f} MW"
    )
    print(
        f"Metered interchange vs PSSE_in_out: max |diff| = "
        f"{spread('export', 'psse_exchange'):.2f} MW"
    )
    gap = r.max_interchange_vs_pdes_mw
    pdes_sum = sum(r[f"pdes_{a}"] for a in AREAS)
    print(
        f"Metered interchange vs PDES: max |diff| = {gap.max():.2f} MW; hours above 10 MW: "
        f"{(gap > 10).sum()}; percentiles {gap.quantile([0.5, 0.99]).round(2).to_dict()}"
    )
    print(
        f"  hours whose ten PDES do not add up to zero (|sum| > 1 MW): "
        f"{(pdes_sum.abs() > 1).sum()}, largest {pdes_sum.abs().max():.0f} MW; the swing area "
        f"absorbs the sum and the other areas' residuals"
    )
    swing_area_gap = (r["export_SE3"] - r["pdes_SE3"] + pdes_sum).abs()
    others = max((r[f"export_{a}"] - r[f"pdes_{a}"]).abs().max() for a in AREAS if a != "SE3")
    print(
        f"  non-swing areas within {others:.2f} MW of PDES; swing area SE3 within "
        f"{swing_area_gap.max():.2f} MW once the PDES sum is accounted for"
    )
    ours = r.max_loading_pct >= 100
    table = pd.crosstab(
        r.psse_branch_overload,
        ours,
        rownames=["PSS/E overload flag"],
        colnames=["loading >= 100 %"],
    )
    print("Branch overload (Rate A), PSS/E flag vs loading computed here:\n", table)
    print(
        f"Losses: {r.losses_mw.min():.0f}-{r.losses_mw.max():.0f} MW; swing P: "
        f"{r.swing_pg_mw.min():.0f}-{r.swing_pg_mw.max():.0f} MW; max loading "
        f"{r.max_loading_pct.max():.1f} % (h={int(r.max_loading_pct.idxmax())})"
    )


if __name__ == "__main__":
    main()
