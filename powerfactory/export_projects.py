"""Build powerfactory/Nordic44_2015.pfd, the PowerFactory projects of the comparison.

The file holds one project per PSS/E case, each imported with PowerFactory's own PSS/E
importer as in the batch scripts:

- N44_BC, the base case of the dataset (tests/data/N44_BC.raw);
- N44_hNNNN, every hour with PowerFactory results in powerfactory/exports except the 50
  random load-flow hours, that is the ten representative hours and the other two hours of
  the N-1 comparison.

Every project has the exact transformer ratios of its RAW file (validation log, entry 6),
the generation costs of config/costs.yaml, a single-line diagram drawn by PowerFactory's
diagram layout tool, and a study case that holds the three studies of the comparison:

- the load flow, with the options of the pandapower runs (``pf_n44.LDF_OPTIONS``);
- the contingency analysis, one outage per line and transformer except the radial HVDC
  connections;
- the DC optimal power flow with nodal prices (``pf_n44.OPF_OPTIONS``).

The projects also stay in the study folder of the PowerFactory database. With --check, the
.pfd file is imported again into a temporary folder, and every study with PowerFactory
results in powerfactory/exports is run from the imported projects, with the settings they
carry, and compared with those results.

Usage: python powerfactory/export_projects.py [--check]
"""

from __future__ import annotations

import argparse
from datetime import date, timedelta

import pandas as pd
import pf_n44
import yaml

from n44.config import configured_path, load_config, repo_path
from n44.io.dataset import Dataset
from n44.io.psse_parser import parse_raw

PFD = repo_path("powerfactory", "Nordic44_2015.pfd")
CITATION = (
    "Data from L. Vanfretti et al., Nordic44 - 2015 Powerflow Data, Zenodo, "
    "doi:10.5281/zenodo.162907, licensed CC BY-NC 4.0."
)
# Largest differences accepted by --check against the exports
TOLERANCE = {"vm_pu": 1e-6, "mw": 1e-3, "lmp": 1e-3, "cost_eur": 1.0, "dispatch_mw": 0.05}


def exports() -> dict[str, pd.DataFrame]:
    """The PowerFactory results of the comparison, with circuit and machine ids as text."""
    out = repo_path(load_config()["powerfactory"]["exports_dir"])
    files = {
        "hours": "hours.csv",
        "ldf_buses": "ldf_buses.csv",
        "ldf_branches": "ldf_branches.csv",
        "n1_cases": "n1_cases.csv",
        "n1_branches": "n1_branches.csv.gz",
        "n1_buses": "n1_buses.csv.gz",
        "opf_buses": "opf_buses.csv",
        "opf_machines": "opf_machines.csv",
    }
    tables = {name: pd.read_csv(out / file) for name, file in files.items()}
    for table in tables.values():
        for column in ("ckt", "outage_ckt", "id"):
            if column in table:
                table[column] = table[column].astype(str)
    return tables


def hours(tables: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """Hours of the projects with the date, the hour of the day and why each was chosen."""
    listed = tables["hours"]
    chosen = listed[listed.criterion != "random"][["h", "criterion"]]
    others = sorted(set(tables["n1_cases"].h) - set(chosen.h))
    chosen = pd.concat(
        [
            chosen,
            pd.DataFrame(
                {"h": others, "criterion": "loss of the Sima transformer without an AC solution"}
            ),
        ],
        ignore_index=True,
    )
    chosen["date"] = [str(date(2015, 1, 1) + timedelta(days=int(h) // 24)) for h in chosen.h]
    chosen["hour"] = chosen.h % 24
    chosen["n1"] = chosen.h.isin(tables["n1_cases"].h)
    chosen["opf"] = chosen.h.isin(tables["opf_buses"].h)
    return chosen.sort_values("h", ignore_index=True)


def draw_diagram(app) -> None:
    """One single-line diagram of the ten zone grids, drawn by the diagram layout tool."""
    study = app.GetActiveStudyCase()
    grids = [g for g in app.GetCalcRelevantObjects("*.ElmNet") if g.loc_name != "Summary Grid"]
    selection = study.CreateObject("SetSelect", "Diagram grids")
    selection.AddRef(grids)
    layout = app.GetFromStudyCase("ComSgllayout")
    layout.SetAttribute("iAction", 0)  # generate a new diagram...
    layout.SetAttribute("iGenType", 0)  # ...of the entire grids in pGrids, in one diagram
    layout.SetAttribute("pGrids", selection)
    ok = layout.Execute() == 0
    selection.Delete()
    diagrams = app.GetActiveProject().GetContents("*.IntGrfnet", 1)
    if not ok or len(diagrams) != 1:
        raise RuntimeError(f"diagram layout failed: {pf_n44.output(app)[-3:]}")
    diagrams[0].loc_name = "Nordic 44"


def build(app, name: str, raw_path, text: str, costs: dict, description: list[str]):
    """Import one case and prepare its study case. Leaves the project deactivated."""
    project = pf_n44.import_raw(app, raw_path, name)
    pf_n44.set_exact_ratios(app, parse_raw(text))
    if not pf_n44.run_ldf(app):
        raise RuntimeError(f"{name}: the load flow does not converge")
    command = app.GetFromStudyCase("ComSimoutage")
    for old in command.GetContents("*.ComOutage"):
        old.Delete()
    outages = pf_n44.add_contingencies(app, command)
    pf_n44.set_costs(app, costs)
    pf_n44.set_opf_options(app)
    draw_diagram(app)
    project.SetAttribute("desc", description)
    project.Deactivate()
    print(f"{name}: {len(outages)} contingencies", flush=True)
    return project


def export(app, projects: list) -> None:
    folder = pf_n44.project_folder(app)
    exporter = folder.GetContents("PFD export.ComPfdexport")
    exporter = exporter[0] if exporter else folder.CreateObject("ComPfdexport", "PFD export")
    exporter.SetAttribute("g_objects", projects)
    exporter.SetAttribute("g_file", str(PFD))
    if exporter.Execute() != 0 or not PFD.exists():
        raise RuntimeError(f"export failed: {pf_n44.output(app)[-3:]}")
    print(f"{PFD} ({PFD.stat().st_size / 1e6:.2f} MB, {len(projects)} projects)")


def worst(model: pd.DataFrame, reference: pd.DataFrame, keys: list[str], columns: dict) -> dict:
    """Largest absolute difference per column group, after matching rows on ``keys``."""
    merged = model.merge(reference, on=keys, suffixes=("", "_ref"), validate="1:1")
    if len(merged) != len(reference):
        raise RuntimeError(f"{len(merged)} of {len(reference)} rows matched on {keys}")
    return {
        group: float(max((merged[c] - merged[f"{c}_ref"]).abs().max() for c in cols))
        for group, cols in columns.items()
    }


def check(app, tables: dict[str, pd.DataFrame], costs: dict) -> None:
    """Import the .pfd file into a temporary folder and repeat the studies of the comparison."""
    folder = pf_n44.project_folder(app)
    for old in folder.GetContents("check.IntFolder"):
        _delete(app, old)
    target = folder.CreateObject("IntFolder", "check")
    importer = folder.GetContents("PFD import.ComPfdimport")
    importer = importer[0] if importer else folder.CreateObject("ComPfdimport", "PFD import")
    importer.SetAttribute("g_file", str(PFD))
    importer.SetAttribute("g_target", target)
    if importer.Execute() != 0:
        raise RuntimeError(f"import of {PFD} failed: {pf_n44.output(app)[-3:]}")
    projects = target.GetContents("*.IntPrj")
    rows = []
    for project in sorted(projects, key=lambda p: p.loc_name):
        project.Activate()
        row = {"project": project.loc_name}
        ldf = app.GetFromStudyCase("ComLdf")
        row["ldf"] = ldf.Execute() == 0 and bool(app.IsLdfValid())
        h = int(project.loc_name[5:]) if project.loc_name.startswith("N44_h") else None
        exact = tables["hours"].h.eq(h).any()
        if row["ldf"] and exact:
            found = pf_n44.export_tables(app)
            ref = {
                name: tables[f"ldf_{name}"].query("h == @h and variant == 'exact'")
                for name in ("buses", "branches")
            }
            row |= worst(found["buses"], ref["buses"], ["bus"], {"ldf_vm": ["vm_pu"]})
            row |= worst(
                found["branches"],
                ref["branches"],
                ["kind", "bus1", "bus2", "ckt"],
                {"ldf_flow": ["p1_mw", "q1_mvar", "p2_mw", "q2_mvar"]},
            )
        if h in set(tables["n1_cases"].h):
            command = app.GetFromStudyCase("ComSimoutage")
            elements = {e.loc_name: e for e in app.GetCalcRelevantObjects("*.ElmLne,*.ElmTr2")}
            outages = {
                int(o.GetAttribute("number")): pf_n44.branch_key(elements[o.loc_name])
                for o in command.GetContents("*.ComOutage")
            }
            found = pf_n44.contingency_results(app, command, outages)
            keys = ["outage_kind", "outage_bus1", "outage_bus2", "outage_ckt"]
            cases = found["cases"].merge(
                tables["n1_cases"].query("h == @h"), on=keys, suffixes=("", "_ref")
            )
            row["n1_cases"] = len(cases)
            row["n1_same_convergence"] = bool((cases.converged == cases.converged_ref).all())
            converged = cases[cases.converged][keys]
            branches = found["branches"].merge(converged, on=keys)
            reference = tables["n1_branches"].query("h == @h").merge(converged, on=keys)
            row |= worst(
                branches,
                reference,
                [*keys, "kind", "bus1", "bus2", "ckt"],
                {"n1_flow": ["p1_mw", "q1_mvar", "p2_mw", "q2_mvar"]},
            )
            buses = found["buses"].merge(converged, on=keys)
            reference = tables["n1_buses"].query("h == @h").merge(converged, on=keys)
            row |= worst(buses, reference, [*keys, "bus"], {"n1_vm": ["vm_pu"]})
        if h in set(tables["opf_buses"].h):
            row["opf"] = app.GetFromStudyCase("ComOpf").Execute() == 0
            prices = pd.DataFrame(
                [
                    (int(t.loc_name.split()[0]), pf_n44._get(t, "m:LMP"))
                    for t in app.GetCalcRelevantObjects("*.ElmTerm")
                ],
                columns=["bus", "lmp"],
            )
            dispatch = pd.DataFrame(
                [
                    (pf_n44._bus(s.bus1), s.loc_name.split("_", 2)[2], pf_n44._get(s, "m:P:bus1"))
                    for s in app.GetCalcRelevantObjects("*.ElmSym")
                ],
                columns=["bus", "id", "p_mw"],
            )
            reference = tables["opf_buses"].query("h == @h")
            row |= worst(prices, reference, ["bus"], {"opf_lmp": ["lmp"]})
            # Machines of equal cost can share their output in more than one optimal way, so
            # the dispatch is compared per technology, with the total cost, as in script 17
            output, cost = opf_totals(dispatch, costs)
            output_ref, cost_ref = opf_totals(tables["opf_machines"].query("h == @h"), costs)
            row["opf_cost"] = abs(cost - cost_ref)
            row["opf_technology"] = float((output - output_ref).abs().max())
        project.Deactivate()
        rows.append(row)
    for project in projects:
        pf_n44.discard(app, project, folder=target)
    _delete(app, target)
    summary = pd.DataFrame(rows)
    with pd.option_context("display.width", 200, "display.max_columns", 20):
        print(summary.to_string(index=False, float_format=lambda v: f"{v:.2g}"))
    limits = {
        "ldf_vm": TOLERANCE["vm_pu"],
        "ldf_flow": TOLERANCE["mw"],
        "n1_flow": TOLERANCE["mw"],
        "n1_vm": TOLERANCE["vm_pu"],
        "opf_lmp": TOLERANCE["lmp"],
        "opf_cost": TOLERANCE["cost_eur"],
        "opf_technology": TOLERANCE["dispatch_mw"],
    }
    failed = [c for c, limit in limits.items() if c in summary and (summary[c] > limit).any()]
    failed += [
        c
        for c in ("ldf", "opf", "n1_same_convergence")
        if c in summary and not summary[c].dropna().all()
    ]
    print("check passed" if not failed else f"check FAILED on {failed}")


def opf_totals(dispatch: pd.DataFrame, costs: dict) -> tuple[pd.Series, float]:
    """Output per technology and hourly cost of a dispatch (columns bus, id, p_mw)."""
    technology = []
    for bus, machine in zip(dispatch.bus, dispatch.id, strict=True):
        plant = costs["plants"][int(bus)]
        technology.append(plant.get("machines", {}).get(str(machine), plant["technology"]))
    output = dispatch.p_mw.groupby(technology).sum()
    cost = sum(p * costs["technologies"][t] for p, t in zip(dispatch.p_mw, technology, strict=True))
    return output, float(cost)


def _delete(app, obj) -> None:
    """Delete an object of the study folder and its entry in the recycle bin."""
    name, cls = obj.loc_name, obj.GetClassName()
    obj.Delete()
    recycle = app.GetCurrentUser().GetContents("RecBin.IntFolder")
    for entry in recycle[0].GetContents() if recycle else []:
        content = entry.GetContents()
        if len(content) == 1 and content[0].GetClassName() == cls and content[0].loc_name == name:
            entry.Delete()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="re-import the file and compare")
    args = parser.parse_args()
    with open(repo_path("config", "costs.yaml"), encoding="utf-8") as fh:
        costs = yaml.safe_load(fh)
    tables = exports()
    raw_dir = configured_path("processed_dir") / "powerfactory" / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    ds = Dataset()
    app = pf_n44.start()

    base = repo_path("tests", "data", "N44_BC.raw")
    projects = [
        build(
            app,
            "N44_BC",
            base,
            base.read_text(encoding="latin-1"),
            costs,
            [
                "Nordic 44 base case of the dataset (N44_BC.raw). Its generation was rescaled "
                "after the last solution, so it is not a solved power flow (see "
                "docs/validation_log.md, entry 1).",
                "Imported with PowerFactory's PSS/E importer, with the transformer ratios set "
                "to the RAW file's WINDV1.",
                CITATION,
            ],
        )
    ]
    for row in hours(tables).itertuples():
        text = ds.raw_text(row.h, "after")
        raw_path = raw_dir / f"h{row.h:04d}.raw"
        raw_path.write_text(text, encoding="latin-1", newline="")
        studies = ["load flow"] + ["N-1"] * row.n1 + ["DC-OPF"] * row.opf
        description = [
            f"Nordic 44 snapshot of {row.date}, hour {row.hour} (h = {row.h}), {row.criterion}.",
            "Imported with PowerFactory's PSS/E importer from the dataset's after_PF RAW file, "
            "with the transformer ratios set to the RAW file's WINDV1.",
            f"Compared with this project's results in PowerFactory: {', '.join(studies)}.",
            CITATION,
        ]
        projects.append(build(app, f"N44_h{row.h:04d}", raw_path, text, costs, description))
    export(app, projects)
    if args.check:
        check(app, tables, costs)


if __name__ == "__main__":
    main()
