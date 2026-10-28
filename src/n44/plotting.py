"""Static figures for the report and the README (matplotlib).

Colours follow the project's data-viz palette: categorical slots in fixed order, recessive
grey chrome, and text in ink colours, never in series colours. A map is a chart where any two
marks can be neighbours, so it uses at most three categorical colours. That is why the zone
map colours buses by country and names the ten bidding zones with direct labels.
"""

from __future__ import annotations

from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.patheffects as pe  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
from matplotlib.lines import Line2D  # noqa: E402

from n44.zones import LOAD_LINKS  # noqa: E402

INK = {
    "primary": "#0b0b0b",
    "secondary": "#52514e",
    "muted": "#898781",
    "grid": "#e1e0d9",
    "surface": "#fcfcfb",
}
SERIES = ("#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#6250d6", "#e34948")
COUNTRY_COLOR = {"NO": SERIES[0], "SE": SERIES[1], "FI": SERIES[2]}
COUNTRY_NAME = {"NO": "Norway", "SE": "Sweden", "FI": "Finland"}
REFERENCE_LATITUDE = 62.0  # equirectangular projection, x scaled by cos(latitude)
# Zone label anchors (longitude, latitude), placed in clear space next to each zone
ZONE_LABELS = {
    "NO1": (11.9, 60.6),
    "NO2": (4.6, 58.3),
    "NO3": (8.4, 63.6),
    "NO4": (11.6, 67.4),
    "NO5": (4.3, 60.9),
    "SE1": (21.9, 67.2),
    "SE2": (15.1, 63.75),
    "SE3": (15.6, 58.4),
    "SE4": (15.6, 55.8),
    "FI1": (26.6, 61.4),
}


def _chart_axes(ax) -> None:
    """Recessive chrome for an x-y chart: hairline grid, no box, muted ticks."""
    ax.set_facecolor(INK["surface"])
    for name, spine in ax.spines.items():
        spine.set_visible(name == "bottom")
        spine.set_color("#c3c2b7")
    ax.grid(axis="y", color=INK["grid"], lw=0.6)
    ax.set_axisbelow(True)
    ax.tick_params(colors=INK["muted"], labelsize=7.5, length=0)
    ax.xaxis.label.set_color(INK["secondary"])
    ax.yaxis.label.set_color(INK["secondary"])


BLUE_RAMP = (
    "#cde2fb",
    "#b7d3f6",
    "#9ec5f4",
    "#86b6ef",
    "#6da7ec",
    "#5598e7",
    "#3987e5",
    "#2a78d6",
    "#256abf",
    "#1c5cab",
    "#184f95",
    "#104281",
    "#0d366b",
)


def _save(fig, out: Path) -> Path:
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out, facecolor=INK["surface"])
    plt.close(fig)
    return out


def _title(ax, title: str, subtitle: str | None = None) -> None:
    ax.set_title(title, loc="left", fontsize=9.5, color=INK["primary"], pad=16 if subtitle else 6)
    if subtitle:
        ax.text(
            0,
            1.02,
            subtitle,
            transform=ax.transAxes,
            fontsize=7.5,
            color=INK["secondary"],
            va="bottom",
        )


def plot_cut_duration(
    model: pd.DataFrame, nordpool: pd.DataFrame, capacity: pd.Series, out: Path
) -> Path:
    """Duration curves of the Swedish cuts: model flow and Nord Pool's border flow.

    One panel per cut. The dotted line is the sum of the RATE A of the cut's branches: an
    upper bound of the equivalent model's thermal capacity, not the operational limit.
    """
    fig, axes = plt.subplots(1, len(model.columns), figsize=(10.5, 3.8), dpi=200, sharey=True)
    fig.patch.set_facecolor(INK["surface"])
    hours = np.arange(1, len(model) + 1)
    for ax, cut in zip(axes, model.columns, strict=True):
        _chart_axes(ax)
        ax.axhline(0, color="#c3c2b7", lw=1.0, zorder=1)
        ax.plot(
            hours,
            np.sort(model[cut].to_numpy())[::-1],
            color=SERIES[0],
            lw=2,
            label="N44 model",
            zorder=3,
        )
        ax.plot(
            hours,
            np.sort(nordpool[cut].to_numpy())[::-1],
            color=SERIES[1],
            lw=2,
            label="Nord Pool",
            zorder=2,
        )
        ax.axhline(capacity[cut], color=INK["muted"], lw=1.0, ls=(0, (1, 2)), zorder=1)
        ax.text(
            len(hours),
            capacity[cut],
            f"Σ RATE A {capacity[cut]:,.0f} MW",
            ha="right",
            va="bottom",
            fontsize=7,
            color=INK["secondary"],
        )
        ax.set_xlabel("hours of 2015, sorted", fontsize=8)
        gap = np.abs(np.sort(model[cut].to_numpy()) - np.sort(nordpool[cut].to_numpy())).max()
        means = f"mean N44 {model[cut].mean():,.0f} MW, Nord Pool {nordpool[cut].mean():,.0f} MW"
        _title(ax, cut, means + (" (curves coincide)" if gap < 50 else ""))
    axes[0].set_ylabel("north-to-south flow (MW)", fontsize=8)
    axes[0].legend(frameon=False, fontsize=7.5, labelcolor=INK["secondary"], loc="lower left")
    fig.suptitle(
        "Transfer through the Swedish cuts, duration curves",
        x=0.01,
        ha="left",
        fontsize=12,
        color=INK["primary"],
    )
    fig.tight_layout(rect=(0, 0, 1, 0.93))
    return _save(fig, out)


def plot_corridor_loading(loading: pd.DataFrame, out: Path) -> Path:
    """Duration curves of the most loaded branch in each cut, with the 80/90/100 % lines."""
    fig, ax = plt.subplots(figsize=(7.2, 4.0), dpi=200)
    fig.patch.set_facecolor(INK["surface"])
    _chart_axes(ax)
    hours = np.arange(1, len(loading) + 1)
    for level in (80, 90, 100):
        ax.axhline(level, color=INK["muted"], lw=0.8, ls=(0, (1, 2)), zorder=1)
        ax.text(
            len(hours),
            level,
            f"{level} %",
            ha="right",
            va="bottom",
            fontsize=7,
            color=INK["secondary"],
        )
    for k, cut in enumerate(loading.columns):
        curve = np.sort(loading[cut].to_numpy())[::-1]
        above = int((loading[cut] >= 100).sum())
        ax.plot(
            hours,
            curve,
            color=SERIES[k],
            lw=2,
            label=f"{cut}, {above:,} h at or above 100 %",
            zorder=2,
        )
    ax.set_xlabel("hours of 2015, sorted", fontsize=8)
    ax.set_ylabel("loading of the most loaded branch (% of RATE A)", fontsize=8)
    _title(
        ax,
        "Most loaded branch in each Swedish cut",
        "terminal current for lines, MVA for transformers, as PSS/E rates them",
    )
    ax.legend(
        frameon=False,
        fontsize=7.5,
        labelcolor=INK["secondary"],
        loc="upper right",
        bbox_to_anchor=(1.0, 0.92),
    )
    fig.tight_layout()
    return _save(fig, out)


def plot_month_hour(panels: list[tuple[str, str, pd.DataFrame, str]], out: Path) -> Path:
    """Month x hour-of-day heatmaps on the sequential blue ramp, one colour bar each.

    ``panels`` holds (title, subtitle, table with rows = months and columns = hours, unit).
    """
    from matplotlib.colors import LinearSegmentedColormap

    cmap = LinearSegmentedColormap.from_list("blue", BLUE_RAMP)
    fig, axes = plt.subplots(1, len(panels), figsize=(5.0 * len(panels), 3.9), dpi=200)
    fig.patch.set_facecolor(INK["surface"])
    months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
    for ax, (title, subtitle, table, unit) in zip(np.atleast_1d(axes), panels, strict=True):
        image = ax.imshow(table.to_numpy(), aspect="auto", cmap=cmap, origin="upper")
        ax.set_yticks(range(12), months)
        ax.set_xticks(range(0, 24, 3), [f"{h:02d}" for h in range(0, 24, 3)])
        ax.tick_params(colors=INK["muted"], labelsize=7.5, length=0)
        for spine in ax.spines.values():
            spine.set_visible(False)
        ax.set_xlabel("hour of the day (local time)", fontsize=8, color=INK["secondary"])
        _title(ax, title, subtitle)
        bar = fig.colorbar(image, ax=ax, fraction=0.046, pad=0.03)
        bar.outline.set_visible(False)
        bar.ax.tick_params(colors=INK["muted"], labelsize=7, length=0)
        bar.set_label(unit, fontsize=7.5, color=INK["secondary"])
    fig.tight_layout()
    return _save(fig, out)


def plot_drivers(
    flow: pd.Series, drivers: list[tuple[str, pd.Series]], title: str, out: Path
) -> Path:
    """Scatter of a cut flow against candidate drivers, with Pearson r in each subtitle."""
    fig, axes = plt.subplots(
        1, len(drivers), figsize=(4.6 * len(drivers), 3.9), dpi=200, sharey=True
    )
    fig.patch.set_facecolor(INK["surface"])
    for ax, (label, series) in zip(np.atleast_1d(axes), drivers, strict=True):
        _chart_axes(ax)
        ax.scatter(series, flow, s=4, color=SERIES[0], alpha=0.25, edgecolor="none")
        r = series.corr(flow)
        ax.set_xlabel(label, fontsize=8)
        _title(ax, label.split(" (")[0], f"Pearson r = {r:.2f}, 8,760 hours")
    np.atleast_1d(axes)[0].set_ylabel(f"{flow.name} (MW)", fontsize=8)
    fig.suptitle(title, x=0.01, ha="left", fontsize=12, color=INK["primary"])
    fig.tight_layout(rect=(0, 0, 1, 0.92))
    return _save(fig, out)


def plot_border_errors(mae: pd.Series, monthly: pd.DataFrame, out: Path) -> Path:
    """P1: model minus Nord Pool per border (annual mean |difference|) and its monthly course.

    ``monthly`` holds up to three borders (columns) by month (rows).
    """
    fig, (left, right) = plt.subplots(
        1, 2, figsize=(10.0, 4.4), dpi=200, gridspec_kw={"width_ratios": [1.05, 1]}
    )
    fig.patch.set_facecolor(INK["surface"])
    _chart_axes(left)
    left.grid(axis="y", visible=False)
    left.grid(axis="x", color=INK["grid"], lw=0.6)
    ordered = mae.sort_values()
    left.barh(range(len(ordered)), ordered.to_numpy(), color=SERIES[0], height=0.7)
    left.set_yticks(range(len(ordered)), ordered.index)
    for k, value in enumerate(ordered.to_numpy()):
        left.text(value, k, f" {value:,.0f}", va="center", fontsize=7, color=INK["secondary"])
    left.set_xlabel("mean |model - Nord Pool| over 2015 (MW)", fontsize=8)
    _title(left, "Where the equivalent's border flows depart from Nord Pool")

    _chart_axes(right)
    months = np.arange(1, 13)
    for k, border in enumerate(monthly.columns):
        right.plot(
            months,
            monthly[border].to_numpy(),
            color=SERIES[k],
            lw=2,
            marker="o",
            markersize=4,
            label=border,
        )
    right.set_xticks(months, ["J", "F", "M", "A", "M", "J", "J", "A", "S", "O", "N", "D"])
    right.set_ylabel("mean |model - Nord Pool| (MW)", fontsize=8)
    right.set_ylim(bottom=0)
    right.legend(frameon=False, fontsize=7.5, labelcolor=INK["secondary"], loc="upper center")
    _title(right, "Monthly course on three borders")
    fig.suptitle(
        "N44 border flows against Nord Pool, 2015",
        x=0.01,
        ha="left",
        fontsize=12,
        color=INK["primary"],
    )
    fig.tight_layout(rect=(0, 0, 1, 0.93))
    return _save(fig, out)


def plot_validation_residuals(
    buses: pd.DataFrame, branches: pd.DataFrame, tolerances: dict, out: Path
) -> Path:
    """pandapower minus PSS/E against the PSS/E value, for voltages and branch flows.

    The differences are orders of magnitude below the tolerances. Drawing the
    tolerance band would flatten them into a line, so each panel states its margin instead.
    """
    hours = buses.h.nunique()
    p = np.r_[branches.p_i_mw_ref, branches.p_j_mw_ref]
    dp = np.r_[branches.d_p_i_mw, branches.d_p_j_mw]
    q = np.r_[branches.q_i_mvar_ref, branches.q_j_mvar_ref]
    dq = np.r_[branches.d_q_i_mvar, branches.d_q_j_mvar]
    panels = [
        (
            "Bus voltage magnitude",
            buses.vm_ref,
            buses.dvm * 1e6,
            "PSS/E voltage (pu)",
            "difference (10⁻⁶ pu)",
            abs(buses.dvm).max(),
            tolerances["vm_pu"],
            "pu",
        ),
        (
            "Bus voltage angle",
            buses.va_ref,
            buses.dva * 1e3,
            "PSS/E angle (degrees)",
            "difference (10⁻³ degrees)",
            abs(buses.dva).max(),
            tolerances["va_deg"],
            "°",
        ),
        (
            "Branch active power",
            p,
            dp,
            "PSS/E flow (MW)",
            "difference (MW)",
            np.abs(dp).max(),
            tolerances["p_mw"],
            "MW",
        ),
        (
            "Branch reactive power",
            q,
            dq,
            "PSS/E flow (Mvar)",
            "difference (Mvar)",
            np.abs(dq).max(),
            tolerances["q_mvar"],
            "Mvar",
        ),
    ]
    fig, axes = plt.subplots(2, 2, figsize=(9.0, 6.4), dpi=200)
    fig.patch.set_facecolor(INK["surface"])
    for ax, (title, x, y, xlabel, ylabel, worst, tol, unit) in zip(axes.flat, panels, strict=True):
        _chart_axes(ax)
        ax.axhline(0, color="#c3c2b7", lw=1.0, zorder=1)
        ax.scatter(x, y, s=9, color=SERIES[0], alpha=0.55, edgecolor="none", zorder=2)
        ax.set_xlabel(xlabel, fontsize=8)
        ax.set_ylabel(ylabel, fontsize=8)
        ax.set_title(title, loc="left", fontsize=9.5, color=INK["primary"], pad=16)
        ax.text(
            0,
            1.02,
            f"max |diff| {worst:.2g} {unit}, tolerance {tol:g} {unit} ({tol / worst:,.0f}× margin)",
            transform=ax.transAxes,
            fontsize=7.5,
            color=INK["secondary"],
            va="bottom",
        )
    fig.suptitle(
        f"pandapower against PSS/E on {hours} representative hours of 2015",
        x=0.01,
        ha="left",
        fontsize=12,
        color=INK["primary"],
    )
    fig.text(
        0.01,
        0.005,
        "Branch flows are terminal flows at both ends. The PSS/E flows "
        "follow from its stored voltages, rounded to 5 decimals (VM) and 4 (VA)",
        fontsize=6.5,
        color=INK["muted"],
    )
    fig.tight_layout(rect=(0, 0.02, 1, 0.96))
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out, facecolor=INK["surface"])
    plt.close(fig)
    return out


def plot_dc_errors(branches: pd.DataFrame, buses: pd.DataFrame, out: Path) -> Path:
    """DC against AC: branch flow error against loading, and mean angle error per zone.

    Two series in each panel: the plain DC power flow and the loss-compensated one. The
    subtitle of the left panel gives the rank correlation of the plain-DC error with loading
    and with R/X, because R/X is the usual suspect and here it explains little.
    """
    series = (
        ("plain DC", "err_dc_mw", "va_dc", SERIES[0]),
        ("loss-compensated DC", "err_dc_comp_mw", "va_dc_comp", SERIES[1]),
    )
    fig, (left, right) = plt.subplots(1, 2, figsize=(9.6, 4.1), dpi=200)
    fig.patch.set_facecolor(INK["surface"])
    for ax in (left, right):
        _chart_axes(ax)
        ax.axhline(0, color="#c3c2b7", lw=1.0, zorder=1)

    for label, err, _, color in series:
        left.scatter(
            branches.loading_a_pct,
            branches[err],
            s=10,
            color=color,
            alpha=0.5,
            edgecolor="none",
            label=label,
            zorder=2,
        )
    error = branches.err_dc_mw.abs()
    rho_loading = error.corr(branches.loading_a_pct, method="spearman")
    rho_rx = error.corr(branches.r_over_x, method="spearman")
    left.set_xlabel("AC loading (% of RATE A)", fontsize=8)
    left.set_ylabel("DC minus AC active flow (MW)", fontsize=8)
    left.set_title(
        "Flow error grows with loading, not with R/X",
        loc="left",
        fontsize=9.5,
        color=INK["primary"],
        pad=16,
    )
    left.text(
        0,
        1.02,
        f"rank correlation of |plain-DC error| with loading {rho_loading:.2f}, "
        f"with R/X {rho_rx:.2f}",
        transform=left.transAxes,
        fontsize=7.5,
        color=INK["secondary"],
        va="bottom",
    )
    left.legend(
        frameon=False, fontsize=7.5, labelcolor=INK["secondary"], loc="lower left", markerscale=1.6
    )

    zones = ["NO1", "NO2", "NO3", "NO4", "NO5", "SE1", "SE2", "SE3", "SE4", "FI1"]
    x = np.arange(len(zones))
    width = 0.38
    for k, (label, _, angle, color) in enumerate(series):
        mean = (buses[angle] - buses.va_ac).groupby(buses.zone).mean().reindex(zones)
        right.bar(
            x + (k - 0.5) * (width + 0.02), mean, width=width, color=color, label=label, zorder=2
        )
    right.set_xticks(x, zones)
    right.legend(frameon=False, fontsize=7.5, labelcolor=INK["secondary"], loc="upper right")
    right.set_ylabel("mean DC minus AC angle (degrees)", fontsize=8)
    right.set_title(
        "Without loss compensation, southern Norway drifts by about 5°",
        loc="left",
        fontsize=9.5,
        color=INK["primary"],
        pad=16,
    )
    right.text(
        0,
        1.02,
        "angles relative to the slack bus 3300 (SE3)",
        transform=right.transAxes,
        fontsize=7.5,
        color=INK["secondary"],
        va="bottom",
    )
    hours = branches.h.nunique()
    fig.suptitle(
        f"DC power flow against AC on {hours} representative hours",
        x=0.01,
        ha="left",
        fontsize=12,
        color=INK["primary"],
    )
    fig.tight_layout(rect=(0, 0, 1, 0.94))
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out, facecolor=INK["surface"])
    plt.close(fig)
    return out


def plot_powerfactory_validation(
    buses: pd.DataFrame, branches: pd.DataFrame, tolerances: dict, out: Path, top: int = 4
) -> Path:
    """PowerFactory against pandapower, per bus and per branch, for both model variants.

    Each dot is the largest difference of one bus (left) or branch (right) over all hours,
    ranked by the "as imported" variant. The few elements that stand out are labelled: they
    sit next to transformers whose RAW ratio is off PowerFactory's tap grid.
    """
    series = (
        ("as imported", "imported", SERIES[0]),
        ("exact transformer ratios", "exact", SERIES[1]),
    )
    dq = branches.assign(dq=np.maximum(branches.d_q_i_mvar.abs(), branches.d_q_j_mvar.abs()))
    kind = dq.kind.map({"line": "line ", "transformer": "transformer "})
    branch_name = kind + dq.i.astype(str) + "–" + dq.j.astype(str)
    floor = 1e-10  # voltage-controlled buses agree to round-off and are left out
    panels = (
        (
            "Bus voltage magnitude",
            buses.assign(value=buses.dvm.abs(), name="bus " + buses.bus.astype(str)),
            "max |ΔV| over all hours (pu)",
            tolerances["vm_pu"],
            "pu",
            "buses",
        ),
        (
            "Branch reactive power at the terminals",
            dq.assign(value=dq.dq, name=branch_name),
            "max |ΔQ| over all hours (Mvar)",
            tolerances["q_mvar"],
            "Mvar",
            "branches",
        ),
    )
    fig, axes = plt.subplots(1, 2, figsize=(9.6, 4.3), dpi=200)
    fig.patch.set_facecolor(INK["surface"])
    hidden = 0
    for ax, (title, data, ylabel, tol, unit, noun) in zip(axes, panels, strict=True):
        _chart_axes(ax)
        worst = data.groupby(["variant", "name"]).value.max().unstack("variant")
        worst = worst.sort_values("imported", ascending=False)
        if noun == "buses":
            shown = worst.max(axis=1) >= floor
            hidden, worst = int((~shown).sum()), worst[shown]
        rank = np.arange(1, len(worst) + 1)
        for label, variant, color in series:
            ax.scatter(
                rank, worst[variant], s=16, color=color, edgecolor="none", label=label, zorder=3
            )
        # Labels stacked in the headroom above the ranked curve, log-spaced downwards
        first = worst.imported.iloc[0]
        ax.set_yscale("log")
        ax.set_ylim(top=first * 10)
        for k, (name, row) in enumerate(worst.head(top).iterrows()):
            ax.annotate(
                name,
                (k + 1, row.imported),
                xytext=(top + 2.5, first * 5 / 2.2**k),
                textcoords="data",
                fontsize=7,
                color=INK["secondary"],
                va="center",
                bbox={"boxstyle": "square,pad=0.1", "fc": INK["surface"], "ec": "none"},
                arrowprops={"arrowstyle": "-", "color": INK["muted"], "lw": 0.5, "shrinkB": 3},
            )
        ax.axhline(tol, color=INK["muted"], lw=1.0, ls=(0, (1, 2)), zorder=1)
        ax.text(
            len(worst),
            tol,
            f"tolerance {tol:g} {unit}",
            ha="right",
            va="bottom",
            fontsize=7,
            color=INK["secondary"],
        )
        ax.set_xlabel(f"{noun}, ranked by the 'as imported' difference", fontsize=8)
        ax.set_ylabel(ylabel, fontsize=8)
        imported, exact = worst.imported.max(), worst.exact.max()
        _title(
            ax,
            title,
            f"largest difference {imported:.2g} {unit} as imported, {exact:.2g} {unit} exact",
        )
        ax.legend(
            frameon=False,
            fontsize=7.5,
            labelcolor=INK["secondary"],
            loc="upper right",
            bbox_to_anchor=(1, 0.86),
        )
    fig.suptitle(
        "PowerFactory matches pandapower once its importer's tap rounding is undone",
        x=0.01,
        ha="left",
        fontsize=12,
        color=INK["primary"],
    )
    fig.text(
        0.01,
        0.005,
        f"{buses.h.nunique()} hours of 2015. PowerFactory's PSS/E importer rounds each "
        "transformer ratio (WINDV) to the nearest tap position, and 'exact' restores the RAW "
        f"ratio.\nThe {hidden} voltage-controlled buses are left out, as they agree to better "
        f"than {floor:g} pu in both variants.",
        fontsize=6.5,
        color=INK["muted"],
    )
    fig.tight_layout(rect=(0, 0.04, 1, 0.94))
    return _save(fig, out)


def _short(name: str) -> str:
    """'transformer 5300-5301-1' -> 'T 5300–5301', 'line 3100-3200-2' -> '3100–3200 (2)'."""
    kind, key = name.split(" ", 1)
    i, j, ckt = key.split("-")
    label = f"{i}–{j}" + (f" ({ckt})" if ckt != "1" else "")
    return ("T " if kind == "transformer" else "") + label


def plot_n1_ranking(rank: pd.DataFrame, out: Path, top: int = 12) -> Path:
    """Hours of 2015 in which each outage breaks N-1, for the worst outages.

    Two disjoint reasons per (hour, outage): no AC solution at all, or a solution with an
    induced overload (a branch beyond RATE B that was within it before the outage).
    """
    data = rank.assign(overload_only=rank.insecure_h - rank.no_solution_h)
    data = data.nlargest(top, "insecure_h").iloc[::-1]
    fig, ax = plt.subplots(figsize=(8.2, 4.6), dpi=200)
    fig.patch.set_facecolor(INK["surface"])
    _chart_axes(ax)
    ax.grid(axis="y", visible=False)
    ax.grid(axis="x", color=INK["grid"], lw=0.6)
    y = np.arange(len(data))
    ax.barh(y, data.no_solution_h, height=0.62, color=SERIES[1], label="no AC solution", zorder=2)
    ax.barh(
        y,
        data.overload_only,
        left=data.no_solution_h + 30,
        height=0.62,
        color=SERIES[0],
        label="overload or voltage below 0.90 p.u.",
        zorder=2,
    )
    for k, row in enumerate(data.itertuples()):
        ax.text(
            row.insecure_h + 90,
            k,
            f"{row.insecure_h:,}",
            va="center",
            fontsize=7,
            color=INK["secondary"],
        )
    ax.set_yticks(y, [_short(n) for n in data.outage], fontsize=7.5)
    ax.tick_params(axis="y", colors=INK["secondary"])
    ax.set_xlim(0, 8760)
    ax.set_xlabel("hours of 2015 in which the outage breaks N-1", fontsize=8)
    _title(
        ax,
        "One transformer outage has no AC solution in most hours",
        "AC N-1 of all 8,760 hours, outages ranked by insecure hours (T marks a transformer)",
    )
    ax.legend(frameon=False, fontsize=7.5, labelcolor=INK["secondary"], loc="lower right")
    fig.tight_layout()
    return _save(fig, out)


def plot_n1_map(pairs: pd.DataFrame, rank: pd.DataFrame, out: Path, top: int = 12) -> Path:
    """Contingency x overloaded branch: hours in which the outage overloads the branch."""
    rows = rank.nlargest(top, "overload_h").outage
    data = pairs.loc[rows]
    cols = data.sum().nlargest(top).index
    data = data[cols]
    fig, ax = plt.subplots(figsize=(8.6, 5.4), dpi=200)
    fig.patch.set_facecolor(INK["surface"])
    ax.set_facecolor(INK["surface"])
    values = data.to_numpy().astype(float)
    shown = np.where(values > 0, values, np.nan)
    from matplotlib.colors import LinearSegmentedColormap, LogNorm

    cmap = LinearSegmentedColormap.from_list("blue", BLUE_RAMP)
    image = ax.imshow(shown, cmap=cmap, norm=LogNorm(vmin=1, vmax=8760), aspect="auto")
    for (r, c), v in np.ndenumerate(values):
        if v > 0:
            ax.text(
                c,
                r,
                f"{int(v):,}",
                ha="center",
                va="center",
                fontsize=5.8,
                color="white" if v > 600 else INK["primary"],
            )
    ax.set_xticks(range(len(cols)), [_short(n) for n in cols], rotation=45, ha="right", fontsize=7)
    ax.set_yticks(range(len(rows)), [_short(n) for n in rows], fontsize=7)
    ax.tick_params(colors=INK["secondary"], length=0)
    for spine in ax.spines.values():
        spine.set_visible(False)
    ax.set_xlabel(
        "overloaded branch (beyond RATE B, within it before the outage)",
        fontsize=8,
        color=INK["secondary"],
    )
    ax.set_ylabel("outage", fontsize=8, color=INK["secondary"])
    bar = fig.colorbar(image, ax=ax, shrink=0.7, pad=0.02)
    bar.set_label("hours of 2015", fontsize=7.5, color=INK["secondary"])
    bar.ax.tick_params(labelsize=7, colors=INK["muted"])
    bar.outline.set_visible(False)
    _title(
        ax,
        "Which outage overloads which branch",
        "hours of 2015 with the induced overload, for the most frequent outages and branches",
    )
    fig.tight_layout()
    return _save(fig, out)


def plot_n1_screening(
    arrays: dict,
    table: pd.DataFrame,
    accuracy: pd.DataFrame,
    errors: dict,
    out: Path,
    step: int = 6,
) -> Path:
    """DC screening against AC N-1: loading estimates, and the work it saves.

    Left: post-contingency loading predicted by the screening against the AC result, for
    every outage, branch and sixth hour (density). Right: share of (hour, outage) pairs the
    screening sends to AC at each threshold, with the number of AC-insecure pairs missed.
    """
    to_b = (table.rate_a / table.rate_b).to_numpy()
    ac = np.asarray(arrays["ac_loading"][::step], float) * to_b
    dc = np.asarray(arrays["dc_loading"][::step], float) * to_b
    ok = np.isfinite(ac) & np.isfinite(dc) & (ac > 20)
    fig, (left, right) = plt.subplots(
        1, 2, figsize=(9.6, 4.2), dpi=200, gridspec_kw={"width_ratios": [1.15, 1]}
    )
    fig.patch.set_facecolor(INK["surface"])
    for ax in (left, right):
        _chart_axes(ax)
    from matplotlib.colors import LinearSegmentedColormap, LogNorm

    cmap = LinearSegmentedColormap.from_list("blue", BLUE_RAMP)
    left.hexbin(
        ac[ok],
        dc[ok],
        gridsize=70,
        cmap=cmap,
        norm=LogNorm(),
        mincnt=1,
        extent=(20, 220, 20, 220),
        linewidths=0,
    )
    left.plot([20, 220], [20, 220], color=INK["muted"], lw=1.0, ls=(0, (1, 2)))
    left.set_xlim(20, 220)
    left.set_ylim(20, 220)
    left.set_xlabel("AC post-contingency loading (% of RATE B)", fontsize=8)
    left.set_ylabel("DC screening estimate (% of RATE B)", fontsize=8)
    _title(
        left,
        "Screening estimate against AC",
        f"mean abs. error {errors['error_mae_pct']:.1f} points, 95 % within "
        f"{errors['error_p95_abs_pct']:.1f}",
    )
    share = 100 - accuracy.ac_runs_saved_pct
    right.plot(accuracy.threshold_pct, share, color=SERIES[0], lw=2, marker="o", ms=4, zorder=3)
    for row, value in zip(accuracy.itertuples(), share, strict=True):
        thermal = row.missed_overload + row.missed_no_solution
        right.annotate(
            f"{thermal:,}",
            (row.threshold_pct, value),
            xytext=(0, 7),
            textcoords="offset points",
            ha="center",
            fontsize=6.5,
            color=INK["secondary"],
        )
    right.invert_xaxis()
    right.set_xlabel("screening threshold (% of RATE B)", fontsize=8)
    right.set_ylabel("(hour, outage) pairs sent to AC (%)", fontsize=8)
    right.set_ylim(0, max(share) * 1.25)
    thermal = errors["insecure_overload"] + errors["insecure_no_solution"]
    _title(
        right,
        "AC runs needed against cases missed",
        f"labels count missed overloads and collapses, of {thermal:,}. "
        f"Low voltages ({errors['insecure_low_voltage']:,}) need AC",
    )
    fig.tight_layout()
    return _save(fig, out)


def plot_transfer_margins(margins: pd.DataFrame, summary: pd.DataFrame, out: Path) -> Path:
    """Duration curves of each Swedish cut's N-1 transfer capability and its market flow.

    The capability is the cut's flow in the hour's secure (redispatched) state plus the part
    of the remaining N-1 margin that crosses the cut. DC estimates on each hour's AC flows.
    """
    cuts = list(summary.cut)
    fig, axes = plt.subplots(1, len(cuts), figsize=(10.5, 3.9), dpi=200, sharey=True)
    fig.patch.set_facecolor(INK["surface"])
    hours = np.arange(1, 8761)
    for ax, cut in zip(axes, cuts, strict=True):
        _chart_axes(ax)
        data = margins[margins.cut == cut]
        ax.axhline(0, color="#c3c2b7", lw=1.0, zorder=1)
        for label, column, color in (
            ("market flow", "market_flow_mw", SERIES[0]),
            ("N-1 transfer capability", "capability_n1_mw", SERIES[1]),
        ):
            values = np.sort(data[column].to_numpy())[::-1]
            ax.plot(hours[: len(values)], values, color=color, lw=2, label=label, zorder=2)
        row = summary.set_index("cut").loc[cut]
        _title(
            ax,
            cut,
            f"capability median {row.capability_n1_median_mw:,.0f} MW, market above it "
            f"in {row.hours_market_above_capability:,} h",
        )
        ax.set_xlabel("hours of 2015, sorted", fontsize=8)
        ax.set_xlim(0, 8760)
    axes[0].set_ylabel("flow north to south (MW)", fontsize=8)
    axes[0].legend(frameon=False, fontsize=7.5, labelcolor=INK["secondary"], loc="lower left")
    fig.suptitle(
        "N-1 transfer capability of the Swedish cuts against the market flow",
        x=0.01,
        ha="left",
        fontsize=12,
        color=INK["primary"],
    )
    fig.text(
        0.01,
        0.005,
        "The capability is the flow in the hour's N-1 secure (redispatched) state plus the part "
        "of the remaining margin that crosses the cut.\nThe transfer runs between the zones "
        "either side of the cut, with shift keys from mean 2015 output and critical elements "
        "at 5 % sensitivity.",
        fontsize=6.5,
        color=INK["muted"],
    )
    fig.tight_layout(rect=(0, 0.03, 1, 0.94))
    return _save(fig, out)


def plot_redispatch(results: pd.DataFrame, zones: pd.DataFrame, out: Path) -> Path:
    """Redispatch needed by month (N-0 and N-1) and where it moves generation (N-1).

    Left: monthly redispatched energy, grouped bars for the two criteria. Right: energy
    moved up and down in each zone under N-1, diverging around zero.
    """
    month = pd.Series(
        pd.date_range("2015-01-01", periods=len(results), freq="h").month, index=results.index
    )
    monthly = pd.DataFrame(
        {
            "intact network (N-0)": results.n0_volume_mw.groupby(month).sum() / 1e3,
            "N-0 and N-1": results.n1_volume_mw.groupby(month).sum() / 1e3,
        }
    )
    fig, (left, right) = plt.subplots(
        1, 2, figsize=(10.0, 4.2), dpi=200, gridspec_kw={"width_ratios": [1.5, 1]}
    )
    fig.patch.set_facecolor(INK["surface"])
    for ax in (left, right):
        _chart_axes(ax)
    x = np.arange(12)
    width = 0.4
    for k, (label, color) in enumerate(zip(monthly.columns, SERIES[:2], strict=True)):
        left.bar(
            x + (k - 0.5) * (width + 0.02),
            monthly[label],
            width=width,
            color=color,
            label=label,
            zorder=2,
        )
    left.set_xticks(x, ["J", "F", "M", "A", "M", "J", "J", "A", "S", "O", "N", "D"])
    left.set_ylabel("redispatched energy (GWh)", fontsize=8)
    total = results.n1_volume_mw.sum() / 1e6
    _title(
        left,
        "Redispatch needed to make the market dispatch secure",
        f"{total:.1f} TWh in 2015 for N-0 and N-1, "
        f"{results.n0_volume_mw.sum() / 1e6:.1f} TWh for N-0 alone",
    )
    left.set_ylim(0, monthly.to_numpy().max() * 1.28)  # headroom for the legend
    left.legend(frameon=False, fontsize=7.5, labelcolor=INK["secondary"], loc="upper left")
    order = ["NO1", "NO2", "NO3", "NO4", "NO5", "SE1", "SE2", "SE3", "SE4", "FI1"]
    data = zones.reindex(order).fillna(0)
    y = np.arange(len(order))[::-1]
    right.barh(y, data.up_twh, color=SERIES[1], height=0.6, label="up", zorder=2)
    right.barh(y, -data.down_twh, color=SERIES[0], height=0.6, label="down", zorder=2)
    right.axvline(0, color="#c3c2b7", lw=1.0)
    right.set_yticks(y, order, fontsize=7.5)
    right.grid(axis="y", visible=False)
    right.grid(axis="x", color=INK["grid"], lw=0.6)
    right.set_xlabel("energy moved (TWh), N-0 and N-1", fontsize=8)
    _title(right, "Where generation moves", "down in SE1-SE2 and western Norway, up in SE3")
    right.legend(frameon=False, fontsize=7.5, labelcolor=INK["secondary"], loc="lower right")
    fig.tight_layout()
    return _save(fig, out)


def plot_opf_prices(zones: pd.DataFrame, binding: pd.DataFrame, out: Path, top: int = 10) -> Path:
    """Mean nodal price per zone, and the branches that bind most often in the DC-OPF.

    The dotted line is the mean energy component (the price at the slack bus); each bar's
    distance from it is the zone's mean congestion component. Prices follow from the cost
    assumptions of config/costs.yaml, not from market data.
    """
    order = ["NO1", "NO2", "NO3", "NO4", "NO5", "SE1", "SE2", "SE3", "SE4", "FI1"]
    data = zones.reindex(order)
    energy = float((data.mean_nodal_price_eur - data.mean_congestion_eur).mean())
    fig, (left, right) = plt.subplots(1, 2, figsize=(10.0, 4.2), dpi=200)
    fig.patch.set_facecolor(INK["surface"])
    for ax in (left, right):
        _chart_axes(ax)
    x = np.arange(len(order))
    left.bar(x, data.mean_nodal_price_eur, width=0.62, color=SERIES[0], zorder=2)
    left.axhline(energy, color=INK["muted"], lw=1.0, ls=(0, (1, 2)), zorder=3)
    left.text(
        -0.4,
        energy + 0.3,
        f"energy component {energy:.1f}",
        ha="left",
        va="bottom",
        fontsize=7,
        color=INK["secondary"],
    )
    for k, value in enumerate(data.mean_nodal_price_eur):
        if abs(value - energy) >= 0.05:  # only the zones whose price departs from it
            left.text(
                k, value + 0.3, f"{value:.1f}", ha="center", fontsize=6.8, color=INK["secondary"]
            )
    left.set_xticks(x, order, fontsize=7.5)
    left.set_ylabel("mean nodal price over 2015 (EUR/MWh)", fontsize=8)
    _title(
        left,
        "Nodal prices under the cost assumptions",
        "zone average of the buses' DC-OPF prices, loss-compensated, with RATE A limits",
    )
    data_b = binding.head(top).iloc[::-1]
    y = np.arange(len(data_b))
    right.barh(y, data_b.hours, height=0.6, color=SERIES[0], zorder=2)
    for k, row in enumerate(data_b.itertuples()):
        right.text(
            row.hours + 60,
            k,
            f"{row.hours:,} h, {row.mean_shadow_eur:.0f} EUR/MWh",
            va="center",
            fontsize=6.8,
            color=INK["secondary"],
        )
    right.set_yticks(y, [_short(n) for n in data_b.index], fontsize=7.5)
    right.grid(axis="y", visible=False)
    right.grid(axis="x", color=INK["grid"], lw=0.6)
    right.set_xlim(0, max(8760, data_b.hours.max() * 1.45))
    right.set_xlabel("hours of 2015 in which the branch limit binds", fontsize=8)
    _title(right, "Where the network binds", "hours binding and mean shadow price")
    fig.tight_layout()
    return _save(fig, out)


REINFORCEMENTS = {  # scenario: (label, colour, line style); dashed where N-0 binds
    "base": ("2015 network", INK["secondary"], "-"),
    "ac_line": ("+ AC Hjälta–Ringhals", SERIES[0], "--"),
    "hvdc": ("+ VSC Hjälta–Ringhals", SERIES[1], "--"),
    "ac_line_north": ("+ AC Hjälta–Grundfors", SERIES[0], "-"),
    "hvdc_north": ("+ VSC Grundfors–Ringhals", SERIES[1], "-"),
}


def plot_reinforcements(
    summary: pd.DataFrame, frames: dict[str, pd.DataFrame], step: int, out: Path
) -> Path:
    """N-1 redispatch and snitt 2's N-1 transfer capability for each reinforcement.

    Left: N-1 redispatch per year. Right: duration curves of snitt 2's N-1 capability, with
    the market flow. Colour is the technology (AC blue, VSC orange) and the line style the
    place: dashed on Hjälta-Ringhals, where the intact network binds; solid at the N-1
    bottleneck at Grundfors. Bars of the dashed options are drawn as tints.
    """
    cut = "snitt 2 (SE2->SE3)"
    data = summary.set_index("scenario")
    fig, (left, right) = plt.subplots(
        1, 2, figsize=(10.4, 4.3), dpi=200, gridspec_kw={"width_ratios": [1, 1.3]}
    )
    fig.patch.set_facecolor(INK["surface"])
    for ax in (left, right):
        _chart_axes(ax)
    base = float(data.loc["base", "n1_redispatch_twh"])
    y = np.arange(len(REINFORCEMENTS))[::-1]
    for k, (tag, (_, color, style)) in enumerate(REINFORCEMENTS.items()):
        value = float(data.loc[tag, "n1_redispatch_twh"])
        left.barh(
            y[k],
            value,
            height=0.62,
            color=color,
            alpha=0.4 if style == "--" else 1.0,
            zorder=2,
        )
        pct = 100 * (value / base - 1)
        change = "" if tag == "base" else f"  {pct:+.{1 if abs(pct) < 1 else 0}f} %"
        left.text(
            value + base * 0.02,
            y[k],
            f"{value:.1f} TWh{change}",
            va="center",
            fontsize=7.5,
            color=INK["primary"],
        )
    left.set_yticks(y, [v[0] for v in REINFORCEMENTS.values()], fontsize=7.5)
    left.grid(axis="y", visible=False)
    left.grid(axis="x", color=INK["grid"], lw=0.6)
    left.set_xlim(0, max(float(data.loc[t, "n1_redispatch_twh"]) for t in REINFORCEMENTS) * 1.4)
    left.set_xlabel("N-1 redispatch per year (TWh)", fontsize=8)
    _title(left, "Redispatch to make the market dispatch secure", f"one hour in {step} of 2015")

    for tag, (label, color, style) in REINFORCEMENTS.items():
        capability = np.sort(frames[tag][f"capability {cut}"].dropna().to_numpy())[::-1]
        hours = np.arange(1, len(capability) + 1) * step
        right.plot(hours, capability, color=color, ls=style, lw=1.8, label=label, zorder=2)
    market = np.sort(frames["base"][f"market {cut}"].dropna().to_numpy())[::-1]
    right.plot(
        np.arange(1, len(market) + 1) * step,
        market,
        color=INK["primary"],
        ls=(0, (1, 1.5)),
        lw=1.4,
        label="market flow, 2015",
        zorder=3,
    )
    right.axhline(0, color="#c3c2b7", lw=1.0)
    right.set_xlabel("hours of 2015, sorted", fontsize=8)
    right.set_ylabel("snitt 2, north to south (MW)", fontsize=8)
    _title(
        right,
        "Snitt 2's N-1 transfer capability",
        "from each hour's secure state, dashed where N-0 binds",
    )
    right.legend(frameon=False, fontsize=7, labelcolor=INK["secondary"], loc="lower left")
    fig.tight_layout()
    return _save(fig, out)


def plot_northern_load(summary: pd.DataFrame, step: int, out: Path) -> Path:
    """Redispatch and cut flows as new industrial load is added in SE1/SE2.

    Left: N-0 and N-1 redispatch per year against the new load (0 = the 2015 network), with
    the two wind variants at 5 GW as open markers. Right: mean flow across the three Swedish
    cuts against the new load.
    """
    data = summary.set_index("scenario")
    sweep = data[data.index.str.startswith("north_load") | (data.index == "base")]
    load = np.array(
        [0.0 if s == "base" else float(s.split("_")[-1].replace("gw", "")) for s in sweep.index]
    )
    order = np.argsort(load)
    load, sweep = load[order], sweep.iloc[order]
    fig, (left, right) = plt.subplots(1, 2, figsize=(10.4, 4.3), dpi=200)
    fig.patch.set_facecolor(INK["surface"])
    for ax in (left, right):
        _chart_axes(ax)
    curves = [sweep[c].to_numpy() for c in ("n0_redispatch_twh", "n1_redispatch_twh")]
    for column, label, color in (
        ("n0_redispatch_twh", "intact network (N-0)", SERIES[0]),
        ("n1_redispatch_twh", "N-0 and N-1", SERIES[1]),
    ):
        left.plot(load, sweep[column], color=color, lw=2, marker="o", ms=4, label=label, zorder=2)
        for tag in [t for t in data.index if t.startswith("north_wind")]:
            wind = tag.split("wind")[-1].replace("gw", "")
            value = data.loc[tag, column]
            left.scatter(
                5.0,
                value,
                s=34,
                facecolor=INK["surface"],
                edgecolor=color,
                lw=1.5,
                zorder=3,
            )
            # labelled on the right of the marker, unless a curve passes close by there
            right_side = all(abs(np.interp(5.6, load, c) - value) > 1.5 for c in curves)
            left.annotate(
                f"+{wind} GW wind",
                (5.0, value),
                xytext=(6, 0) if right_side else (-6, 0),
                textcoords="offset points",
                fontsize=7,
                color=INK["secondary"],
                ha="left" if right_side else "right",
                va="center",
                path_effects=[pe.withStroke(linewidth=3, foreground=INK["surface"])],
                zorder=4,
            )
    left.set_ylim(bottom=0)
    left.set_xlabel("new industrial load in SE1/SE2 (GW)", fontsize=8)
    left.set_ylabel("redispatch per year (TWh)", fontsize=8)
    _title(
        left,
        "Redispatch as northern demand grows",
        f"same market dispatch elsewhere, one hour in {step} of 2015, open markers with new wind",
    )
    left.legend(frameon=False, fontsize=7.5, labelcolor=INK["secondary"], loc="upper left")
    cuts = [c for c in data.columns if c.startswith("mean flow ")]
    for column, color in zip(cuts, SERIES, strict=False):
        right.plot(load, sweep[column], color=color, lw=2, marker="o", ms=4, zorder=2)
        right.annotate(
            column.replace("mean flow ", ""),
            (load[-1], sweep[column].iloc[-1]),
            xytext=(6, 0),
            textcoords="offset points",
            fontsize=7,
            color=INK["secondary"],
            va="center",
        )
    right.axhline(0, color="#c3c2b7", lw=1.0)
    right.set_xlim(load.min() - 0.3, load.max() * 1.32)  # room for the labels
    right.set_xticks(np.arange(0, load.max() + 1, 2))
    right.set_xlabel("new industrial load in SE1/SE2 (GW)", fontsize=8)
    right.set_ylabel("mean flow north to south (MW)", fontsize=8)
    _title(right, "Northern load turns the cut flows around", "mean over the evaluated hours")
    fig.tight_layout()
    return _save(fig, out)


def _style(ax) -> None:
    ax.set_facecolor(INK["surface"])
    for spine in ax.spines.values():
        spine.set_visible(False)
    ax.set_xticks([])
    ax.set_yticks([])


def read_coordinates(path: Path) -> pd.DataFrame:
    """Bus latitude and longitude from the toolbox's coordinate workbook, by bus name."""
    sheet = pd.read_excel(path, sheet_name=0, header=None)
    names = sheet.iloc[1:, 2].dropna().astype(str).str.strip()
    latlon = sheet.loc[names.index, 3].astype(str).str.split(",", expand=True).astype(float)
    return pd.DataFrame(
        {"lat": latlon[0].to_numpy(), "lon": latlon[1].to_numpy()},
        index=names.str.upper().to_numpy(),
    )


def plot_zone_map(net, coordinates: pd.DataFrame, out: Path) -> Path:
    """Map of the network: buses coloured by country, bidding zones labelled.

    Ties between zones are drawn darker; the two artificial ties of the equivalent are dashed,
    and the links carried by exchange-load pairs (Fenno-Skan, NO4-FI) are dotted.
    """
    names = net.bus.name.str.strip().str.upper()
    missing = sorted(set(names) - set(coordinates.index))
    if missing:
        raise KeyError(f"no coordinates for buses {missing}")
    lat = coordinates.lat[names].to_numpy()
    lon = coordinates.lon[names].to_numpy()
    xy = pd.DataFrame(
        {"x": lon * np.cos(np.deg2rad(REFERENCE_LATITUDE)), "y": lat}, index=net.bus.index
    )
    zone = net.bus.zone

    fig, ax = plt.subplots(figsize=(7.2, 8.6), dpi=200)
    fig.patch.set_facecolor(INK["surface"])
    _style(ax)

    artificial = {frozenset(("SE1", "SE3")), frozenset(("SE2", "FI1"))}
    branches = pd.concat(
        [
            net.line[["from_bus", "to_bus"]].rename(columns={"from_bus": "a", "to_bus": "b"}),
            net.trafo[["hv_bus", "lv_bus"]].rename(columns={"hv_bus": "a", "lv_bus": "b"}),
        ],
        ignore_index=True,
    ).drop_duplicates()
    for a, b in branches.itertuples(index=False):
        za, zb = zone[a], zone[b]
        if za == zb:
            style = {"color": INK["muted"], "lw": 0.9, "zorder": 1}
        elif frozenset((za, zb)) in artificial:
            style = {"color": INK["secondary"], "lw": 1.6, "ls": (0, (4, 2)), "zorder": 2}
        else:
            style = {"color": INK["secondary"], "lw": 1.8, "zorder": 2}
        ax.plot(xy.x[[a, b]], xy.y[[a, b]], solid_capstyle="round", **style)
    loads = net.load.set_index(["bus", "psse_id"]).index
    for (za, zb), sending in LOAD_LINKS.items():
        peer = {("SE3", "FI1"): (7000, "6"), ("NO4", "FI1"): (7100, "3")}[(za, zb)]
        if sending in loads and peer in loads:
            a, b = sending[0], peer[0]
            ax.plot(
                xy.x[[a, b]], xy.y[[a, b]], color=INK["secondary"], lw=1.6, ls=(0, (1, 2)), zorder=2
            )

    size = net.bus.vn_kv.map({420.0: 46.0, 300.0: 32.0}).fillna(20.0)
    for country, color in COUNTRY_COLOR.items():
        members = zone.str.startswith(country)
        ax.scatter(
            xy.x[members],
            xy.y[members],
            s=size[members],
            color=color,
            edgecolor=INK["surface"],
            linewidth=1.2,
            zorder=3,
        )

    halo = [pe.withStroke(linewidth=3, foreground=INK["surface"])]
    scale = np.cos(np.deg2rad(REFERENCE_LATITUDE))
    for name, (label_lon, label_lat) in ZONE_LABELS.items():
        ax.text(
            label_lon * scale,
            label_lat,
            name,
            fontsize=10,
            fontweight="bold",
            color=INK["primary"],
            ha="center",
            va="center",
            path_effects=halo,
            zorder=4,
        )

    handles = [
        Line2D(
            [],
            [],
            marker="o",
            ls="",
            markersize=7,
            markerfacecolor=c,
            markeredgecolor=INK["surface"],
            label=COUNTRY_NAME[k],
        )
        for k, c in COUNTRY_COLOR.items()
    ]
    handles += [
        Line2D([], [], color=INK["muted"], lw=0.9, label="Line or transformer in a zone"),
        Line2D([], [], color=INK["secondary"], lw=1.8, label="Tie between zones"),
        Line2D(
            [],
            [],
            color=INK["secondary"],
            lw=1.6,
            ls=(0, (4, 2)),
            label="Tie with no Nord Pool border",
        ),
        Line2D(
            [],
            [],
            color=INK["secondary"],
            lw=1.6,
            ls=(0, (1, 2)),
            label="Link modelled as a load pair",
        ),
    ]
    legend = ax.legend(
        handles=handles,
        loc="lower right",
        frameon=False,
        fontsize=8,
        labelcolor=INK["secondary"],
        borderaxespad=0.2,
    )
    legend.set_zorder(5)
    ax.set_title(
        "Nordic 44 equivalent grid by bidding zone",
        loc="left",
        fontsize=12,
        color=INK["primary"],
        pad=14,
    )
    ax.text(
        0,
        1.005,
        "44 buses, 67 lines and 12 transformers. Marker size shows 420, 300 or 135 kV",
        transform=ax.transAxes,
        fontsize=8,
        color=INK["secondary"],
        va="bottom",
    )
    ax.set_aspect("equal")
    ax.margins(0.06)
    fig.text(
        0.01,
        0.01,
        "Bus positions from the N44 geo-coordinates of ALSETLab/Nordic44-Nordpool",
        fontsize=6.5,
        color=INK["muted"],
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out, bbox_inches="tight", facecolor=INK["surface"])
    plt.close(fig)
    return out


DISPLAY_NAMES = {
    "HJALTA": "Hjälta",
    "BLAFALLI": "Blåfalli",
    "MALMO": "Malmö",
    "HOGASEN": "Högåsen",
    "JARPSTROMMEN": "Järpströmmen",
}


def _display(name: str) -> str:
    name = name.strip().upper()
    return DISPLAY_NAMES.get(name, name.title())


def _branch_buses(key: str) -> tuple[int, int]:
    """Bus numbers of a branch key such as "line 6000-6100-1"."""
    i, j, _ = key.split(" ")[1].split("-", 2)
    return int(i), int(j)


def _binding_states(pairs: pd.DataFrame, key: str, names: pd.Series) -> str:
    """When a branch limits the redispatch, as the second line of its map label.

    Names the fewest states (at most two) that cover 90 % of its hours, prefixed with
    "mostly" when some of its hours are left out.
    """
    rows = pairs[pairs.branch == key]
    total = rows.h.nunique()
    counts = rows.groupby("state").h.nunique().sort_values(ascending=False, kind="stable")
    states, covered = [], set()
    for state in counts.index[:2]:
        states.append(state)
        covered |= set(rows.h[rows.state == state])
        if len(covered) >= 0.9 * total:
            break
    lost = []
    for state in states:
        if state != "intact":
            a, b = _branch_buses(state)
            pair = f"{_display(names[a])}–{_display(names[b])}"
            lost.append(f"the {pair} transformer" if state.startswith("transformer") else pair)
    parts = [f"after the loss of {lost[0]}"] if lost else []
    parts += [f"or {name}" for name in lost[1:]]
    if "intact" in states:
        parts.append("in the intact network" if not lost else "or in the intact network")
    text = " ".join(parts) if len(" ".join(parts)) <= 40 else "\n".join(parts)
    return ("mostly " if len(covered) < 0.995 * total else "") + text


def _distance_to_segment(points: np.ndarray, start: np.ndarray, end: np.ndarray) -> np.ndarray:
    """Distance from each of ``points`` to the segment from ``start`` to ``end``."""
    d = end - start
    t = np.clip((points - start) @ d / max(float(d @ d), 1e-12), 0.0, 1.0)
    return np.hypot(*(points - (start + t[:, None] * d)).T)


def plot_bottleneck_map(net, coordinates: pd.DataFrame, pairs: pd.DataFrame, out: Path) -> Path:
    """Map of the branches that limit the N-1 redispatch, and the outages behind them.

    ``pairs`` has one row per binding limit of the redispatch: the hour ``h``, the limited
    ``branch`` and the ``state`` it binds in ("intact" or the outage). Branches are drawn on the
    sequential blue ramp by the hours they bind, the others as hairlines; the outages behind
    the most frequent limits are crossed, and the five most frequent limits are labelled.
    """
    from matplotlib.colors import LinearSegmentedColormap

    names = net.bus.name.str.strip().str.upper()
    missing = sorted(set(names) - set(coordinates.index))
    if missing:
        raise KeyError(f"no coordinates for buses {missing}")
    scale = np.cos(np.deg2rad(REFERENCE_LATITUDE))
    xy = pd.DataFrame(
        {
            "x": coordinates.lon[names].to_numpy() * scale,
            "y": coordinates.lat[names].to_numpy(),
        },
        index=net.bus.index,
    )
    limits = pairs.drop_duplicates(["h", "branch"]).branch.value_counts()
    outages = pairs[pairs.state != "intact"].drop_duplicates(["h", "state"])
    triggers = outages.state.value_counts()

    fig, ax = plt.subplots(figsize=(10.4, 6.5), dpi=200)
    fig.subplots_adjust(left=0.01, right=0.99, bottom=0.04, top=0.9)
    fig.patch.set_facecolor(INK["surface"])
    _style(ax)
    branches = pd.concat(
        [
            net.line[["from_bus", "to_bus"]].rename(columns={"from_bus": "a", "to_bus": "b"}),
            net.trafo[["hv_bus", "lv_bus"]].rename(columns={"hv_bus": "a", "lv_bus": "b"}),
        ],
        ignore_index=True,
    ).drop_duplicates()
    for a, b in branches.itertuples(index=False):
        ax.plot(xy.x[[a, b]], xy.y[[a, b]], color="#cfcec8", lw=0.8, zorder=1)

    ramp = LinearSegmentedColormap.from_list("blue", BLUE_RAMP[3:])
    peak = float(limits.max())
    for key, hours in limits.sort_values().items():
        a, b = _branch_buses(key)
        ax.plot(
            xy.x[[a, b]],
            xy.y[[a, b]],
            color=ramp(hours / peak),
            lw=1.4 + 3.6 * hours / peak,
            solid_capstyle="round",
            zorder=2,
        )
    ax.scatter(xy.x, xy.y, s=9, color=INK["secondary"], edgecolor=INK["surface"], lw=0.6, zorder=3)
    halo = [pe.withStroke(linewidth=3, foreground=INK["surface"])]
    crosses = np.array(
        [xy.loc[list(_branch_buses(key))].mean().to_numpy() for key in triggers.index[:4]]
    )
    for cx, cy in crosses:
        ax.scatter(
            cx,
            cy,
            marker="x",
            s=52,
            color=INK["primary"],
            lw=1.7,
            zorder=4,
            path_effects=halo,
        )
    for name, (label_lon, label_lat) in ZONE_LABELS.items():
        ax.text(
            label_lon * scale,
            label_lat,
            name,
            fontsize=8.5,
            color=INK["muted"],
            fontweight="bold",
            ha="center",
            va="center",
            path_effects=halo,
            zorder=4,
        )

    # the most frequent limits, labelled in a column on the side nearer to them
    ax.set_aspect("equal")
    ax.set_xlim(xy.x.min() - 7.4, xy.x.max() + 7.4)
    ax.set_ylim(xy.y.min() - 0.5, xy.y.max() + 0.5)
    fig.canvas.draw()  # fixes the axes box, so text heights convert to data units
    origin, unit = ax.transData.transform([(0, 0), (0, 1)])
    line = 13.5 / ((unit[1] - origin[1]) * 72.0 / fig.dpi)  # 13.5 pt per label line
    x_mid = xy.x.median()
    left_x, right_x = xy.x.min() - 1.4, xy.x.max() + 0.9
    labels = []
    for key in limits.index[:5]:
        a, b = _branch_buses(key)
        ends = xy.loc[[a, b]].to_numpy()
        text = (
            f"{_display(names[a])}–{_display(names[b])}, {limits[key]:,.0f} h\n"
            + _binding_states(pairs, key, names)
        )
        labels.append([ends[:, 0].mean() < x_mid, (a, b), ends, text])
    # Each label sits at its branch's height, below the label above it, and its leader is
    # level where the branch spans that height. A leader that would pass within a line of an
    # outage cross, a zone name or another bus moves the label up or down, a line at a time.
    marks = np.vstack([crosses, [(lon * scale, lat) for lon, lat in ZONE_LABELS.values()]])
    for side in (True, False):
        group = sorted(
            (lab for lab in labels if lab[0] == side), key=lambda lab: -lab[2][:, 1].mean()
        )
        y_prev, lines_prev = np.inf, 0
        lx = left_x if side else right_x
        for _, buses, ends, text in group:
            lines = text.count("\n") + 1
            top = y_prev - (lines_prev + lines) / 2 * line
            obstacles = np.vstack([marks, xy.drop(list(buses)).to_numpy()])
            base = min(ends[:, 1].mean(), top)
            rise = ends[1, 1] - ends[0, 1]
            for lift in (0, 1, -1, 2, -2, 3, -3):
                ly = base + lift * line
                t = np.clip((ly - ends[0, 1]) / rise, 0.15, 0.85) if abs(rise) > 1e-9 else 0.5
                anchor = (1 - t) * ends[0] + t * ends[1]
                clear = _distance_to_segment(obstacles, np.array([lx, ly]), anchor).min()
                if ly <= top and clear >= line:
                    break
            else:
                anchor = ends.mean(axis=0)
                ly = base
            y_prev, lines_prev = ly, lines
            ax.annotate(
                text,
                xy=tuple(anchor),
                xytext=(lx, ly),
                fontsize=7.2,
                color=INK["primary"],
                ha="right" if side else "left",
                va="center",
                arrowprops={"arrowstyle": "-", "color": INK["muted"], "lw": 0.7},
                zorder=5,
            )

    bar = ax.inset_axes([0.03, 0.07, 0.3, 0.014])
    gradient = np.linspace(0, 1, 256)[None, :]
    bar.imshow(gradient, aspect="auto", cmap=ramp, extent=(0, peak, 0, 1))
    bar.set_yticks([])
    for spine in bar.spines.values():
        spine.set_visible(False)
    bar.tick_params(colors=INK["muted"], labelsize=7, length=0)
    bar.set_xlabel(
        "hours of 2015 in which the branch limits the N-1 redispatch",
        fontsize=7.5,
        color=INK["secondary"],
    )
    handles = [
        Line2D(
            [],
            [],
            marker="x",
            ls="",
            markersize=6,
            markeredgewidth=1.6,
            color=INK["primary"],
            label="outage behind the limits (four most frequent)",
        ),
        Line2D([], [], color="#cfcec8", lw=1.0, label="branch that never limits it"),
    ]
    ax.legend(
        handles=handles,
        loc="lower right",
        frameon=False,
        fontsize=7.5,
        labelcolor=INK["secondary"],
        borderaxespad=0.2,
    )
    ax.set_title(
        "Where the network limits the 2015 market dispatch under N-1",
        loc="left",
        fontsize=12,
        color=INK["primary"],
        pad=14,
    )
    ax.text(
        0,
        1.005,
        "Branches of the Nordic 44 equivalent that bind in the N-1 redispatch of every hour of "
        "2015",
        transform=ax.transAxes,
        fontsize=8,
        color=INK["secondary"],
        va="bottom",
    )
    fig.text(
        0.01,
        0.01,
        "Bus positions from the N44 geo-coordinates of ALSETLab/Nordic44-Nordpool",
        fontsize=6.5,
        color=INK["muted"],
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out, bbox_inches="tight", facecolor=INK["surface"])
    plt.close(fig)
    return out
