"""Single-line diagram of the Nordic 44 projects, drawn the same way in every project.

The busbars sit at fixed positions (``BUSES``), roughly as on the map, with Norway to the west,
Sweden in the middle and Finland to the east. Machines hang above their busbar and loads below
it, spaced so that every name fits. Lines and transformers leave from the ends of the busbar with
a short vertical leg and run straight to the other busbar, parallel circuits side by side.

PowerFactory's diagram layout tool creates the graphic objects, one per element, and ``draw``
then gives each of them its position and its connection lines, so the drawing depends neither on
the tool nor on the project. It also shows every element's name in one line, colours the zones
while no calculation is shown and writes the zone names. ``signature`` sums up a drawn diagram,
to check that two projects show the same drawing.

All lengths are in mm, with y upwards, as in PowerFactory's schematic diagrams.
"""

from __future__ import annotations

import hashlib
import math
import tempfile
from collections import defaultdict
from pathlib import Path

import pf_n44

GRID = 4.375  # PowerFactory's drawing grid
PITCH = 3 * GRID  # between the machines (or loads) of a busbar, room for their names
SLOT = 2 * GRID  # between the line and transformer connections of a busbar
END_LEFT = 2 * GRID  # free busbar left of the first connection, next to its result box
END_RIGHT = GRID  # free busbar right of the last connection
DROP = 3 * GRID  # from a busbar to the centre of its machines and loads
PORT = GRID  # from the centre of a symbol to its connection point
STUB = 2 * GRID  # vertical leg of a line or transformer at the busbar
STUB_STEP = 1.5 * GRID  # longer leg for each further connection on the same end and side
BAR_UNIT = 26.25  # length of a busbar symbol of size 1

# Centre of each busbar, placed so that no name, symbol or line covers another
BUSES = {
    # NO1
    5100: (791.875, 608.125),  # Tretten
    5101: (730.625, 546.875),  # Hasle
    5103: (542.5, 573.125),  # Kongsberg
    5400: (586.25, 441.875),  # Oslo1
    5401: (546.875, 411.25),  # Sylling
    5402: (573.125, 494.375),  # Kaggefoss
    5500: (695.625, 411.25),  # Oslo2
    5501: (590.625, 367.5),  # Skien
    # NO2
    5600: (323.75, 328.125),  # Kristiansand
    5601: (280.0, 411.25),  # Stavanger
    5602: (551.25, 328.125),  # Sandefjord
    5603: (538.125, 288.75),  # Arendal
    5610: (450.625, 245.0),  # Kristiansand HVDC
    5620: (275.625, 288.75),  # Feda HVDC
    6000: (140.0, 367.5),  # Kvilldal
    6100: (153.125, 494.375),  # Blåfalli
    # NO3
    6500: (691.25, 730.625),  # Trondheim
    # NO4
    6700: (770.0, 844.375),  # Røssåga
    6701: (914.375, 927.5),  # Ofoten
    # NO5
    5102: (446.25, 573.125),  # Dagali
    5300: (188.125, 573.125),  # Sima
    5301: (315.0, 638.75),  # Aurland
    5304: (428.75, 638.75),  # Geilo
    5305: (354.375, 682.5),  # Eidfjord
    6001: (376.25, 525.0),  # Hagafoss
    # SE1
    3115: (1036.875, 901.25),  # Porjus
    # SE2
    3100: (910.0, 494.375),  # Hjälta
    3244: (914.375, 783.125),  # Högasen
    3245: (945.0, 730.625),  # Järpströmmen
    3249: (813.75, 980.0),  # Grundfors
    3701: (818.125, 901.25),  # Ajaure
    # SE3
    3000: (1054.375, 525.0),  # Forsmark
    3020: (1128.75, 573.125),  # Dannebo HVDC
    3200: (870.625, 245.0),  # Tenhult
    3300: (953.75, 166.25),  # Oskarshamn
    3359: (791.875, 328.125),  # Ringhals
    3360: (822.5, 441.875),  # Stenkullen HVDC
    # SE4
    8500: (743.75, 166.25),  # Malmö
    8600: (695.625, 78.75),  # Arrie HVDC
    8700: (783.125, 105.0),  # Karlshamn HVDC
    # FI1
    7000: (1303.75, 455.0),  # Helsinki
    7010: (1491.875, 411.25),  # Vyborg HVDC
    7020: (1277.5, 328.125),  # Estlink HVDC
    7100: (1172.5, 901.25),  # Oulu
}

# Colour of each zone's grid while no calculation is shown (blues for Norway, browns and
# oranges for Sweden, green for Finland), and the centre of the zone's name
ZONES = {
    "NO1": ("1F4E9C", (630.0, 481.25)),
    "NO2": ("0E7C86", (354.375, 358.75)),
    "NO3": ("6050C8", (691.25, 761.25)),
    "NO4": ("2B6CB0", (883.75, 883.75)),
    "NO5": ("3B8ED9", (350.0, 603.75)),
    "SE1": ("9C6A12", (1036.875, 931.875)),
    "SE2": ("D35A26", (866.25, 778.75)),
    "SE3": ("9A3412", (923.125, 367.5)),
    "SE4": ("B5651D", (726.25, 118.125)),
    "FI1": ("15803D", (1312.5, 525.0)),
}
ZONE_TEXT = 6.0  # height of the zone names
COLOUR_BY_GRID = 7  # PowerFactory's "Grids" colouring

# Label boxes as PowerFactory sets them up for each symbol (layer "Labels", reference points,
# offset, rotation), to which draw() adds a width that fits the name
_BOX = {
    "ifont": 5,
    "anr": 0,
    "vlpos": 0,
    "sh_frame": 0,
    "do_rot": 3,
    "iKeepPos": 0,
    "iRot1_4": 0,
    "iLines": 1,
    "zlvl": 1,
    "glvl": 2,
    "iOrient": 1,
    "iHorAlignPar": 1,
    "iVerAlignPar": 0,
    "iBoxRefPt": 3,
    "iSmBox": 0,
    "iLinSt": 1,
    "iCol": -1,
    "iParRefPt": 7,
    "iArrowVis": 1,
    "center_x": 0.0,
    "center_y": 0.0,
    "rotate": 0.0,
}
LABEL_BOXES = {
    "TermStrip": _BOX
    | {
        "ifont": 4,
        "iOrient": 2,
        "iHorAlignPar": 0,
        "iVerAlignPar": 1,
        "iBoxRefPt": 1,
        "iParRefPt": 5,
        "center_x": -0.65625,
        "center_y": 0.2625,
    },
    "d_symg": _BOX,
    "d_load": _BOX,
    "d_lin": _BOX
    | {
        "iKeepPos": 1,
        "iVerAlignPar": 1,
        "iBoxRefPt": 7,
        "iParRefPt": 0,
        "center_x": -1.3125,
        "rotate": math.pi / 2,
    },
    "d_tr2": _BOX
    | {
        "iKeepPos": 1,
        "iHorAlignPar": 0,
        "iVerAlignPar": 1,
        "iBoxRefPt": 7,
        "iParRefPt": 5,
        "center_x": -0.875,
        "rotate": math.pi / 2,
    },
}


def topology(app) -> dict:
    """Busbars, lines, transformers, machines and loads of the active project."""
    lines = [
        (o.loc_name, "line", pf_n44._bus(o.bus1), pf_n44._bus(o.bus2))
        for o in app.GetCalcRelevantObjects("*.ElmLne")
    ]
    trafos = [
        (o.loc_name, "trafo", pf_n44._bus(o.bushv), pf_n44._bus(o.buslv))
        for o in app.GetCalcRelevantObjects("*.ElmTr2")
    ]
    units = [
        (o.loc_name, "gen", pf_n44._bus(o.bus1)) for o in app.GetCalcRelevantObjects("*.ElmSym")
    ]
    units += [
        (o.loc_name, "load", pf_n44._bus(o.bus1)) for o in app.GetCalcRelevantObjects("*.ElmLod")
    ]
    return {
        "buses": {
            int(t.loc_name.split()[0]): t.loc_name for t in app.GetCalcRelevantObjects("*.ElmTerm")
        },
        "branches": sorted(lines + trafos),
        "units": units,
    }


def shapes(topo: dict) -> dict[str, dict]:
    """Symbol, centre, rotation and connection lines of every element, by element name.

    Each branch connection is a polyline from the element (a transformer's terminal, or the
    middle of a line) to its busbar; connection 0 is a line's first bus or a transformer's HV
    side, as in PowerFactory.
    """
    parts = defaultdict(lambda: {"gen": [], "load": [], "ends": []})
    for name, kind, bus in topo["units"]:
        parts[bus][kind].append(name)
    for k, (_, _, a, b) in enumerate(topo["branches"]):
        parts[a]["ends"].append((k, 0, b))
        parts[b]["ends"].append((k, 1, a))
    order = _parallel_order(topo["branches"])
    sides = _sides(topo["branches"], parts)
    out, legs = {}, {}
    for bus, (bx, by) in BUSES.items():
        gens = sorted(parts[bus]["gen"], key=_unit_id)
        loads = sorted(parts[bus]["load"], key=_unit_id)
        ends = {"L": [], "R": []}
        for k, end, far in parts[bus]["ends"]:
            side = sides[(k, end)]
            dx, dy = BUSES[far][0] - bx, BUSES[far][1] - by
            up = dy > 0 if abs(dy) > 1e-9 else True
            corridor, rank = order[k]
            # inner slots for steeper branches; parallel circuits keep their order at both ends
            p, q = BUSES[corridor[0]], BUSES[corridor[1]]
            inward = (SLOT if side == "L" else -SLOT, STUB_STEP if up else -STUB_STEP)
            sign = 1 if (p[1] - q[1]) * inward[0] + (q[0] - p[0]) * inward[1] > 0 else -1
            steep = round(math.degrees(math.atan2(abs(dy), abs(dx))), 6)
            ends[side].append((steep, corridor, sign * rank, k, end, up))
        length = _length(parts[bus])
        x0 = bx - length / 2
        out[topo["buses"][bus]] = {
            "sym": "TermStrip",
            "centre": (bx, by),
            "rot": 0,
            "size": length / BAR_UNIT,
            "con": {},
        }
        u0 = x0 + END_LEFT + SLOT * len(ends["L"])
        for i, name in enumerate(gens):
            x = u0 + PITCH * (i + 0.5)
            out[name] = {
                "sym": "d_symg",
                "centre": (x, by + DROP),
                "rot": 180,
                "con": {0: [(x, by + DROP - PORT), (x, by)]},
            }
        for i, name in enumerate(loads):
            x = u0 + PITCH * (i + 0.5)
            out[name] = {
                "sym": "d_load",
                "centre": (x, by - DROP),
                "rot": 0,
                "con": {0: [(x, by - DROP + PORT), (x, by)]},
            }
        for side, items in ends.items():
            grown = {True: 0, False: 0}
            for j, (_, _, _, k, end, up) in enumerate(sorted(items)):
                if side == "L":
                    x = x0 + END_LEFT + SLOT * (j + 0.5)
                else:
                    x = x0 + length - END_RIGHT - SLOT * (j + 0.5)
                stub = STUB + STUB_STEP * grown[up]
                grown[up] += 1
                legs[(k, end)] = ((x, by), (x, by + (stub if up else -stub)))
    for k, (name, kind, _, _) in enumerate(topo["branches"]):
        (pa, sa), (pb, sb) = legs[(k, 0)], legs[(k, 1)]
        vx, vy = sb[0] - sa[0], sb[1] - sa[1]
        if kind == "line":
            mid = (sa[0] + vx / 2, sa[1] + vy / 2)
            angle = math.degrees(math.atan2(vy, vx))  # the name reads along the line
            angle = angle - 180 if angle > 90 else angle + 180 if angle <= -90 else angle
            out[name] = {
                "sym": "d_lin",
                "centre": mid,
                "rot": (angle + 90) % 360,
                "con": {0: [mid, sa, pa], 1: [mid, sb, pb]},
            }
        else:
            centre = (sa[0] + vx / 2, sa[1] + vy / 2)
            # the HV terminal sits at +PORT on the symbol's axis, turned by iRot from upwards
            rot = round(math.degrees(math.atan2(-vy, -vx)) - 90) % 360
            ux, uy = -math.sin(math.radians(rot)), math.cos(math.radians(rot))
            hv = (centre[0] + PORT * ux, centre[1] + PORT * uy)
            lv = (centre[0] - PORT * ux, centre[1] - PORT * uy)
            out[name] = {
                "sym": "d_tr2",
                "centre": centre,
                "rot": rot,
                "con": {0: [hv, sa, pa], 1: [lv, sb, pb]},
            }
    return out


def draw(app) -> None:
    """Draw the single-line diagram of the active project, which has no diagram yet."""
    project = app.GetActiveProject()
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
    diagrams = project.GetContents("*.IntGrfnet", 1)
    if not ok or len(diagrams) != 1:
        raise RuntimeError(f"diagram layout failed: {pf_n44.output(app)[-3:]}")
    diagram = diagrams[0]
    diagram.loc_name = "Nordic 44"
    geometry = shapes(topology(app))
    label = _label_format(project)
    for g in diagram.GetContents("*.IntGrf"):
        name = g.GetAttribute("pDataObj").loc_name
        s = geometry[name]
        g.SetAttribute("rCenterX", float(s["centre"][0]))
        g.SetAttribute("rCenterY", float(s["centre"][1]))
        g.SetAttribute("iRot", int(round(s["rot"])) % 360)
        if s["sym"] == "TermStrip":
            g.SetAttribute("rSizeX", float(s["size"]))
        # the layout tool now and then leaves out one connection of a line, so any missing
        # connection is added
        cons = {int(c.GetAttribute("iDatConNr")): c for c in g.GetContents("*.IntGrfcon")}
        for number, points in s["con"].items():
            con = cons.get(number)
            if con is None:
                con = g.CreateObject("IntGrfcon", f"GCO_{number + 1}")
                con.SetAttribute("iDatConNr", number)
                con.SetAttribute("iLinSt", 1)
                con.SetAttribute("rLinWd", 0.0)
            con.SetAttribute("rX", [float(p[0]) for p in points] + [-1.0] * (20 - len(points)))
            con.SetAttribute("rY", [float(p[1]) for p in points] + [-1.0] * (20 - len(points)))
        # a label box as wide as the name, which PowerFactory would otherwise cut short
        for old in g.GetContents("*.SetVitxt"):
            old.Delete()
        box = g.CreateObject("SetVitxt", f"Label{s['sym']}")
        for key, value in LABEL_BOXES[s["sym"]].items():
            box.SetAttribute(key, value)
        box.SetAttribute("chars", len(name) + 5)
        box.SetAttribute("format", label)
    _zones(app, project, diagram)
    _page(project, diagram, geometry)


def signature(project) -> str:
    """Short hash of the positions and connection lines of a project's diagram."""
    rows = []
    for diagram in project.GetContents("*.IntGrfnet", 1):
        for g in diagram.GetContents("*.IntGrf"):
            row = [
                g.GetAttribute("pDataObj").loc_name,
                g.GetAttribute("sSymNam"),
                g.GetAttribute("iRot"),
                round(g.GetAttribute("rSizeX"), 4),
            ]
            row += [round(g.GetAttribute(a), 3) for a in ("rCenterX", "rCenterY")]
            for con in sorted(
                g.GetContents("*.IntGrfcon"), key=lambda c: c.GetAttribute("iDatConNr")
            ):
                row += [round(v, 3) for v in con.GetAttribute("rX") + con.GetAttribute("rY")]
            rows.append(repr(row))
    return hashlib.sha1("\n".join(sorted(rows)).encode()).hexdigest()[:12]


def _label_format(project):
    """Project label format that shows only the element's name (the default adds its type)."""
    settings = project.GetContents("Settings.SetFold")[0]
    formats = settings.GetContents("Formats.SetFoldform")
    formats = formats[0] if formats else settings.CreateObject("SetFoldform", "Formats")
    grf = formats.GetContents("Grf.IntFolder")
    grf = grf[0] if grf else formats.CreateObject("IntFolder", "Grf")
    for old in grf.GetContents("Label.IntFormsel"):
        old.Delete()
    selection = grf.CreateObject("IntFormsel", "Label")
    selection.SetAttribute("usedfor", "Label")
    form = selection.CreateObject("IntForm", "branch_bas_sym")
    form.SetAttribute("otarget", "branch")
    form.SetAttribute("lstr", ["# $N,@:e:loc_name"])
    return selection


def _zones(app, project, diagram) -> None:
    """Zone colours on the grids, colouring by grid without results, and the zone names."""
    for grid in app.GetCalcRelevantObjects("*.ElmNet"):
        zone = grid.loc_name.split()[-1]
        if zone in ZONES:
            grid.SetAttribute("icolor", _argb(ZONES[zone][0]))
    scheme = project.GetContents("*.SetColscheme", 1)[0]
    scheme.SetColouring("basic", -1, -1, COLOUR_BY_GRID)
    # PowerFactory's text format for annotations, with the colour, font, height and centre
    lines = ["$G"]
    for zone, (colour, (x, y)) in ZONES.items():
        lines.append(
            f"T,{_argb(colour)},0,0,6,{ZONE_TEXT:.6f},0,1,{zone},{x:.6f},{y:.6f},0,0.000000,1,0"
        )
    lines.append("/E")
    layer = diagram.GetContents("Layers.IntFolder")[0].GetContents("Annotations.IntGrflayer")[0]
    layer.ClearData()
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "zones.txt"
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        layer.Import(str(path))


def _page(project, diagram, geometry) -> None:
    """Size the diagram's page to the drawing."""
    page = diagram.GetContents("Settings.IntFolder")[0].GetContents("Format.SetGrfpage")[0]
    form = page.GetAttribute("pDrwFrm")
    if form is None:  # the layout tool refers to its page format by name
        folder = project.GetContents("Settings.SetFold")[0].GetContents("Page Formats.SetFoldpage")[
            0
        ]
        form = folder.GetContents(f"{page.GetAttribute('aDrwFrm')}.SetFormat")[0]
        page.SetAttribute("pDrwFrm", form)
    xs = [x for s in geometry.values() for x, _ in [s["centre"]]]
    ys = [y for s in geometry.values() for _, y in [s["centre"]]]
    form.SetAttribute("iSizeX", math.ceil(max(xs) + 60))
    form.SetAttribute("iSizeY", math.ceil(max(ys) + 40))


def _length(part: dict) -> float:
    units = max(len(part["gen"]), len(part["load"]))
    return max(END_LEFT + END_RIGHT + SLOT * len(part["ends"]) + PITCH * units, BAR_UNIT)


def _parallel_order(branches) -> dict[int, tuple]:
    """Corridor (pair of buses) and rank of every branch among the circuits of its corridor."""
    corridors = defaultdict(list)
    for k, (name, _, a, b) in enumerate(branches):
        corridors[(min(a, b), max(a, b))].append((name, k))
    return {
        k: (c, r) for c, members in corridors.items() for r, (_, k) in enumerate(sorted(members))
    }


def _sides(branches, parts) -> dict[tuple, str]:
    """Busbar end (L or R) of both connections of every branch.

    The end towards the other busbar, unless the straight leg between the two busbars would
    then cross the machines or loads of either of them.
    """
    areas = {}
    for bus, (cx, cy) in BUSES.items():
        left = sum(1 for _, _, far in parts[bus]["ends"] if BUSES[far][0] < cx)
        x0 = cx - _length(parts[bus]) / 2 + END_LEFT + SLOT * left
        x1 = x0 + PITCH * max(len(parts[bus]["gen"]), len(parts[bus]["load"]))
        areas[bus] = [_rect(x0, cy + 5, x1, cy + 20)] * bool(parts[bus]["gen"])
        areas[bus] += [_rect(x0, cy - 20, x1, cy - 5)] * bool(parts[bus]["load"])
    out = {}
    for k, (_, _, a, b) in enumerate(branches):
        (ax, ay), (bx, by) = BUSES[a], BUSES[b]
        best = None
        for sa in "LR":
            for sb in "LR":
                pa = (_slot_x(ax, parts[a], sa), ay + (STUB if by > ay or by == ay else -STUB))
                pb = (_slot_x(bx, parts[b], sb), by + (STUB if ay > by or ay == by else -STUB))
                hits = sum(_overlap([pa, pb], area) for area in areas[a] + areas[b])
                natural = (sa == ("L" if bx < ax else "R")) + (sb == ("L" if ax < bx else "R"))
                cost = (hits, -natural, math.dist(pa, pb))
                if best is None or cost < best[0]:
                    best = (cost, sa, sb)
        out[(k, 0)], out[(k, 1)] = best[1], best[2]
    return out


def _slot_x(cx, part, side) -> float:
    """x of the outermost slot at one end of a busbar."""
    half = _length(part) / 2
    return cx - half + END_LEFT + SLOT / 2 if side == "L" else cx + half - END_RIGHT - SLOT / 2


def _rect(x0, y0, x1, y1) -> list:
    return [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]


def _overlap(a: list, b: list) -> bool:
    """Whether two convex polygons (or a segment and a polygon) overlap, by separating axes."""
    for poly in (a, b):
        for (px, py), (qx, qy) in zip(poly, poly[1:] + poly[:1], strict=True):
            nx, ny = qy - py, px - qx
            pa = [x * nx + y * ny for x, y in a]
            pb = [x * nx + y * ny for x, y in b]
            if max(pa) <= min(pb) or max(pb) <= min(pa):
                return False
    return True


def _unit_id(name: str) -> tuple:
    tail = name.rsplit("_", 1)[-1]
    return (0, int(tail)) if tail.isdigit() else (1, tail)


def _argb(rgb: str) -> int:
    """PowerFactory colour, an opaque ARGB value as a signed 32-bit integer."""
    return int("FF" + rgb, 16) - (1 << 32)
