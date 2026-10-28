"""Shared fixtures. Tests marked ``dataset`` need the Zenodo archives in data/raw/zenodo."""

from pathlib import Path

import pandas as pd
import pytest

from n44.config import configured_path

FIXTURES = Path(__file__).parent / "data"


def _dataset_available() -> bool:
    return any(configured_path("zenodo_dir").glob("Week*.zip"))


def pytest_collection_modifyitems(config, items):
    if _dataset_available():
        return
    skip = pytest.mark.skip(reason="Zenodo dataset not downloaded (scripts/01_download_data.py)")
    for item in items:
        if "dataset" in item.keywords:
            item.add_marker(skip)


@pytest.fixture(scope="session")
def dataset():
    from n44.io.dataset import Dataset

    return Dataset()


@pytest.fixture(scope="session")
def excel_sheet(dataset):
    """Reader for one daily Excel file: ``excel_sheet("2015-03-25", "exchange", "SE")``."""
    from n44.io.dataset import read_member
    from n44.io.nordpool import read_daily_sheet

    files = dataset.excel_files()

    def read(day: str, kind: str, country: str):
        row = files[
            (files["date"] == pd.Timestamp(day).date())
            & (files["kind"] == kind)
            & (files["country"] == country)
        ].iloc[0]
        return read_daily_sheet(read_member(row["archive"], row["member"]))

    return read
