"""Reader for PSS/E RAW files, revision 33.

``parse_raw`` turns the text of a RAW file into a :class:`RawCase`: the case header plus one
DataFrame per data section. Column names are the field names of the PSS/E 33 manual in lower
case, with hyphens dropped (``r1_2`` is R1-2).

Only the sections a balanced AC power flow needs are supported. Any other section that holds
data raises :class:`UnsupportedRawError`, so nothing is silently dropped. Fields left out at
the end of a record take the defaults of the manual.
"""

from __future__ import annotations

import csv
import re
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

SUPPORTED_REVISION = 33

# Section order of a revision-33 RAW file, after the three case-identification lines.
SECTIONS = (
    "bus",
    "load",
    "fixed_shunt",
    "generator",
    "branch",
    "transformer",
    "area",
    "two_terminal_dc",
    "vsc_dc",
    "impedance_correction",
    "multi_terminal_dc",
    "multi_section_line",
    "zone",
    "inter_area_transfer",
    "owner",
    "facts",
    "switched_shunt",
    "gne",
    "induction_machine",
)
SUPPORTED_SECTIONS = frozenset(
    {
        "bus",
        "load",
        "fixed_shunt",
        "generator",
        "branch",
        "transformer",
        "area",
        "zone",
        "owner",
        "switched_shunt",
    }
)


class UnsupportedRawError(ValueError):
    """The RAW file uses a revision or a feature this reader does not handle."""


class _Marker:
    """Default that depends on other data; resolved after all sections are read."""

    def __init__(self, name: str):
        self.name = name

    def __repr__(self) -> str:
        return self.name


REQUIRED = _Marker("REQUIRED")
BUS_VALUE = _Marker("BUS_VALUE")  # same value as the record's bus (area, zone, owner)
SYSTEM_BASE = _Marker("SYSTEM_BASE")  # the case MVA base

Field = tuple[str, str, object]  # (name, type "i"/"f"/"s", default)


def _fields(spec: str, defaults: dict | None = None) -> tuple[Field, ...]:
    """Field list from ``"name:type name:type ..."``; unlisted defaults are 0, 0.0 or ''."""
    defaults = defaults or {}
    out = []
    for item in spec.split():
        name, kind = item.split(":")
        out.append((name, kind, defaults.get(name, {"i": 0, "f": 0.0, "s": ""}[kind])))
    return tuple(out)


_OWNERSHIP = "o1:i f1:f o2:i f2:f o3:i f3:f o4:i f4:f"
_OWNERSHIP_DEFAULTS = {"o1": BUS_VALUE, "f1": 1.0, "f2": 1.0, "f3": 1.0, "f4": 1.0}

BUS = _fields(
    "i:i name:s baskv:f ide:i area:i zone:i owner:i vm:f va:f nvhi:f nvlo:f evhi:f evlo:f",
    {
        "i": REQUIRED,
        "ide": 1,
        "area": 1,
        "zone": 1,
        "owner": 1,
        "vm": 1.0,
        "nvhi": 1.1,
        "nvlo": 0.9,
        "evhi": 1.1,
        "evlo": 0.9,
    },
)
LOAD = _fields(
    "i:i id:s status:i area:i zone:i pl:f ql:f ip:f iq:f yp:f yq:f owner:i scale:i intrpt:i",
    {
        "i": REQUIRED,
        "id": "1",
        "status": 1,
        "area": BUS_VALUE,
        "zone": BUS_VALUE,
        "owner": BUS_VALUE,
        "scale": 1,
    },
)
FIXED_SHUNT = _fields("i:i id:s status:i gl:f bl:f", {"i": REQUIRED, "id": "1", "status": 1})
GENERATOR = _fields(
    "i:i id:s pg:f qg:f qt:f qb:f vs:f ireg:i mbase:f zr:f zx:f rt:f xt:f gtap:f stat:i "
    f"rmpct:f pt:f pb:f {_OWNERSHIP} wmod:i wpf:f",
    {
        "i": REQUIRED,
        "id": "1",
        "qt": 9999.0,
        "qb": -9999.0,
        "vs": 1.0,
        "mbase": SYSTEM_BASE,
        "zx": 1.0,
        "gtap": 1.0,
        "stat": 1,
        "rmpct": 100.0,
        "pt": 9999.0,
        "pb": -9999.0,
        "wpf": 1.0,
        **_OWNERSHIP_DEFAULTS,
    },
)
BRANCH = _fields(
    "i:i j:i ckt:s r:f x:f b:f ratea:f rateb:f ratec:f gi:f bi:f gj:f bj:f st:i met:i len:f "
    f"{_OWNERSHIP}",
    {
        "i": REQUIRED,
        "j": REQUIRED,
        "ckt": "1",
        "x": REQUIRED,
        "st": 1,
        "met": 1,
        **_OWNERSHIP_DEFAULTS,
    },
)
TRANSFORMER_LINE1 = _fields(
    f"i:i j:i k:i ckt:s cw:i cz:i cm:i mag1:f mag2:f nmetr:i name:s stat:i {_OWNERSHIP} vecgrp:s",
    {
        "i": REQUIRED,
        "j": REQUIRED,
        "ckt": "1",
        "cw": 1,
        "cz": 1,
        "cm": 1,
        "nmetr": 2,
        "stat": 1,
        **_OWNERSHIP_DEFAULTS,
    },
)
TRANSFORMER_LINE2_2W = _fields(
    "r1_2:f x1_2:f sbase1_2:f", {"x1_2": REQUIRED, "sbase1_2": SYSTEM_BASE}
)
TRANSFORMER_LINE2_3W = _fields(
    "r1_2:f x1_2:f sbase1_2:f r2_3:f x2_3:f sbase2_3:f r3_1:f x3_1:f sbase3_1:f vmstar:f anstar:f",
    {"sbase1_2": SYSTEM_BASE, "sbase2_3": SYSTEM_BASE, "sbase3_1": SYSTEM_BASE, "vmstar": 1.0},
)
_WINDING_SPEC = (
    ("windv", "f"),
    ("nomv", "f"),
    ("ang", "f"),
    ("rata", "f"),
    ("ratb", "f"),
    ("ratc", "f"),
    ("cod", "i"),
    ("cont", "i"),
    ("rma", "f"),
    ("rmi", "f"),
    ("vma", "f"),
    ("vmi", "f"),
    ("ntp", "i"),
    ("tab", "i"),
    ("cr", "f"),
    ("cx", "f"),
    ("cnxa", "f"),
)


def winding_fields(n: int) -> tuple[Field, ...]:
    """Fields of the full winding record ``n`` (lines 3 to 5 of a transformer)."""
    spec = " ".join(f"{name}{n}:{kind}" for name, kind in _WINDING_SPEC)
    return _fields(
        spec,
        {
            f"windv{n}": 1.0,
            f"rma{n}": 1.1,
            f"rmi{n}": 0.9,
            f"vma{n}": 1.1,
            f"vmi{n}": 0.9,
            f"ntp{n}": 33,
        },
    )


TRANSFORMER_LINE4_2W = _fields("windv2:f nomv2:f", {"windv2": 1.0})
TRANSFORMER_2W = TRANSFORMER_LINE1 + TRANSFORMER_LINE2_2W + winding_fields(1) + TRANSFORMER_LINE4_2W
TRANSFORMER_3W = (
    TRANSFORMER_LINE1
    + TRANSFORMER_LINE2_3W
    + winding_fields(1)
    + winding_fields(2)
    + winding_fields(3)
)
AREA = _fields("i:i isw:i pdes:f ptol:f arname:s", {"i": REQUIRED, "ptol": 10.0})
ZONE = _fields("i:i zoname:s", {"i": REQUIRED})
OWNER = _fields("i:i owname:s", {"i": REQUIRED})
SWITCHED_SHUNT = _fields(
    "i:i modsw:i adjm:i stat:i vswhi:f vswlo:f swrem:i rmpct:f rmidnt:s binit:f "
    + " ".join(f"n{k}:i b{k}:f" for k in range(1, 9)),
    {"i": REQUIRED, "modsw": 1, "stat": 1, "vswhi": 1.0, "vswlo": 1.0, "rmpct": 100.0},
)

_SINGLE_LINE_RECORDS = {
    "bus": BUS,
    "load": LOAD,
    "fixed_shunt": FIXED_SHUNT,
    "generator": GENERATOR,
    "branch": BRANCH,
    "area": AREA,
    "zone": ZONE,
    "owner": OWNER,
    "switched_shunt": SWITCHED_SHUNT,
}

# Everything before the first slash outside single quotes; the rest is a comment.
_DATA_PART = re.compile(r"(?:[^'/]|'[^']*')*")
_BLANK_SEPARATED = re.compile(r"'([^']*)'|([^\s']+)")


def tokenize(line: str) -> list[str | None]:
    """Split one record line into fields.

    Fields are separated by commas or blanks. Quoted strings may contain both, and lose their
    quotes and padding. Empty comma-separated fields come back as ``None``.
    """
    data = _DATA_PART.match(line).group(0)
    if "," in data:
        fields = next(csv.reader([data], quotechar="'", skipinitialspace=True))
        return [f.strip() or None for f in fields]
    return [(quoted or bare).strip() for quoted, bare in _BLANK_SEPARATED.findall(data)]


def _to_int(text: str) -> int:
    try:
        return int(text)
    except ValueError:
        return int(float(text))


def _convert(fields: list[str | None], spec: tuple[Field, ...], context: str) -> list:
    values = []
    for k, (name, kind, default) in enumerate(spec):
        raw = fields[k] if k < len(fields) else None
        if raw is None:
            if default is REQUIRED:
                raise ValueError(f"{context}: missing required field '{name}'")
            values.append(default)
        elif kind == "i":
            values.append(_to_int(raw))
        elif kind == "f":
            values.append(float(raw))
        else:
            values.append(raw)
    return values


@dataclass
class RawCase:
    """Contents of a revision-33 RAW file."""

    ic: int
    sbase: float
    rev: int
    xfrrat: int
    nxfrat: int
    basfrq: float
    title: tuple[str, str]
    export_note: str
    bus: pd.DataFrame
    load: pd.DataFrame
    fixed_shunt: pd.DataFrame
    generator: pd.DataFrame
    branch: pd.DataFrame
    transformer: pd.DataFrame
    transformer3: pd.DataFrame
    area: pd.DataFrame
    zone: pd.DataFrame
    owner: pd.DataFrame
    switched_shunt: pd.DataFrame


def _read_header(lines: list[str]) -> dict:
    if len(lines) < 3:
        raise UnsupportedRawError("file too short for a RAW case")
    fields = tokenize(lines[0]) + [None] * 6
    if fields[2] is None or _to_int(fields[2]) != SUPPORTED_REVISION:
        raise UnsupportedRawError(
            f"RAW revision {fields[2]} is not supported (need {SUPPORTED_REVISION})"
        )
    slash = lines[0].find("/")
    return {
        "ic": _to_int(fields[0]),
        "sbase": float(fields[1]),
        "rev": SUPPORTED_REVISION,
        "xfrrat": _to_int(fields[3]) if fields[3] else 0,
        "nxfrat": _to_int(fields[4]) if fields[4] else 0,
        "basfrq": float(fields[5]) if fields[5] else 60.0,
        "title": (lines[1].strip(), lines[2].strip()),
        "export_note": lines[0][slash + 1 :].strip() if slash >= 0 else "",
    }


def _read_sections(lines: list[str]) -> dict[str, list[list]]:
    """Raw value lists per section; reading stops at 'Q' or at the end of the file."""
    rows: dict[str, list[list]] = {name: [] for name in SECTIONS}
    rows["transformer3"] = []
    pos, n_lines, stopped = 3, len(lines), False
    for section in SECTIONS:
        while not stopped:
            if pos >= n_lines:
                stopped = True
                break
            fields = tokenize(lines[pos])
            if not fields:
                pos += 1
                continue
            if len(fields) == 1 and fields[0] in ("0", "Q"):
                stopped = fields[0] == "Q"
                pos += 1
                break
            context = f"line {pos + 1} ({section} data)"
            if section not in SUPPORTED_SECTIONS:
                raise UnsupportedRawError(f"{context}: section '{section}' holds data")
            if section == "transformer":
                three_winding = len(fields) > 2 and fields[2] not in (None, "0")
                n_rec = 5 if three_winding else 4
                if pos + n_rec > n_lines:
                    raise ValueError(f"{context}: truncated transformer record")
                record = [tokenize(lines[pos + k]) for k in range(n_rec)]
                if three_winding:
                    parts = (
                        TRANSFORMER_LINE1,
                        TRANSFORMER_LINE2_3W,
                        *map(winding_fields, (1, 2, 3)),
                    )
                else:
                    parts = (
                        TRANSFORMER_LINE1,
                        TRANSFORMER_LINE2_2W,
                        winding_fields(1),
                        TRANSFORMER_LINE4_2W,
                    )
                values = []
                for part_fields, spec in zip(record, parts, strict=True):
                    values += _convert(part_fields, spec, context)
                rows["transformer3" if three_winding else "transformer"].append(values)
                pos += n_rec
            else:
                rows[section].append(_convert(fields, _SINGLE_LINE_RECORDS[section], context))
                pos += 1
    return rows


_DTYPES = {"i": "int64", "f": "float64", "s": object}
_BUS_COLUMN = {"area": "area", "zone": "zone", "owner": "owner", "o1": "owner"}


def _frame(
    rows: list[list], spec: tuple[Field, ...], bus_values: dict[str, dict], sbase: float
) -> pd.DataFrame:
    """DataFrame of one section, with marker defaults resolved and final dtypes."""
    columns = list(zip(*rows, strict=True)) if rows else [()] * len(spec)
    data = {}
    for k, (name, kind, default) in enumerate(spec):
        values = columns[k]
        if default is BUS_VALUE:
            lookup = bus_values[_BUS_COLUMN[name]]
            values = [
                lookup[bus] if v is BUS_VALUE else v
                for v, bus in zip(values, columns[0], strict=True)
            ]
        elif default is SYSTEM_BASE:
            values = [sbase if v is SYSTEM_BASE else v for v in values]
        data[name] = np.asarray(values, dtype=_DTYPES[kind])
    return pd.DataFrame(data)


def parse_raw(text: str) -> RawCase:
    """Parse the text of a revision-33 PSS/E RAW file."""
    lines = text.splitlines()
    header = _read_header(lines)
    rows = _read_sections(lines)
    sbase = header["sbase"]
    bus = _frame(rows["bus"], BUS, {}, sbase)
    bus_values = {
        column: dict(zip(bus["i"].tolist(), bus[column].tolist(), strict=True))
        for column in ("area", "zone", "owner")
    }
    tables = {
        name: _frame(rows[name], spec, bus_values, sbase)
        for name, spec in (
            ("load", LOAD),
            ("fixed_shunt", FIXED_SHUNT),
            ("generator", GENERATOR),
            ("branch", BRANCH),
            ("transformer", TRANSFORMER_2W),
            ("transformer3", TRANSFORMER_3W),
            ("area", AREA),
            ("zone", ZONE),
            ("owner", OWNER),
            ("switched_shunt", SWITCHED_SHUNT),
        )
    }
    return RawCase(**header, bus=bus, **tables)


def read_raw(path: str | Path) -> RawCase:
    return parse_raw(Path(path).read_text(encoding="latin-1"))
