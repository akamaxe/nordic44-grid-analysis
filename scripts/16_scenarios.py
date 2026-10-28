"""Future scenarios through the same chain as the 2015 analysis (question P5).

Scenarios (config/scenarios/*.yaml):
- **base:** the 2015 network;
- **ac_line:** a third AC circuit on snitt 2's most loaded corridor, Hjalta-Ringhals, where
  the intact network binds;
- **hvdc:** a 1,200 MW VSC link on the same corridor;
- **ac_line_north, hvdc_north:** the same two options at the N-1 bottleneck north of
  Hjalta: a second Hjalta-Grundfors circuit, and the link from Grundfors to Ringhals;
- **north_load:** new industrial load in SE1/SE2, from 1 to 10 GW;
- **north_wind:** 5 GW of new load with 4 or 8 GW of new northern wind (fixed output).

For every seventh hour of 2015 (1,252 hours), each scenario is solved in AC (``n44.acpf``)
and then redispatched for N-0 and N-1 (``n44.redispatch``, with the HVDC set-point as a
control). Seven is prime to the 24 hours of a day and the 168 of a week, so the sample covers
every hour of the day and day of the week: on the 2015 network it reproduces the annual
redispatch of scripts/15_redispatch.py to 0.4 % (every sixth hour would sample only 00, 06,
12 and 18 h and miss the N-0 volume by 9 %).
The recorded metrics:
- intact-network overloads;
- losses;
- flows across the Swedish cuts;
- redispatch volumes, scaled to the year;
- the N-1 transfer capability of each cut, from the hour's secure (redispatched) state, as
  in scripts/12_transfer_margins.py. An HVDC link keeps its set-point while the transfer
  grows, its flow counts in the cut it crosses, and its trip is one more contingency.

Every 13th evaluated hour (97 hours) is then checked with a full AC N-1 after the
redispatch.

The work is split into chunks of hours per variant. Each finished chunk is saved at once in
results/scenarios/parts/, and a new run skips the chunks already there, so an interrupted run
resumes where it stopped (--force starts again). Each worker process commits about 1.2 GB:
check the free memory before choosing --workers.

Writes results/scenarios/*.parquet, results/scenario_comparison.csv and the figures
scenarios_reinforcement.png and scenarios_north.png.

Usage: python scripts/16_scenarios.py [--workers N] [--step 7] [--force]
"""

import argparse
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

import numpy as np
import pandas as pd

from n44.acpf import branch_flows, grid_from_case, solve
from n44.config import configured_path
from n44.contingency import (
    PTDF_THRESHOLD,
    ac_n1_sweep,
    branch_table,
    cut_crossing,
    dc_model,
    radial_branches,
    shunt_outage_shift,
    transfer_margins,
    transfer_sensitivity,
    transfer_shift,
)
from n44.io.build_net import build_net
from n44.io.dataset import Dataset
from n44.io.psse_parser import parse_raw
from n44.opf import machine_table
from n44.plotting import plot_northern_load, plot_reinforcements
from n44.redispatch import effective_limits, redispatch
from n44.scenarios import links, load_scenario, modify_case
from n44.snapshots import snapshot_dir
from n44.zones import CUTS, cut_flows

AC_CHECK_STRIDE = 13  # every 13th evaluated hour gets a full AC N-1 after the redispatch
CHUNKS = 6  # chunks of hours per variant, each saved as soon as it is done
VOLTAGE_MIN = 0.90
MATERIAL_MW = 50.0  # the market flow counts as above the capability beyond this


def variants() -> list[tuple[str, float | None, float | None]]:
    """(scenario, new load MW, new wind MW) of every run."""
    out = [
        (name, None, None) for name in ("base", "ac_line", "hvdc", "ac_line_north", "hvdc_north")
    ]
    out += [
        ("north_load", float(mw), None)
        for mw in load_scenario("north_load")["add_load"]["sweep_mw"]
    ]
    wind = load_scenario("north_wind")
    out += [
        ("north_wind", float(wind["add_load"]["sweep_mw"][0]), float(w))
        for w in wind["add_wind"]["sweep_mw"]
    ]
    return out


def label(name: str, load_mw: float | None, wind_mw: float | None = None) -> str:
    if load_mw is None:
        return name
    tag = f"{name}_{load_mw / 1000:.0f}gw"
    return tag if wind_mw is None else f"{tag}_wind{wind_mw / 1000:.0f}gw"


def _context(name: str, load_mw: float | None, wind_mw: float | None):
    scenario = load_scenario(name)
    ds = Dataset()
    reference = modify_case(parse_raw(ds.raw_text(0, "after")), scenario, load_mw, wind_mw=wind_mw)
    net = build_net(reference)
    table = branch_table(net)
    names = list(table.index)
    outages = [n for n in names if n not in radial_branches(table)]
    dc = dc_model(net)
    units = machine_table(net)
    return scenario, ds, net, table, names, outages, dc, units, shunt_outage_shift(net, dc, outages)


def mean_generation() -> pd.Series:
    """Mean 2015 output per bus (MW), the weights of the generation shift keys."""
    gen_p = pd.read_parquet(snapshot_dir() / "gen_p.parquet")
    return gen_p.mean().groupby(lambda c: int(c.split("_")[0])).sum()


def _cut_model(net, dc, table, outages, units, hvdc, generation: pd.Series) -> dict:
    """Shift keys, crossing branches and sensitivities of each cut for its N-1 capability."""
    names = list(table.index)
    zone = net.bus.zone
    ptdf = dc.ptdf.loc[names]
    model = {
        "units": ptdf[units.bus].to_numpy() * units.in_service.to_numpy()[None, :],
        "links": np.column_stack([ptdf[b].to_numpy() - ptdf[a].to_numpy() for a, b, _ in hvdc])
        if hvdc
        else np.zeros((len(names), 0)),
        "cuts": {},
    }
    for cut, (north, south, (sending, receiving)) in CUTS.items():
        shift = transfer_shift(generation, zone, [sending], [receiving])
        crossing = cut_crossing(table, zone, north, south)
        sens = transfer_sensitivity(dc, shift, table, outages)[:, 0]
        sides = [(zone[a] in north) - (zone[a] in south) for a, _, _ in hvdc]
        ends = [(zone[b] in south) - (zone[b] in north) for _, b, _ in hvdc]
        model["cuts"][cut] = {
            "shift": shift,
            "crossing": crossing,
            "share": float(crossing @ sens),
            "sens": sens,
            # +1 for a link from the north side to the south side, -1 the other way
            "link_crossing": np.array(
                [(s + e) / 2 if s == e else 0.0 for s, e in zip(sides, ends, strict=True)]
            ),
        }
    return model


def _capabilities(pre, delta, set_points, model, dc, table, outages, shift) -> dict:
    """Market flow, secure flow and N-1 transfer capability of each cut in one hour."""
    change = model["units"] @ delta + model["links"] @ set_points
    secure = {key: value[None, :].copy() for key, value in pre.items()}
    secure["p_i"] += change
    secure["p_j"] -= change
    before = (pre["p_i"] - pre["p_j"]) / 2
    after = before + change
    current = (table.basis == "current").to_numpy()
    limit_b = effective_limits(pre, table.rate_b.to_numpy(), current)
    out = {}
    for cut, c in model["cuts"].items():
        margin = transfer_margins(secure, dc, c["shift"], table, outages, outage_shift=shift)
        margin = float(margin.margin_n1_mw.iloc[0])
        critical = np.abs(c["sens"]) >= PTDF_THRESHOLD
        for j, p in enumerate(set_points):  # the trip puts the link's flow back on the AC network
            tripped = after - model["links"][:, j] * p
            room = np.where(c["sens"] > 0, limit_b - tripped, limit_b + tripped)
            margin = min(margin, float(np.min(room[critical] / np.abs(c["sens"][critical]))))
        secure_flow = float(after @ c["crossing"] + c["link_crossing"] @ set_points)
        out[f"market {cut}"] = float(before @ c["crossing"])
        out[f"secure {cut}"] = secure_flow
        out[f"capability {cut}"] = secure_flow + c["share"] * max(margin, 0.0)
    return out


def _pre(grid, v) -> tuple[dict[str, np.ndarray], np.ndarray, np.ndarray, np.ndarray]:
    si, sj, loading = branch_flows(grid, v)
    pre = {
        "p_i": si.real,
        "p_j": sj.real,
        "q_i": si.imag,
        "q_j": sj.imag,
        "v_i": np.abs(v[grid.f]),
        "v_j": np.abs(v[grid.t]),
    }
    return pre, si, sj, loading


def _dispatch(case, grid, v, units) -> np.ndarray:
    """Output of every unit: scheduled for the PV machines, solved for the swing bus."""
    gen = case.generator.set_index(["i", "id"]).pg
    slack_bus = grid.model.buses[grid.slack]
    s_calc = v * np.conj(grid.model.ybus @ v) * grid.model.sbase
    load = case.load[(case.load.status == 1) & (case.load.i == slack_bus)].pl.sum()
    out = []
    for row in units.itertuples():
        out.append(
            gen.loc[(row.bus, row.id)] if row.element == "gen" else s_calc[grid.slack].real + load
        )
    return np.asarray(out, dtype=float)


def solve_hours(job: tuple) -> Path:
    """Solve one chunk of hours of one variant and save its rows at ``job``'s path."""
    name, load_mw, wind_mw, hours, ac_hours, generation, path = job
    scenario, ds, net, table, names, outages, dc, units, shift = _context(name, load_mw, wind_mw)
    hvdc = links(scenario)
    zone = net.bus.zone
    cut_model = _cut_model(net, dc, table, outages, units, hvdc, generation)
    rows = []
    for h in hours:
        raw_case = parse_raw(ds.raw_text(int(h), "after"))
        case = modify_case(raw_case, scenario, load_mw, wind_mw=wind_mw)
        grid = grid_from_case(case)
        if grid.names != names:
            raise RuntimeError("branch order differs between the AC and DC models")
        base = solve(grid)
        row = {"h": int(h), "converged": base.converged}
        if not base.converged:
            rows.append(row)
            continue
        pre, si, sj, loading = _pre(grid, base.v)
        flows = grid.model.branches[["i", "j", "metered"]].assign(p_i_mw=si.real, p_j_mw=sj.real)
        cuts = cut_flows(flows, zone)
        row.update(
            {
                "n0_max_loading_pct": float(np.nanmax(loading)),
                "n0_overloaded": int((loading > 100).sum()),
                "losses_mw": float(np.nansum(si.real + sj.real)),
                **{f"flow {cut}": float(cuts[cut]) for cut in CUTS},
            }
        )
        dispatch = _dispatch(case, grid, base.v, units)
        for tag, n1 in (("n0", False), ("n1", True)):
            r = redispatch(pre, dc, table, units, dispatch, outages, shift, n1=n1, links=hvdc)
            zonal = pd.Series(r.up - r.down, index=units.zone).groupby(level=0).sum()
            row.update(
                {
                    f"{tag}_volume_mw": r.volume_mw,
                    f"{tag}_interzonal_mw": float(zonal.clip(lower=0).sum()),
                    f"{tag}_unresolved_mw": r.unresolved_mw,
                }
            )
            if hvdc:
                row[f"{tag}_hvdc_mw"] = float(np.abs(r.links).sum())
        row["n1_binding"] = "; ".join(r.binding)
        row.update(_capabilities(pre, r.up - r.down, r.links, cut_model, dc, table, outages, shift))
        if h in ac_hours:
            row.update(ac_check(raw_case, scenario, load_mw, wind_mw, r, units, outages, table))
        rows.append(row)
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_suffix(".tmp")
    pd.DataFrame(rows).to_parquet(partial)
    partial.replace(path)  # a chunk on disk is always complete
    return path


def ac_check(raw_case, scenario, load_mw, wind_mw, result, units, outages, table) -> dict:
    """Full AC N-1 after the N-1 redispatch (the HVDC trip itself is not re-checked)."""
    set_points = result.links if len(result.links) else None
    after = modify_case(raw_case, scenario, load_mw, set_points=set_points, wind_mw=wind_mw)
    gen = after.generator.set_index(["i", "id"])
    for row, d in zip(units.itertuples(), result.up - result.down, strict=True):
        if row.element == "gen":
            gen.loc[(row.bus, row.id), "pg"] += d
    after.generator = gen.reset_index()
    grid = grid_from_case(after)
    positions = [grid.names.index(o) for o in outages]
    base = solve(grid)
    losses = np.nan
    if base.converged:
        si, sj, _ = branch_flows(grid, base.v)
        converters = sum(
            abs(p) * spec["losses"]
            for spec, p in zip(scenario.get("hvdc", []), result.links, strict=True)
        )
        losses = float(np.nansum(si.real + sj.real) + converters)
    res = ac_n1_sweep(grid, positions)
    to_b = (table.rate_a / table.rate_b).to_numpy()
    base_b = res["base_loading"] * to_b
    post_b = res["loading"] * to_b
    induced = (post_b > 100) & (base_b <= 100)[None, :]
    insecure = induced.any(axis=1) | ~res["converged"] | (res["vm_min"] < VOLTAGE_MIN)
    return {
        "ac_checked": True,
        "ac_n0_max_loading_pct": float(np.nanmax(res["base_loading"])),
        "ac_insecure_outages": int(insecure.sum()),
        "ac_no_solution": int((~res["converged"]).sum()),
        "ac_losses_after_mw": losses,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workers", type=int, default=2, help="about 1.2 GB of memory each")
    parser.add_argument("--step", type=int, default=7, help="evaluate every n-th hour")
    parser.add_argument("--force", action="store_true", help="discard saved chunks")
    args = parser.parse_args()
    hours = np.arange(0, 8760, args.step)
    ac_hours = {int(h) for h in hours[::AC_CHECK_STRIDE]}
    folder = configured_path("results_dir") / "scenarios"
    parts = folder / "parts" / f"step{args.step}"
    if args.force:
        for old in parts.glob("*.parquet"):
            old.unlink()
    generation = mean_generation()
    chunks = np.array_split(hours, CHUNKS)
    tags = [label(*variant) for variant in variants()]
    jobs = [
        (*variant, chunk, ac_hours, generation, parts / f"{tag}_{k}.parquet")
        for variant, tag in zip(variants(), tags, strict=True)
        for k, chunk in enumerate(chunks)
        if not (parts / f"{tag}_{k}.parquet").exists()
    ]
    print(f"{len(jobs)} of {len(tags) * CHUNKS} chunks to solve", flush=True)
    if jobs:
        with ProcessPoolExecutor(max_workers=args.workers) as pool:
            futures = [pool.submit(solve_hours, job) for job in jobs]
            for done, future in enumerate(as_completed(futures), 1):
                print(f"  [{done}/{len(jobs)}] {future.result().stem}", flush=True)
    summary = []
    scale = 8760 / len(hours)
    for tag in tags:
        frame = pd.concat([pd.read_parquet(parts / f"{tag}_{k}.parquet") for k in range(CHUNKS)])
        frame = frame.set_index("h").sort_index()
        frame.to_parquet(folder / f"{tag}.parquet")
        solved = frame[frame.converged]
        flag = frame.get("ac_checked", pd.Series(False, index=frame.index))
        checked = frame[flag.eq(True)]  # unchecked hours hold NaN
        entry = {
            "scenario": tag,
            "hours": len(frame),
            "unsolved_hours": int((~frame.converged).sum()),
            "n0_overload_share": float((solved.n0_overloaded > 0).mean()),
            "losses_twh": solved.losses_mw.mean() * 8760 / 1e6,
            "n0_redispatch_twh": solved.n0_volume_mw.sum() * scale / 1e6,
            "n1_redispatch_twh": solved.n1_volume_mw.sum() * scale / 1e6,
            "n1_interzonal_twh": solved.n1_interzonal_mw.sum() * scale / 1e6,
            "n1_unresolved_hours_share": float((solved.n1_unresolved_mw > 1).mean()),
            "ac_check_hours": len(checked),
            "ac_mean_insecure_outages": float(checked.ac_insecure_outages.mean())
            if len(checked)
            else np.nan,
            "ac_losses_after_mw": float(checked.ac_losses_after_mw.mean())
            if len(checked)
            else np.nan,
            **{f"mean flow {cut}": solved[f"flow {cut}"].mean() for cut in CUTS},
            **{f"capability median {cut}": solved[f"capability {cut}"].median() for cut in CUTS},
            **{
                f"hours above capability {cut}": float(
                    (solved[f"market {cut}"] > solved[f"capability {cut}"] + MATERIAL_MW).mean()
                    * 8760
                )
                for cut in CUTS
            },
        }
        if "n1_hvdc_mw" in solved:
            entry["hvdc_mean_mw"] = solved.n1_hvdc_mw.mean()
        summary.append(entry)
    summary = pd.DataFrame(summary)
    out = configured_path("results_dir")
    summary.to_csv(out / "scenario_comparison.csv", index=False, float_format="%.4g")
    print(summary.T.to_string())
    frames = {tag: pd.read_parquet(folder / f"{tag}.parquet") for tag in tags}
    plot_reinforcements(summary, frames, args.step, out / "figures" / "scenarios_reinforcement.png")
    plot_northern_load(summary, args.step, out / "figures" / "scenarios_north.png")


if __name__ == "__main__":
    main()
