"""Bidding zones, exchange loads, borders and cuts of the Nordic 44 model.

Zones. The PSS/E area of each bus is its Nord Pool bidding zone (see data/README.md).

Borders. A border between two modelled zones is the set of branches joining them, plus
the exchange-load pairs that stand for links with no branch: Fenno-Skan (SE3-FI) and the
NO4-FI exchange. Branch flows are taken at the metered end, as PSS/E measures area
interchange. The equivalent model has two ties that no Nord Pool border matches: 3000-3115
(SE3-SE1) and 3249-7100 (SE2-FI).

Cuts. Svenska kraftnät's transfer sections (*snitt*) are defined here as flows between
sets of Swedish zones. A cut therefore also collects the flow on the artificial SE3-SE1 tie.

The functions work on a branch flow table with columns ``i``, ``j``, ``metered``, ``p_i_mw``
and ``p_j_mw``, as returned by ``n44.ybus.branch_flows`` or ``n44.results.branch_flows``.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

ZONES = ("NO1", "NO2", "NO3", "NO4", "NO5", "SE1", "SE2", "SE3", "SE4", "FI1")


@dataclass(frozen=True)
class ExchangeLoad:
    """A load that stands for an exchange (HVDC link or AC border) on one side."""

    bus: int
    load_id: str
    zone: str
    link: str  # e.g. "SE3-FI1", or "SE3-DK1" for a neighbour outside the model
    nordpool: str  # Nord Pool column on this side ("A_B" > 0 = import into A from B)


# Mapping of the original toolbox (nordic44/n44.py). Load P = -(Nord Pool exchange).
EXCHANGE_LOADS = (
    ExchangeLoad(3020, "1", "SE3", "SE3-FI1", "SE3_FI"),
    ExchangeLoad(7000, "6", "FI1", "SE3-FI1", "FI_SE3"),
    ExchangeLoad(6701, "1", "NO4", "NO4-FI1", "NO_FI"),
    ExchangeLoad(7100, "3", "FI1", "NO4-FI1", "FI_NO"),
    ExchangeLoad(3360, "1", "SE3", "SE3-DK1", "SE3_DK1"),
    ExchangeLoad(5610, "1", "NO2", "NO2-DK1", "NO_DK"),
    ExchangeLoad(5620, "1", "NO2", "NO2-NL", "NO_NL"),
    ExchangeLoad(6701, "3", "NO4", "NO4-RU", "NO_RU"),
    ExchangeLoad(7010, "1", "FI1", "FI1-RU", "FI_RU"),
    ExchangeLoad(7020, "1", "FI1", "FI1-EE", "FI_EE"),
    ExchangeLoad(8500, "4", "SE4", "SE4-DK2", "SE4_DK2"),
    ExchangeLoad(8600, "1", "SE4", "SE4-DE", "SE_DE"),
    ExchangeLoad(8700, "1", "SE4", "SE4-PL", "SE_PL"),
)
EXCHANGE_LOAD_KEYS = frozenset((e.bus, e.load_id) for e in EXCHANGE_LOADS)

# Links to neighbours outside the model, by the zone names of the ENTSO-E platform, with the
# exchange load that carries each.
EXTERNAL_LINKS = {
    ("NO2", "DK1"): (5610, "1"),
    ("NO2", "NL"): (5620, "1"),
    ("NO4", "RU"): (6701, "3"),
    ("SE3", "DK1"): (3360, "1"),
    ("SE4", "DK2"): (8500, "4"),
    ("SE4", "DE"): (8600, "1"),
    ("SE4", "PL"): (8700, "1"),
    ("FI1", "EE"): (7020, "1"),
    ("FI1", "RU"): (7010, "1"),
}
# Links built after 2015 have no connection bus in the equivalent: NordLink (NO2-DE, 2020),
# North Sea Link (NO2-GB, 2021) and NordBalt (SE4-LT, 2016). Their export counts as demand of
# their zone, spread over its loads. On an exchange load they would load a radial branch far
# beyond what it was built for (NordLink and North Sea Link at Feda put 3.4 GW on 5600-5620).
NEW_LINKS = (("NO2", "DE"), ("NO2", "GB"), ("SE4", "LT"))

# Links inside the model carried by loads: the load on the sending zone's side gives the flow.
LOAD_LINKS = {("SE3", "FI1"): (3020, "1"), ("NO4", "FI1"): (6701, "1")}

# Nord Pool borders between modelled zones, oriented A -> B, with the Nord Pool column written
# from A's side ("A_B", or the country aggregate when the border is the only one, as for
# NO4-FI). A positive column value is an import into A, so the flow A -> B is -column.
NORDPOOL_BORDERS = {
    ("SE1", "SE2"): "SE1_SE2",
    ("SE2", "SE3"): "SE2_SE3",
    ("SE3", "SE4"): "SE3_SE4",
    ("SE1", "FI1"): "SE1_FI",
    ("SE3", "FI1"): "SE3_FI",
    ("NO4", "FI1"): "NO_FI",
    ("SE1", "NO4"): "SE1_NO4",
    ("SE2", "NO3"): "SE2_NO3",
    ("SE2", "NO4"): "SE2_NO4",
    ("SE3", "NO1"): "SE3_NO1",
    ("NO1", "NO2"): "NO1_NO2",
    ("NO1", "NO3"): "NO1_NO3",
    ("NO1", "NO5"): "NO1_NO5",
    ("NO5", "NO2"): "NO5_NO2",
    ("NO4", "NO3"): "NO4_NO3",
}
# Ties of the equivalent that have no Nord Pool counterpart
ARTIFICIAL_BORDERS = (("SE1", "SE3"), ("SE2", "FI1"))

# Swedish transfer sections as flows between zone sets, with the Nord Pool border they follow
CUTS = {
    "snitt 1 (SE1->SE2)": (("SE1",), ("SE2", "SE3", "SE4"), ("SE1", "SE2")),
    "snitt 2 (SE2->SE3)": (("SE1", "SE2"), ("SE3", "SE4"), ("SE2", "SE3")),
    "snitt 4 (SE3->SE4)": (("SE1", "SE2", "SE3"), ("SE4",), ("SE3", "SE4")),
}


def bus_zones(bus: pd.DataFrame, area: pd.DataFrame) -> pd.Series:
    """Zone name of each bus number, from the PSS/E bus and area tables."""
    names = area.set_index("i").arname
    return pd.Series(bus.area.map(names).to_numpy(), index=bus.i.to_numpy(), name="zone")


def _metered_flow(flows: pd.DataFrame, zone: pd.Series) -> pd.DataFrame:
    """Each inter-zonal branch as a flow from the zone of its metered end to the other zone."""
    zi, zj = flows.i.map(zone).to_numpy(), flows.j.map(zone).to_numpy()
    metered_i = (flows.metered == flows.i).to_numpy()
    out = pd.DataFrame(
        {
            "from_zone": np.where(metered_i, zi, zj),
            "to_zone": np.where(metered_i, zj, zi),
            "p_mw": np.where(metered_i, flows.p_i_mw, flows.p_j_mw),
        }
    )
    return out[out.from_zone != out.to_zone]


def zone_to_zone(
    flows: pd.DataFrame, zone: pd.Series, load_p: pd.Series | None = None
) -> pd.Series:
    """Net flow between every ordered pair of zones (A, B), A < B, in MW (A -> B positive).

    ``load_p`` is the P of the loads indexed by (bus, load id); it adds the flow of the links
    carried by loads.
    """
    ties = _metered_flow(flows, zone)
    flip = ties.from_zone > ties.to_zone
    a = np.where(flip, ties.to_zone, ties.from_zone)
    b = np.where(flip, ties.from_zone, ties.to_zone)
    p = np.where(flip, -ties.p_mw, ties.p_mw)
    net = (
        pd.Series(p, index=pd.MultiIndex.from_arrays([a, b], names=["a", "b"]))
        .groupby(level=["a", "b"])
        .sum()
    )
    if load_p is not None:
        for (za, zb), key in LOAD_LINKS.items():
            value = load_p.get(key, 0.0)
            pair = (za, zb) if za < zb else (zb, za)
            net[pair] = net.get(pair, 0.0) + (value if za < zb else -value)
    return net


def border_flows(
    flows: pd.DataFrame, zone: pd.Series, load_p: pd.Series | None = None
) -> pd.Series:
    """Flow on each Nord Pool border and artificial tie, oriented as in ``NORDPOOL_BORDERS``."""
    net = zone_to_zone(flows, zone, load_p)
    out = {}
    for a, b in (*NORDPOOL_BORDERS, *ARTIFICIAL_BORDERS):
        value = net.get((a, b), 0.0) if a < b else -net.get((b, a), 0.0)
        out[f"{a}->{b}"] = value
    return pd.Series(out, name="p_mw")


def cut_flows(flows: pd.DataFrame, zone: pd.Series) -> pd.Series:
    """Flow across each cut in ``CUTS``, in MW (north to south positive)."""
    ties = _metered_flow(flows, zone)
    out = {}
    for name, (north, south, _) in CUTS.items():
        forward = ties.from_zone.isin(north) & ties.to_zone.isin(south)
        backward = ties.from_zone.isin(south) & ties.to_zone.isin(north)
        out[name] = ties.p_mw[forward].sum() - ties.p_mw[backward].sum()
    return pd.Series(out, name="p_mw")


def nordpool_border_flows(exchange: pd.DataFrame) -> pd.DataFrame:
    """Nord Pool flow on each border, oriented as in ``NORDPOOL_BORDERS``.

    ``exchange`` has one column per Nord Pool exchange column ("A_B", from the SE, NO and FI
    files) and one row per hour.
    """
    return pd.DataFrame(
        {f"{a}->{b}": -exchange[column] for (a, b), column in NORDPOOL_BORDERS.items()}
    )
