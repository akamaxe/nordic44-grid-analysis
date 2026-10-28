"""Drive DIgSILENT PowerFactory from Python for the Nordic 44 validation.

Needs PowerFactory 2024 with its Python API for Python 3.12 (paths in the ``powerfactory``
section of config/base.yaml, or the N44_PF_INSTALL_DIR / N44_PF_PYTHON_API variables).
PowerFactory itself must be closed, because the scripts open its local database.

Everything here happens inside one folder of the PowerFactory user's root (``folder`` in the
configuration). RAW files are imported with PowerFactory's own PSS/E importer (ComPsse), so
the PowerFactory model is built independently of this project's parser. Temporary projects
are deleted again, together with the copy that PowerFactory keeps in the recycle bin.

Exported tables use PowerFactory's own orientation (``bus1``/``bus2``, HV/LV for
transformers) and PSS/E bus numbers; ``n44.validation.solution_from_powerfactory`` orients
them like PSS/E.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pandas as pd

from n44.config import load_config
from n44.io.psse_parser import RawCase

# Load-flow options matching the pandapower runs.
LDF_OPTIONS = {
    "iopt_net": 0,  # AC load flow, balanced, positive sequence
    "iopt_lim": 1,  # consider reactive power limits
    "iopt_at": 0,  # no automatic tap adjustment: taps as in the RAW file
    "iPST_at": 0,  # no automatic phase-shifter adjustment
    "iopt_asht": 0,  # no automatic shunt switching
    "iopt_pq": 0,  # no voltage dependence of loads (constant power)
    "iopt_apdist": 0,  # active power "as dispatched"...
    "iPbalancing": 0,  # ...with the reference machine taking the balance (PSS/E swing bus)
    "iopt_plim": 0,  # no active power limit on the reference machine, as PSS/E
    "iInterChg": 0,  # no interchange schedules (the snapshots are already solved for them)
    "iopt_fl": 1,  # flat start
    "errlf": 0.001,  # max. bus-equation error in kVA (default 1 kVA)
    "erreq": 0.001,  # max. model-equation error in % (default 0.1 %)
    "itrlx": 50,  # Newton-Raphson iterations
}

# Radial connections of the HVDC terminal buses. Losing one isolates a bus and its link, so
# they are left out of the contingency list.
RADIAL = {
    "lne_3000_3020_1",
    "lne_5600_5620_1",
    "lne_5603_5610_1",
    "lne_7000_7010_1",
    "lne_7000_7020_1",
    "lne_8500_8600_1",
    "lne_8500_8700_1",
    "trf_3359_3360_1",
}
# Open every recording limit and filter of the contingency analysis, so that all branches
# and buses are stored for every contingency
RECORD_ALL = {
    "iopt_Linear": 0,  # AC
    "loadmax": 0.0,  # record loading above 0 %
    "vlmin": 2.0,  # record voltages below 2 p.u....
    "vlmax": 0.0,  # ...and above 0 p.u.
    "iopt_record": 0,
    "maxLoadBase": 10000.0,
    "diffLoadBase": 0.0,
    "minVoltBase": 0.0,
    "maxVoltBase": 10.0,
    "diffVoltBase": 0.0,
}

# DC optimal power flow with the costs of config/costs.yaml
OPF_OPTIONS = {
    "iopt_ACDC": 1,  # DC optimisation (linear programming)
    "iopt_objDC": 1,  # minimise costs
    "iCalcLMP": 1,  # locational marginal prices
    "iopt_pdDC": 1,  # control: generator active power
    "iopt_brnch": 1,  # constraint: branch flow limits
    "iopt_genP": 1,  # constraint: generator active power limits
    "iopt_trfDC": 0,  # taps fixed
    "numOfIt": 30,  # outer loops that add branch constraints
}


def settings() -> dict:
    cfg = dict(load_config()["powerfactory"])
    for key, variable in (
        ("install_dir", "N44_PF_INSTALL_DIR"),
        ("python_api", "N44_PF_PYTHON_API"),
    ):
        cfg[key] = os.environ.get(variable, cfg[key])
    return cfg


def start():
    """Start PowerFactory without its user interface and return the application object."""
    cfg = settings()
    if cfg["python_api"] not in sys.path:
        sys.path.insert(0, cfg["python_api"])
    os.add_dll_directory(cfg["install_dir"])
    import powerfactory

    try:
        app = powerfactory.GetApplicationExt()
    except powerfactory.ExitError as exc:
        hint = " (close PowerFactory first)" if exc.code == 4002 else ""
        raise RuntimeError(f"PowerFactory did not start: ExitError {exc.code}{hint}") from exc
    app.ClearOutputWindow()
    return app


def project_folder(app):
    """The folder of the user's root that holds the projects of this study."""
    user = app.GetCurrentUser()
    name = settings()["folder"]
    found = user.GetContents(f"{name}.IntFolder")
    return found[0] if found else user.CreateObject("IntFolder", name)


def import_raw(app, raw_path: Path, name: str):
    """Import a PSS/E RAW file as a new project with PowerFactory's importer and activate it.

    A project of the same name in the study folder is replaced.
    """
    active = app.GetActiveProject()
    if active is not None:
        active.Deactivate()
    folder = project_folder(app)
    for old in folder.GetContents(f"{name}.IntPrj"):
        discard(app, old)
    importer = folder.GetContents("PSSE import.ComPsse")
    importer = importer[0] if importer else folder.CreateObject("ComPsse", "PSSE import")
    importer.rawname = str(raw_path)
    importer.targname = name
    importer.targpath = folder
    if importer.Execute() != 0:
        raise RuntimeError(f"PSS/E import of {raw_path} failed: {output(app)}")
    project = folder.GetContents(f"{name}.IntPrj")
    if not project:
        raise RuntimeError(f"imported project {name} not found")
    if app.GetActiveProject() != project[0]:  # the importer normally leaves it active
        project[0].Activate()
    if app.GetActiveProject() != project[0]:
        raise RuntimeError(f"imported project {name} could not be activated")
    return project[0]


def discard(app, project, folder=None) -> None:
    """Delete a project of the study folder (or of ``folder``) and its copy in the recycle bin."""
    if project.GetParent() != (folder or project_folder(app)):
        raise ValueError(f"{project.GetFullName()} is not in the study folder")
    active = app.GetActiveProject()
    if active is not None and active == project:
        project.Deactivate()
    name = project.loc_name
    if project.Delete() != 0:
        raise RuntimeError(f"could not delete project {name}")
    recycle = app.GetCurrentUser().GetContents("RecBin.IntFolder")
    for entry in recycle[0].GetContents() if recycle else []:
        content = entry.GetContents()
        if (
            len(content) == 1
            and content[0].GetClassName() == "IntPrj"
            and content[0].loc_name == name
        ):
            entry.Delete()  # inside the recycle bin, Delete() is permanent


def output(app) -> list[str]:
    """Lines of the output window since it was last cleared."""
    return [str(line) for line in app.GetOutputWindow().GetContent()]


def set_ldf_options(app):
    """The load flow of the active study case, with ``LDF_OPTIONS``."""
    ldf = app.GetFromStudyCase("ComLdf")
    for key, value in LDF_OPTIONS.items():
        ldf.SetAttribute(key, value)
    return ldf


def run_ldf(app) -> bool:
    """Run the AC load flow of the active study case with ``LDF_OPTIONS``."""
    return set_ldf_options(app).Execute() == 0 and bool(app.IsLdfValid())


def _bus(cubicle) -> int:
    """PSS/E number of the terminal a cubicle hangs from (the importer names it 'NUMBER NAME')."""
    return int(cubicle.cterm.loc_name.split()[0])


def _get(obj, attribute: str) -> float:
    try:
        return obj.GetAttribute(attribute)
    except AttributeError:
        return float("nan")


def branch_key(obj) -> tuple[str, int, int, str]:
    """(kind, bus1, bus2, ckt) of a line or transformer, as in ``export_tables``."""
    if obj.GetClassName() == "ElmLne":
        return ("line", _bus(obj.bus1), _bus(obj.bus2), obj.loc_name.split("_", 3)[3])
    return ("transformer", _bus(obj.bushv), _bus(obj.buslv), obj.loc_name.split("_", 3)[3])


def export_tables(app) -> dict[str, pd.DataFrame]:
    """Bus voltages, machine outputs and branch terminal flows of the last load flow."""
    buses = pd.DataFrame(
        [
            (int(t.loc_name.split()[0]), _get(t, "m:u"), _get(t, "m:phiu"))
            for t in app.GetCalcRelevantObjects("*.ElmTerm")
        ],
        columns=["bus", "vm_pu", "va_degree"],
    )
    machines = pd.DataFrame(
        [
            (
                _bus(s.bus1),
                s.loc_name.split("_", 2)[2],
                int(not s.outserv),
                _get(s, "m:P:bus1"),
                _get(s, "m:Q:bus1"),
            )
            for s in app.GetCalcRelevantObjects("*.ElmSym")
        ],
        columns=["bus", "id", "in_service", "p_mw", "q_mvar"],
    )
    rows = []
    for kind, pattern, ends in (
        ("line", "*.ElmLne", ("bus1", "bus2")),
        ("transformer", "*.ElmTr2", ("bushv", "buslv")),
    ):
        for o in app.GetCalcRelevantObjects(pattern):
            a, b = ends
            rows.append(
                (
                    kind,
                    _bus(o.GetAttribute(a)),
                    _bus(o.GetAttribute(b)),
                    o.loc_name.split("_", 3)[3],
                    int(not o.outserv),
                    _get(o, f"m:P:{a}"),
                    _get(o, f"m:Q:{a}"),
                    _get(o, f"m:P:{b}"),
                    _get(o, f"m:Q:{b}"),
                    _get(o, "c:loading"),
                )
            )
    branches = pd.DataFrame(
        rows,
        columns=[
            "kind",
            "bus1",
            "bus2",
            "ckt",
            "in_service",
            "p1_mw",
            "q1_mvar",
            "p2_mw",
            "q2_mvar",
            "loading_pct",
        ],
    )
    return {"buses": buses, "machines": machines, "branches": branches}


def set_exact_ratios(app, case: RawCase) -> pd.DataFrame:
    """Give every transformer the exact ratio WINDV1/WINDV2 of the RAW file.

    PowerFactory's importer turns WINDV into the nearest position of the tap grid defined by
    RMA, RMI and NTP. Each imported transformer has a type of its own, so the tap step of that
    type is changed to make (current position x step) reproduce WINDV exactly; a transformer
    at the neutral position is moved to position +-1. Returns the ratios before and after.
    """
    raw = case.transformer.set_index(["i", "j", "ckt"])
    rows = []
    for trafo in app.GetCalcRelevantObjects("*.ElmTr2"):
        hv, lv = _bus(trafo.bushv), _bus(trafo.buslv)
        ckt = trafo.loc_name.split("_", 3)[3]
        key = (hv, lv, ckt) if (hv, lv, ckt) in raw.index else (lv, hv, ckt)
        rec = raw.loc[key]
        typ = trafo.typ_id
        tap_bus = hv if typ.tap_side == 0 else lv
        if tap_bus != key[0] or rec.windv2 != 1.0 or rec.ang1 != 0.0:
            raise NotImplementedError(f"transformer {trafo.loc_name}: tap not on winding 1 only")
        position, step = int(trafo.nntap), float(typ.dutap)
        before = 1 + position * step / 100
        target = float(rec.windv1)
        if abs(target - 1) < 1e-12:
            trafo.nntap = 0
        else:
            if position == 0:
                position = 1 if target > 1 else -1
                trafo.nntap = position
            typ.dutap = (target - 1) / position * 100
        rows.append((trafo.loc_name, key[0], key[1], ckt, target, before))
    return pd.DataFrame(rows, columns=["name", "i", "j", "ckt", "windv1", "ratio_imported"])


def add_contingencies(app, command) -> dict[int, tuple[str, int, int, str]]:
    """One ComOutage per line and transformer in ``command``, except the radial connections.

    Recording limits and filters are opened (``RECORD_ALL``). Returns the branch key of each
    contingency number.
    """
    outages = {}
    for element in app.GetCalcRelevantObjects("*.ElmLne,*.ElmTr2"):
        if element.loc_name in RADIAL:
            continue
        outage = command.CreateObject("ComOutage", element.loc_name)
        outage.SetObjs([element])
        outages[int(outage.GetAttribute("number"))] = branch_key(element)
    for key, value in RECORD_ALL.items():
        command.SetAttribute(key, value)
    return outages


def contingency_results(app, command, outages: dict) -> dict[str, pd.DataFrame]:
    """Run a contingency analysis and read its result file.

    ``outages`` maps contingency numbers to branch keys, as returned by ``add_contingencies``.
    Returns one row per contingency with PowerFactory's convergence flag, the post-contingency
    flows and loading of every branch, and the post-contingency voltage of every bus.
    """
    if command.Execute() != 0:
        raise RuntimeError(f"contingency analysis failed: {output(app)[-5:]}")
    study = app.GetActiveStudyCase()
    res = study.GetContents("Contingency Analysis AC.ElmRes")[0]
    res.Load()
    holder = project_folder(app)
    vec = holder.GetContents("n44_vec.IntVec")
    vec = vec[0] if vec else holder.CreateObject("IntVec", "n44_vec")

    def column(index: int) -> list[float]:
        res.GetColumnValues(vec, index)
        return list(vec.GetAttribute("V"))

    columns = {}
    for c in range(res.GetNumberOfColumns()):
        obj = res.GetObject(c)
        columns[(obj.loc_name if obj.GetClassName() != "ElmRes" else None, res.GetVariable(c))] = c
    numbers = [int(n) if abs(n) < 1e30 else None for n in column(columns[(None, "b:number")])]
    noconv = column(columns[(None, "b:inoconv")])
    # contingency rows only: the base case is -1, and 1e35 marks a row without a value
    rows = [r for r, n in enumerate(numbers) if n in outages]
    if len(rows) != len(outages):
        raise RuntimeError(f"{len(rows)} result rows for {len(outages)} contingencies")
    cases = pd.DataFrame(
        {
            "outage_kind": [outages[numbers[r]][0] for r in rows],
            "outage_bus1": [outages[numbers[r]][1] for r in rows],
            "outage_bus2": [outages[numbers[r]][2] for r in rows],
            "outage_ckt": [outages[numbers[r]][3] for r in rows],
            "converged": [noconv[r] == 0 for r in rows],
        }
    )
    keys = cases[["outage_kind", "outage_bus1", "outage_bus2", "outage_ckt"]]
    branch_parts, bus_parts = [], []
    for element in app.GetCalcRelevantObjects("*.ElmLne,*.ElmTr2"):
        a, b = ("bus1", "bus2") if element.GetClassName() == "ElmLne" else ("bushv", "buslv")
        values = {}
        for var, label in (
            (f"m:P:{a}", "p1_mw"),
            (f"m:Q:{a}", "q1_mvar"),
            (f"m:P:{b}", "p2_mw"),
            (f"m:Q:{b}", "q2_mvar"),
            ("c:loading", "loading_pf_pct"),
        ):
            data = column(columns[(element.loc_name, var)])
            values[label] = [data[r] for r in rows]
        kind, bus1, bus2, ckt = branch_key(element)
        branch_parts.append(keys.assign(kind=kind, bus1=bus1, bus2=bus2, ckt=ckt, **values))
    for term in app.GetCalcRelevantObjects("*.ElmTerm"):
        data = column(columns[(term.loc_name, "m:u")])
        bus_parts.append(
            keys.assign(bus=int(term.loc_name.split()[0]), vm_pu=[data[r] for r in rows])
        )
    return {
        "cases": cases,
        "branches": pd.concat(branch_parts, ignore_index=True),
        "buses": pd.concat(bus_parts, ignore_index=True),
    }


def set_costs(app, costs: dict) -> None:
    """Linear operating costs (config/costs.yaml) and active-power control on every machine.

    Each machine gets a two-point operating cost table, and the LP uses its average slope.
    """
    for sym in app.GetCalcRelevantObjects("*.ElmSym"):
        bus = int(sym.bus1.cterm.loc_name.split()[0])
        plant = costs["plants"][bus]
        tech = plant.get("machines", {}).get(sym.loc_name.split("_", 2)[2], plant["technology"])
        cost = costs["technologies"][tech]
        p_max = sym.GetAttribute("Pmax_uc")
        sym.SetAttribute("ictpg", 1)  # OPF control of active power
        sym.SetAttribute("iOPFCPmin", 1)
        sym.SetAttribute("iOPFCPmax", 1)
        sym.SetAttribute("isConsCostOp", 1)
        sym.SetAttribute("iInterPol", 1)  # piecewise linear
        sym.SetAttribute("cpower", [0.0, p_max])  # MW
        sym.SetAttribute("ccost", [0.0, cost * p_max])  # cost per hour at each point


def set_opf_options(app):
    """The optimal power flow of the active study case, with ``OPF_OPTIONS``."""
    opf = app.GetFromStudyCase("ComOpf")
    for key, value in OPF_OPTIONS.items():
        opf.SetAttribute(key, value)
    return opf
