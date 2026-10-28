"""Access to the Nordic 44 dataset on Zenodo (record 162907, CC BY-NC 4.0).

The 53 weekly archives hold one folder per day, ``N44_YYYYMMDD/``, each with PSS/E, CIM and
Modelica snapshots. Only ``PSSESnapshots/`` is used here. Its files are read straight out of
the zip archives rather than extracted, because the CIM folders alone would take about 11 GB
on disk.

Snapshot hours are indexed as ``h = 24 * (day_of_year - 1) + hour``, so ``h`` runs from 0
to 8759 over 2015. ``hour`` is the Nord Pool hour index of the file name, in local time.
"""

from __future__ import annotations

import fnmatch
import hashlib
import re
import zipfile
from dataclasses import dataclass
from functools import cache
from pathlib import Path

import pandas as pd
import requests

from n44.config import configured_path, load_config

ZENODO_API = "https://zenodo.org/api/records/{record}"
DEFAULT_PATTERNS = ("Week*.zip", "Models.zip")
BASE_CASE_MEMBER = "PSSE Base case/N44_BC.raw"

_RAW_MEMBER = re.compile(r"N44_(\d{8})/PSSESnapshots/h(\d{1,2})_(before|after)_PF\.raw$")
_XLSX_MEMBER = re.compile(r"N44_(\d{8})/PSSESnapshots/([A-Za-z]+)_([A-Z]{2})\.xlsx$")
# The original toolbox wrote "Procution_*.xlsx" in some versions.
_XLSX_KIND = {
    "Consumption": "consumption",
    "Production": "production",
    "Procution": "production",
    "Exchange": "exchange",
}


@dataclass(frozen=True)
class RemoteFile:
    key: str
    size: int
    md5: str
    url: str


def list_record_files(record: int) -> list[RemoteFile]:
    """Files of a Zenodo record, with sizes and MD5 checksums."""
    resp = requests.get(ZENODO_API.format(record=record), timeout=60)
    resp.raise_for_status()
    files = []
    for entry in resp.json()["files"]:
        algorithm, _, digest = entry["checksum"].partition(":")
        if algorithm != "md5":
            raise ValueError(f"unexpected checksum type for {entry['key']}: {algorithm}")
        files.append(RemoteFile(entry["key"], int(entry["size"]), digest, entry["links"]["self"]))
    return files


def md5sum(path: Path, chunk_size: int = 1 << 20) -> str:
    digest = hashlib.md5()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(chunk_size), b""):
            digest.update(block)
    return digest.hexdigest()


def download(url: str, dest: Path, md5: str | None = None) -> Path:
    """Stream ``url`` to ``dest``; when ``md5`` is given the file must match it."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    partial = dest.with_name(dest.name + ".part")
    with requests.get(url, stream=True, timeout=120) as resp:
        resp.raise_for_status()
        with open(partial, "wb") as fh:
            for block in resp.iter_content(chunk_size=1 << 20):
                fh.write(block)
    if md5 is not None and (found := md5sum(partial)) != md5:
        partial.unlink()
        raise OSError(f"MD5 mismatch for {dest.name}: expected {md5}, got {found}")
    partial.replace(dest)
    return dest


def download_record(
    record: int, dest_dir: Path, patterns: tuple[str, ...] = DEFAULT_PATTERNS
) -> list[Path]:
    """Download the record files whose names match ``patterns``.

    Files already on disk with the right size and checksum are kept.
    """
    paths = []
    for remote in list_record_files(record):
        if not any(fnmatch.fnmatch(remote.key, pattern) for pattern in patterns):
            continue
        dest = dest_dir / remote.key
        if dest.exists() and dest.stat().st_size == remote.size and md5sum(dest) == remote.md5:
            paths.append(dest)
            continue
        paths.append(download(remote.url, dest, remote.md5))
    return paths


@cache
def _open_archive(path: Path) -> zipfile.ZipFile:
    return zipfile.ZipFile(path)


def read_member(archive: Path, member: str) -> bytes:
    return _open_archive(Path(archive)).read(member)


def read_text(archive: Path, member: str) -> str:
    # RAW files are plain ASCII; latin-1 never fails on stray bytes.
    return read_member(archive, member).decode("latin-1")


def _week_number(path: Path) -> int:
    return int(re.search(r"(\d+)", path.stem).group(1))


def _hour_index(day: pd.Series, hour: pd.Series) -> pd.Series:
    return (pd.to_datetime(day).dt.dayofyear - 1) * 24 + hour


def index_snapshot_files(zenodo_dir: Path) -> pd.DataFrame:
    """One row per PSS/E snapshot file in the weekly archives.

    Columns: ``h``, ``date``, ``hour``, ``kind`` ("before"/"after"), ``archive``, ``member``.
    """
    rows = []
    for archive in sorted(zenodo_dir.glob("Week*.zip"), key=_week_number):
        for member in _open_archive(archive).namelist():
            if m := _RAW_MEMBER.search(member):
                day = pd.Timestamp(m.group(1)).date()
                rows.append((day, int(m.group(2)), m.group(3), archive, member))
    files = pd.DataFrame(rows, columns=["date", "hour", "kind", "archive", "member"])
    files.insert(0, "h", _hour_index(files["date"], files["hour"]))
    return files.sort_values(["h", "kind"], ignore_index=True)


def index_excel_files(zenodo_dir: Path) -> pd.DataFrame:
    """One row per daily Excel file.

    Columns: ``date``, ``kind`` (consumption/production/exchange, or the file stem for
    ``PSSE_in_out``), ``country``, ``archive``, ``member``.
    """
    rows = []
    for archive in sorted(zenodo_dir.glob("Week*.zip"), key=_week_number):
        for member in _open_archive(archive).namelist():
            if not member.endswith(".xlsx") or "/PSSESnapshots/" not in member:
                continue
            day = pd.Timestamp(re.search(r"N44_(\d{8})", member).group(1)).date()
            if m := _XLSX_MEMBER.search(member):
                kind = _XLSX_KIND.get(m.group(2), m.group(2))
                rows.append((day, kind, m.group(3), archive, member))
            else:
                rows.append((day, Path(member).stem, None, archive, member))
    return pd.DataFrame(rows, columns=["date", "kind", "country", "archive", "member"])


class Dataset:
    """Index of the downloaded dataset, with readers for snapshot files.

    >>> ds = Dataset()
    >>> text = ds.raw_text(h=2010, kind="after")   # 2015-03-25 h18
    """

    def __init__(self, zenodo_dir: Path | None = None):
        self.zenodo_dir = Path(zenodo_dir) if zenodo_dir else configured_path("zenodo_dir")
        if not any(self.zenodo_dir.glob("Week*.zip")):
            raise FileNotFoundError(
                f"no weekly archives in {self.zenodo_dir}; run scripts/01_download_data.py"
            )
        self.files = index_snapshot_files(self.zenodo_dir)
        self._by_key = self.files.set_index(["h", "kind"])

    def has(self, h: int, kind: str = "after") -> bool:
        return (h, kind) in self._by_key.index

    def raw_text(self, h: int, kind: str = "after") -> str:
        row = self._by_key.loc[(h, kind)]
        return read_text(row["archive"], row["member"])

    def base_case_text(self) -> str:
        archive = self.zenodo_dir / "Models.zip"
        member = next(n for n in _open_archive(archive).namelist() if n.endswith(BASE_CASE_MEMBER))
        return read_text(archive, member)

    def excel_files(self) -> pd.DataFrame:
        return index_excel_files(self.zenodo_dir)


def download_dataset() -> list[Path]:
    """Download the weekly archives, ``Models.zip`` and the bus coordinates."""
    cfg = load_config()
    paths = download_record(cfg["dataset"]["zenodo_record"], configured_path("zenodo_dir"))
    url = cfg["dataset"]["geo_coordinates_url"]
    coords = configured_path("github_dir") / url.rsplit("/", 1)[-1]
    if not coords.exists():
        download(url, coords)
    return [*paths, coords]
