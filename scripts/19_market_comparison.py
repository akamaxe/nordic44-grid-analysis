"""The 2015 market data of ENTSO-E against the dataset and the model (questions P2 to P4).

Reads the ENTSO-E tables of script 18 and saved results of scripts 09, 12 and 14, and
writes:

- results/exchange_type.csv: on each border between modelled zones, Nord Pool's exchange
  (the dataset's input) against ENTSO-E's physical flows and scheduled exchanges, to tell
  which of the two the dataset holds;
- results/ntc_cuts.csv and results/figures/ntc_cuts.png: for each Swedish cut, the capacity
  offered to the day-ahead market on its border against the market flow, the hours the
  border was congested and the zones either side had different prices, and the equivalent's
  N-1 transfer capability;
- results/market_prices.csv, results/market_price_borders.csv and
  results/figures/market_prices.png: real zonal prices against the zone averages of the
  DC-OPF's nodal prices, and how often each border separates prices in each.

Usage: python scripts/19_market_comparison.py
"""

import argparse

import numpy as np
import pandas as pd

from n44.config import configured_path
from n44.io import entsoe
from n44.plotting import plot_market_prices, plot_ntc_cuts
from n44.snapshots import base_net, calendar
from n44.zones import CUTS, NORDPOOL_BORDERS, ZONES, nordpool_border_flows

BORDERS = list(NORDPOOL_BORDERS)
SEPARATED_EUR = 0.01  # prices this far apart count as separated
CONGESTED_MW = 1.0  # scheduled exchange within this of the offered capacity


def congested(flow: pd.Series, offered: pd.Series) -> pd.Series:
    """Hours the scheduled exchange used all the capacity offered (where any was offered)."""
    return (offered > 0) & (flow >= offered - CONGESTED_MW)


def exchange_type(nordpool: pd.DataFrame, physical: pd.DataFrame, scheduled: pd.DataFrame):
    rows = []
    for a, b in BORDERS:
        k = f"{a}->{b}"
        row = {"border": k, "nordpool_mean_mw": nordpool[k].mean()}
        for name, other in (("physical", physical[k]), ("scheduled", scheduled[k])):
            diff = (nordpool[k] - other).dropna()
            row |= {
                f"{name}_mean_mw": other.mean(),
                f"{name}_mae_mw": diff.abs().mean(),
                f"{name}_within_10mw": (diff.abs() <= 10).mean(),
                f"{name}_corr": nordpool[k].corr(other) if other.std() > 0 else np.nan,
            }
        rows.append(row)
    return pd.DataFrame(rows)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()
    processed = configured_path("processed_dir")
    results = configured_path("results_dir")
    real = ~calendar(2015).synthetic.to_numpy()

    def read(name):
        return pd.read_parquet(processed / f"entsoe_2015_{name}.parquet")[real]

    prices, capacity = read("prices"), read("capacity")
    physical = entsoe.net_flow(read("physical"), BORDERS)
    scheduled = entsoe.net_flow(read("scheduled"), BORDERS)
    nordpool = nordpool_border_flows(pd.read_parquet(processed / "nordpool_exchange.parquet"))[real]

    # --- which flows the dataset holds
    kinds = exchange_type(nordpool, physical, scheduled)
    kinds.to_csv(results / "exchange_type.csv", index=False, float_format="%.4g")
    print(kinds.round(3).to_string(index=False))

    # --- the Swedish cuts against the capacity offered to the market
    margins = pd.read_parquet(results / "annual" / "transfer_margins.parquet")
    capability = margins.pivot(index="h", columns="cut", values="capability_n1_mw")[real]
    sum_rate = pd.read_csv(results / "cut_statistics.csv").set_index("cut").sum_rate_a_mw
    rows = []
    for cut, (_, _, (a, b)) in CUTS.items():
        k, back = f"{a}->{b}", f"{b}->{a}"
        offered = capacity[k]
        full = congested(scheduled[k], offered)
        dearer_south = prices[b] - prices[a] > SEPARATED_EUR
        rows.append(
            {
                "cut": cut,
                "border": k,
                "capacity_median_mw": offered.median(),
                "capacity_p10_mw": offered.quantile(0.1),
                "capacity_p90_mw": offered.quantile(0.9),
                "capacity_min_mw": offered.min(),
                "capacity_max_mw": offered.max(),
                "capacity_back_median_mw": capacity[back].median(),
                "market_flow_mean_mw": nordpool[k].mean(),
                "market_flow_max_mw": nordpool[k].max(),
                "hours_flow_above_capacity": int((nordpool[k] > offered).sum()),
                "hours_congested": int(full.sum()),
                "hours_south_dearer": int(dearer_south.sum()),
                "hours_congested_and_dearer": int((full & dearer_south).sum()),
                "hours_north_dearer": int((prices[a] - prices[b] > SEPARATED_EUR).sum()),
                "sum_rate_a_mw": sum_rate[cut],
                "model_capability_n1_median_mw": capability[cut].median(),
                "capacity_over_model_capability": offered.median() / capability[cut].median(),
            }
        )
    cuts = pd.DataFrame(rows)
    cuts.to_csv(results / "ntc_cuts.csv", index=False, float_format="%.4g")
    print(cuts.T.to_string())
    plot_ntc_cuts(
        capacity,
        nordpool,
        capability,
        cuts,
        results / "figures" / "ntc_cuts.png",
    )

    # --- real zonal prices against the DC-OPF's nodal prices
    lmp = pd.read_parquet(results / "annual" / "opf_lmp.parquet")[real]
    zone = base_net().bus.zone
    model = lmp.T.groupby(zone.reindex(lmp.columns.astype(int)).to_numpy()).mean().T
    rows = []
    for z in ZONES:
        spread_real, spread_model = prices[z] - prices["SE3"], model[z] - model["SE3"]
        rows.append(
            {
                "zone": z,
                "real_mean_eur": prices[z].mean(),
                "model_mean_eur": model[z].mean(),
                "real_hours_apart_from_se3": int((spread_real.abs() > SEPARATED_EUR).sum()),
                "model_hours_apart_from_se3": int((spread_model.abs() > SEPARATED_EUR).sum()),
                # only where the model separates the zone at all
                "spread_correlation": spread_real.corr(spread_model)
                if (spread_model.abs() > SEPARATED_EUR).any()
                else np.nan,
            }
        )
    zones = pd.DataFrame(rows)
    rows = []
    for a, b in BORDERS:
        rows.append(
            {
                "border": f"{a}-{b}",
                "real_hours_separated": int(((prices[a] - prices[b]).abs() > SEPARATED_EUR).sum()),
                "model_hours_separated": int(((model[a] - model[b]).abs() > SEPARATED_EUR).sum()),
                "real_hours_congested": int(
                    (
                        congested(scheduled[f"{a}->{b}"], capacity[f"{a}->{b}"])
                        | congested(-scheduled[f"{a}->{b}"], capacity[f"{b}->{a}"])
                    ).sum()
                ),
            }
        )
    borders = pd.DataFrame(rows)
    zones.to_csv(results / "market_prices.csv", index=False, float_format="%.4g")
    borders.to_csv(results / "market_price_borders.csv", index=False)
    print(zones.round(2).to_string(index=False))
    print(borders.to_string(index=False))
    plot_market_prices(zones, borders, results / "figures" / "market_prices.png")


if __name__ == "__main__":
    main()
