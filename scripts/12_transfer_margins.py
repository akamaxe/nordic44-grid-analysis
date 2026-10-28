"""N-1 transfer capability of the Swedish cuts over the year (question P3).

The market dispatch is N-1 insecure in most hours of the equivalent (scripts 11 and 15), so a
margin measured from it is dominated by violations the transfer has nothing to do with. The
margin is therefore measured from each hour's **secure state**: the market dispatch after the
N-1 redispatch of scripts/15_redispatch.py.

For each cut (snitt 1, 2 and 4), the transfer goes between the two zones on either side of it
(SE1 to SE2, SE2 to SE3, SE3 to SE4), with generation shift keys proportional to each bus's
mean 2015 output. Shifting generation in zones further away would let their own
constraints, which have nothing to do with the cut, set its limit. From the secure state, the
margin is the extra transfer before a critical element reaches its limit:
- **N-0:** the intact network against RATE A;
- **N-1:** after any of the 71 outages, against RATE B.

Critical elements change their flow by at least 5 % of the transfer in that state, as in
flow-based capacity calculation.

The **N-1 transfer capability** of the cut is its flow in the secure state plus the part of
the margin that crosses the cut; the rest of a transfer loops through Norway and Finland. The
market flow is above it, by more than 50 MW, in the hours whose redispatch relieves the cut.
Both flows are the mean of the two ends of each crossing branch, lossless like the DC
estimate. The cut flows of scripts 08 and 09 are metered at one end, as PSS/E meters area
interchange, and include half the crossing branches' losses (20-50 MW in the median hour):
comparing a metered flow with this capability would count hours that are above it only by
those losses.

DC margins for every hour; AC margins for the representative hours, with the reference-model
solver, as a check.

Writes results/annual/transfer_margins.parquet, results/transfer_margins_summary.csv,
results/transfer_margins_ac_check.csv and results/figures/transfer_margins.png.
"""

from dataclasses import replace

import numpy as np
import pandas as pd
import yaml

from n44.acpf import grid_from_case
from n44.annual import pre_contingency_flows
from n44.config import configured_path, repo_path
from n44.contingency import (
    PTDF_THRESHOLD,
    ac_transfer_margin,
    branch_table,
    cut_crossing,
    dc_model,
    radial_branches,
    shunt_outage_shift,
    transfer_margins,
    transfer_sensitivity,
    transfer_shift,
)
from n44.io.dataset import Dataset
from n44.io.psse_parser import parse_raw
from n44.opf import machine_table
from n44.plotting import plot_transfer_margins
from n44.snapshots import SnapshotStore, base_net
from n44.zones import CUTS

HOURS = 8760
MATERIAL_MW = 50.0  # the market flow counts as above the capability beyond this
TOP_HOURS = 100  # the hours of highest market flow, summarised apart


def secure_state(pre: dict[str, np.ndarray], dc, table, units) -> dict[str, np.ndarray]:
    """The annual AC flows moved by each hour's N-1 redispatch (DC change, Q and V held)."""
    deltas = pd.read_parquet(configured_path("results_dir") / "annual" / "redispatch_units.parquet")
    sens = dc.ptdf.loc[table.index, units.bus].to_numpy()  # branches x units
    change = deltas.to_numpy() @ sens.T  # hours x branches
    out = dict(pre)
    out["p_i"] = pre["p_i"] + change
    out["p_j"] = pre["p_j"] - change
    return out


def main() -> None:
    net = base_net()
    table = branch_table(net)
    names = list(table.index)
    outages = [n for n in names if n not in radial_branches(table)]
    dc = dc_model(net)
    zone = net.bus.zone
    units = machine_table(net)
    outage_shift = shunt_outage_shift(net, dc, outages)
    gen_p = SnapshotStore().tables["gen_p"]
    mean_generation = gen_p.mean().groupby(lambda c: int(c.split("_")[0])).sum()
    market = pre_contingency_flows(names, table)
    pre = secure_state(market, dc, table, units)
    market_series = (market["p_i"] - market["p_j"]) / 2
    series = (pre["p_i"] - pre["p_j"]) / 2
    redispatch = pd.read_parquet(configured_path("results_dir") / "annual" / "redispatch.parquet")

    margins, summary, shifts = [], [], {}
    for cut, (north, south, (sending, receiving)) in CUTS.items():
        shift = transfer_shift(mean_generation, zone, [sending], [receiving])
        shifts[cut] = shift
        sens = transfer_sensitivity(dc, shift, table, outages)
        crossing = cut_crossing(table, zone, north, south)
        share = float(crossing @ sens[:, 0])  # of the transfer, through the cut
        parts = []
        for first in range(0, HOURS, 730):
            rows = slice(first, first + 730)
            chunk = {key: value[rows] for key, value in pre.items()}
            parts.append(
                transfer_margins(chunk, dc, shift, table, outages, outage_shift=outage_shift)
            )
        result = pd.concat(parts, ignore_index=True).assign(cut=cut, h=np.arange(HOURS))
        result["secure_flow_mw"] = series @ crossing
        result["market_flow_mw"] = market_series @ crossing
        result["capability_n1_mw"] = result.secure_flow_mw + share * result.margin_n1_mw.clip(
            lower=0
        )
        margins.append(result)
        limiting = result.limiting_n1.value_counts(normalize=True)
        outage = result.limiting_outage.value_counts(normalize=True)
        above = result.market_flow_mw > result.capability_n1_mw + MATERIAL_MW
        excess = (result.market_flow_mw - result.capability_n1_mw)[above]
        top = result.nlargest(TOP_HOURS, "market_flow_mw")
        top_limit = (top.limiting_n1 + " after " + top.limiting_outage).value_counts()
        summary.append(
            {
                "cut": cut,
                "crossing_share": share,
                "capability_n1_median_mw": result.capability_n1_mw.median(),
                "capability_n1_p10_mw": result.capability_n1_mw.quantile(0.1),
                "capability_n1_p90_mw": result.capability_n1_mw.quantile(0.9),
                "market_flow_median_mw": result.market_flow_mw.median(),
                "hours_market_above_capability": int(above.sum()),
                "n1_margin_median_mw": result.margin_n1_mw.median(),
                "n1_margin_negative_hours": int((result.margin_n1_mw < -1).sum()),
                "limiting_n1": limiting.index[0],
                "limiting_n1_share": limiting.iloc[0],
                "limiting_outage": outage.index[0],
                "limiting_outage_share": outage.iloc[0],
                "hours_at_n1_limit_share": float((result.margin_n1_mw.abs() < 1).mean()),
                "excess_median_mw": excess.median(),
                "excess_p90_mw": excess.quantile(0.9),
                "top_market_min_mw": top.market_flow_mw.min(),
                "top_market_max_mw": top.market_flow_mw.max(),
                "top_capability_median_mw": top.capability_n1_mw.median(),
                "top_capability_p10_mw": top.capability_n1_mw.quantile(0.1),
                "top_capability_p90_mw": top.capability_n1_mw.quantile(0.9),
                "top_shortfall_median_mw": (top.market_flow_mw - top.capability_n1_mw).median(),
                "top_limit": top_limit.index[0],
                "top_limit_hours": int(top_limit.iloc[0]),
                "top_redispatch_mean_mw": redispatch.n1_volume_mw.iloc[top.h].mean(),
            }
        )
    margins = pd.concat(margins, ignore_index=True)
    margins.to_parquet(configured_path("results_dir") / "annual" / "transfer_margins.parquet")
    summary = pd.DataFrame(summary)
    out = configured_path("results_dir")
    summary.to_csv(out / "transfer_margins_summary.csv", index=False, float_format="%.4g")
    print(summary.T.to_string())

    # AC check from the secure state of the representative hours
    with open(repo_path("config", "representative_hours.yaml"), encoding="utf-8") as fh:
        hours = [row["h"] for row in yaml.safe_load(fh)]
    deltas = pd.read_parquet(out / "annual" / "redispatch_units.parquet")
    ds = Dataset()
    ratio = (table.rate_a / table.rate_b).to_numpy()
    positions = [names.index(o) for o in outages]
    checks = []
    for h in hours:
        grid = grid_from_case(parse_raw(ds.raw_text(h, "after")))
        s_spec = grid.s_spec.copy()
        for bus, element, d in zip(units.bus, units.element, deltas.loc[h].to_numpy(), strict=True):
            if element == "gen":  # the swing bus takes the balance itself
                s_spec[grid.model.positions([bus])[0]] += d / grid.model.sbase
        grid = replace(grid, s_spec=s_spec)
        for cut, shift in shifts.items():
            sens = transfer_sensitivity(dc, shift, table, outages)
            critical = np.abs(sens) >= PTDF_THRESHOLD
            critical[positions, np.arange(1, len(outages) + 1)] = False
            ac = ac_transfer_margin(grid, shift, positions, critical, ratio)
            row = margins[(margins.cut == cut) & (margins.h == h)].iloc[0]
            checks.append(
                {
                    "h": h,
                    "cut": cut,
                    "dc_margin_n0_mw": row.margin_n0_mw,
                    "ac_margin_n0_mw": ac["ac_margin_n0_mw"],
                    "dc_margin_n1_mw": row.margin_n1_mw,
                    "ac_margin_n1_mw": ac["ac_margin_n1_mw"],
                }
            )
            print(f"  AC check h={h} {cut}: {checks[-1]}", flush=True)
    checks = pd.DataFrame(checks)
    checks.to_csv(out / "transfer_margins_ac_check.csv", index=False, float_format="%.0f")
    for label in ("n0", "n1"):
        diff = (
            (checks[f"ac_margin_{label}_mw"] - checks[f"dc_margin_{label}_mw"])
            .replace([np.inf, -np.inf], np.nan)
            .dropna()
        )
        print(
            f"AC - DC margin {label}: median {diff.median():.0f} MW, "
            f"max abs {diff.abs().max():.0f} MW over {len(diff)} of {len(checks)} cases"
        )
    plot_transfer_margins(margins, summary, out / "figures" / "transfer_margins.png")


if __name__ == "__main__":
    main()
