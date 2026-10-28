"""Nord Pool borders, and AC against DC power flow, on the representative hours.

For each hour in config/representative_hours.yaml:

- **Borders and cuts.** Flows on every Nord Pool border and Swedish cut, from the pandapower AC
  solution, against Nord Pool's exchange.
- **AC against DC.** A plain DC power flow and a loss-compensated one, compared with the AC
  solution: branch flow errors against each branch's R/X ratio, and angle errors.

Writes results/validation/{nordpool_borders,dc_branches,dc_buses,dc_summary}.csv and
results/figures/dc_vs_ac.png.
"""

import copy

import numpy as np
import pandas as pd
import yaml

from n44.config import configured_path, repo_path
from n44.io.build_net import build_net
from n44.io.dataset import Dataset
from n44.io.psse_parser import parse_raw
from n44.plotting import plot_dc_errors
from n44.powerflow import dc_branch_flows, loss_compensated_dc, run_ac, run_dc
from n44.results import branch_flows
from n44.zones import CUTS, NORDPOOL_BORDERS, border_flows, cut_flows, nordpool_border_flows


def r_over_x(case) -> pd.Series:
    br, tr = case.branch, case.transformer
    lines = pd.Series(
        (br.r / br.x).to_numpy(),
        index=pd.MultiIndex.from_arrays([["line"] * len(br), br.i, br.j, br.ckt]),
    )
    trafos = pd.Series(
        (tr.r1_2 / tr.x1_2).to_numpy(),
        index=pd.MultiIndex.from_arrays([["transformer"] * len(tr), tr.i, tr.j, tr.ckt]),
    )
    out = pd.concat([lines, trafos])
    out.index.names = ["kind", "i", "j", "ckt"]
    return out


def main() -> None:
    with open(repo_path("config", "representative_hours.yaml"), encoding="utf-8") as fh:
        hours = pd.DataFrame(yaml.safe_load(fh))
    nordpool = nordpool_border_flows(
        pd.read_parquet(configured_path("processed_dir") / "nordpool_exchange.parquet")
    )
    ds = Dataset()
    borders, branches, buses, summaries = [], [], [], []
    keys = ["kind", "i", "j", "ckt"]
    for row in hours.itertuples():
        case = parse_raw(ds.raw_text(row.h, "after"))
        net = build_net(case)
        run_ac(net)
        flows = branch_flows(net)
        load_p = net.load.set_index(["bus", "psse_id"]).p_mw
        model = border_flows(flows, net.bus.zone, load_p)
        for name, value in model.items():
            a, b = name.split("->")
            np_value = nordpool.at[row.h, name] if (a, b) in NORDPOOL_BORDERS else np.nan
            borders.append(
                {
                    "h": row.h,
                    "criterion": row.criterion,
                    "kind": "border",
                    "name": name,
                    "model_mw": value,
                    "nordpool_mw": np_value,
                }
            )
        for name, value in cut_flows(flows, net.bus.zone).items():
            a, b = CUTS[name][2]
            borders.append(
                {
                    "h": row.h,
                    "criterion": row.criterion,
                    "kind": "cut",
                    "name": name,
                    "model_mw": value,
                    "nordpool_mw": nordpool.at[row.h, f"{a}->{b}"],
                }
            )

        plain = copy.deepcopy(net)
        run_dc(plain)
        compensated = loss_compensated_dc(net, flows)
        table = flows.set_index(keys)[["p_i_mw", "loading_a_pct"]].rename(
            columns={"p_i_mw": "p_ac_mw"}
        )
        table["p_dc_mw"] = dc_branch_flows(plain).set_index(keys).p_i_mw
        table["p_dc_comp_mw"] = dc_branch_flows(compensated).set_index(keys).p_i_mw
        table["r_over_x"] = r_over_x(case).reindex(table.index)
        table["err_dc_mw"] = table.p_dc_mw - table.p_ac_mw
        table["err_dc_comp_mw"] = table.p_dc_comp_mw - table.p_ac_mw
        branches.append(table.reset_index().assign(h=row.h))
        angles = pd.DataFrame(
            {
                "va_ac": net.res_bus.va_degree,
                "va_dc": plain.res_bus.va_degree,
                "va_dc_comp": compensated.res_bus.va_degree,
                "zone": net.bus.zone,
            }
        )
        buses.append(angles.rename_axis("bus").reset_index().assign(h=row.h))
        summaries.append(
            {
                "h": row.h,
                "criterion": row.criterion,
                "losses_mw": flows.loss_mw.sum(),
                "max_err_dc_mw": table.err_dc_mw.abs().max(),
                "max_err_dc_comp_mw": table.err_dc_comp_mw.abs().max(),
                "rmse_dc_mw": np.sqrt((table.err_dc_mw**2).mean()),
                "rmse_dc_comp_mw": np.sqrt((table.err_dc_comp_mw**2).mean()),
                "worst_branch_dc": "-".join(map(str, table.err_dc_mw.abs().idxmax()[1:])),
                "max_angle_err_dc_deg": (angles.va_dc - angles.va_ac).abs().max(),
                "max_angle_err_dc_comp_deg": (angles.va_dc_comp - angles.va_ac).abs().max(),
            }
        )

    out = configured_path("results_dir") / "validation"
    borders = pd.DataFrame(borders)
    borders["diff_mw"] = borders.model_mw - borders.nordpool_mw
    borders.to_csv(out / "nordpool_borders.csv", index=False, float_format="%.2f")
    branches = pd.concat(branches, ignore_index=True)
    branches.to_csv(out / "dc_branches.csv", index=False, float_format="%.4f")
    buses = pd.concat(buses, ignore_index=True)
    buses.to_csv(out / "dc_buses.csv", index=False, float_format="%.5f")
    summary = pd.DataFrame(summaries)
    summary.to_csv(out / "dc_summary.csv", index=False, float_format="%.3f")
    figure = plot_dc_errors(
        branches, buses, configured_path("results_dir") / "figures" / "dc_vs_ac.png"
    )

    print("Border and cut flows, model minus Nord Pool (MW), mean |diff| over the hours:")
    stats = borders.dropna(subset=["nordpool_mw"]).groupby(["kind", "name"]).diff_mw
    print(
        stats.agg(mean_abs=lambda d: d.abs().mean(), max_abs=lambda d: d.abs().max())
        .round(0)
        .to_string()
    )
    with pd.option_context("display.width", 200):
        print(
            "\n",
            summary.drop(columns=["criterion"]).to_string(
                index=False, float_format=lambda v: f"{v:.1f}"
            ),
        )
    by_rx = branches.groupby(pd.cut(branches.r_over_x, [-0.01, 0.05, 0.1, 0.2, 1.0]), observed=True)
    print(
        "\nMean |error| by R/X band (MW):\n",
        by_rx[["err_dc_mw", "err_dc_comp_mw"]].agg(lambda e: e.abs().mean()).round(1),
    )
    print(f"figure: {figure}")


if __name__ == "__main__":
    main()
