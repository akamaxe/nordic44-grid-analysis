"""Three-way validation: pandapower, PowerFactory and PSS/E on the same hours.

Reads the exports of powerfactory/run_ldf_batch.py (running PowerFactory is not needed
here), solves the same hours with pandapower and compares every pair: pandapower-PSS/E,
PowerFactory-PSS/E and PowerFactory-pandapower, for both PowerFactory variants (the model as
imported, and with exact transformer ratios).

Writes results/validation/powerfactory_{summary,triangle,buses,branches}.csv and the figure
results/figures/validation_powerfactory.png.
"""

import pandas as pd

from n44.config import configured_path, load_config, repo_path
from n44.io.build_net import build_net
from n44.io.dataset import Dataset
from n44.io.psse_parser import parse_raw
from n44.plotting import plot_powerfactory_validation
from n44.powerflow import run_ac
from n44.validation import (
    compare,
    solution_from_case,
    solution_from_net,
    solution_from_powerfactory,
)


def _generic(key: str, test: str, ref: str) -> str:
    """Summary key with the pair's labels replaced by 'test' and 'ref' (losses_pf_mw...)."""
    for label, generic in ((ref, "ref"), (test, "test")):
        key = key.replace(f"_{label}_", f"_{generic}_")
        if key.endswith(f"_{label}"):
            key = key[: -len(label)] + generic
    return key


def read_exports() -> dict[str, pd.DataFrame]:
    folder = repo_path(load_config()["powerfactory"]["exports_dir"])
    names = ("hours", "ldf_runs", "ldf_buses", "ldf_machines", "ldf_branches")
    tables = {name: pd.read_csv(folder / f"{name}.csv") for name in names}
    tables["ldf_branches"]["ckt"] = tables["ldf_branches"].ckt.astype(str)
    tables["ldf_machines"]["id"] = tables["ldf_machines"].id.astype(str)
    return tables


def main() -> None:
    exports = read_exports()
    runs = exports["ldf_runs"].set_index(["h", "variant"])
    ds = Dataset()
    summaries, buses, branches = [], [], []
    for row in exports["hours"].itertuples():
        case = parse_raw(ds.raw_text(row.h, "after"))
        reference = solution_from_case(case)
        net = build_net(case)
        if not run_ac(net).converged:
            raise RuntimeError(f"pandapower did not converge for h={row.h}")
        solutions = {"pp": solution_from_net(net)}
        pairs = [("pp", "ref", None)]
        for variant in runs.loc[row.h].index:
            if not runs.loc[(row.h, variant), "converged"]:
                raise RuntimeError(f"PowerFactory did not converge for h={row.h} ({variant})")
            parts = [exports[name] for name in ("ldf_buses", "ldf_machines", "ldf_branches")]
            parts = [p[(p.h == row.h) & (p.variant == variant)] for p in parts]
            solutions[variant] = solution_from_powerfactory(*parts, case)
            pairs += [(variant, "ref", variant), (variant, "pp", variant)]
        for test, ref, variant in pairs:
            labels = ("pf" if variant else test, ref)
            result = compare(
                solutions[test], reference if ref == "ref" else solutions[ref], case, labels
            )
            pair = f"{labels[0]}-{labels[1]}"
            summary = {_generic(k, *labels): v for k, v in result.summary.items()}
            summaries.append(
                {
                    "h": row.h,
                    "criterion": row.criterion,
                    "pair": pair,
                    "variant": variant,
                    **summary,
                }
            )
            if labels[0] == "pf":
                b = result.buses[["zone", "dvm", "dva"]].reset_index()
                buses.append(b.assign(h=row.h, pair=pair, variant=variant))
                f = result.branches[["d_p_i_mw", "d_q_i_mvar", "d_p_j_mw", "d_q_j_mvar"]]
                branches.append(f.reset_index().assign(h=row.h, pair=pair, variant=variant))

    out = configured_path("results_dir") / "validation"
    out.mkdir(parents=True, exist_ok=True)
    summary = pd.DataFrame(summaries)
    summary.to_csv(out / "powerfactory_summary.csv", index=False, float_format="%.6g")
    buses = pd.concat(buses, ignore_index=True)
    branches = pd.concat(branches, ignore_index=True)
    buses.to_csv(out / "powerfactory_buses.csv", index=False, float_format="%.4g")
    branches.to_csv(out / "powerfactory_branches.csv", index=False, float_format="%.4g")

    metrics = [
        "max_dvm_pu",
        "max_dva_deg",
        "max_dp_branch_mw",
        "max_dq_branch_mvar",
        "max_dq_plant_mvar",
        "max_dloading_pct",
        "losses_rel",
    ]
    grouped = summary.fillna({"variant": "-"}).groupby(["pair", "variant"], sort=False)
    triangle = grouped[metrics].max()
    triangle["max_abs_slack_dp_mw"] = grouped.slack_dp_mw.apply(lambda s: s.abs().max())
    triangle["rmse_dvm_pu"] = grouped.rmse_dvm_pu.apply(lambda s: (s**2).mean() ** 0.5)
    triangle["hours"] = grouped.size()
    triangle["within_tolerance"] = grouped.within_tolerance.sum()
    triangle["limited_plants_match"] = grouped.limited_plants_match.sum()
    triangle.to_csv(out / "powerfactory_triangle.csv", float_format="%.3g")
    print(triangle.T.to_string())

    figure = plot_powerfactory_validation(
        buses[buses.pair == "pf-pp"],
        branches[branches.pair == "pf-pp"],
        load_config()["validation"],
        configured_path("results_dir") / "figures" / "validation_powerfactory.png",
    )
    print(f"figure: {figure}")


if __name__ == "__main__":
    main()
