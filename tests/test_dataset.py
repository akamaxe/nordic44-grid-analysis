import pandas as pd
import pytest

from n44.io.dataset import read_member
from n44.io.nordpool import column_key, read_psse_in_out


def test_column_key():
    assert column_key("SE1 - SE2") == "SE1_SE2"
    assert column_key("SE\xa0net exchange") == "SE"
    assert column_key("NO Net exchange") == "NO"
    assert column_key("NO1") == "NO1"


@pytest.mark.dataset
def test_every_hour_has_both_snapshots(dataset):
    assert dataset.files.groupby("kind")["h"].nunique().to_dict() == {"after": 8760, "before": 8760}


@pytest.mark.dataset
def test_hour_index(dataset):
    row = dataset.files.query("h == 2010 and kind == 'after'").iloc[0]
    assert (str(row["date"]), row["hour"]) == ("2015-03-25", 18)


@pytest.mark.dataset
def test_exchange_sign_convention(excel_sheet):
    # "A_B" > 0 is an import into A from B, so both sides of a border mirror each other
    se = excel_sheet("2015-03-25", "exchange", "SE").dataset_hours()
    fi = excel_sheet("2015-03-25", "exchange", "FI").dataset_hours()
    pd.testing.assert_series_equal(se["SE3_FI"], -fi["FI_SE3"], check_names=False)


@pytest.mark.dataset
def test_clock_change_days(excel_sheet):
    spring = excel_sheet("2015-03-29", "consumption", "SE")
    autumn = excel_sheet("2015-10-25", "consumption", "SE")
    assert (len(spring.labels), len(autumn.labels)) == (24, 25)
    # The skipped hour 02-03 holds Nord Pool's interpolation and is left out of the day total
    se1 = spring.values["SE1"]
    assert se1[2] == (se1[1] + se1[3]) / 2
    assert se1.drop(index=2).sum() == spring.total["SE1"]


@pytest.mark.dataset
def test_psse_in_out_summary(dataset):
    files = dataset.excel_files()
    row = files[
        (files["date"] == pd.Timestamp("2015-03-25").date()) & (files["kind"] == "PSSE_in_out")
    ].iloc[0]
    areas, flags = read_psse_in_out(read_member(row["archive"], row["member"]))
    assert len(areas) == 240 and flags["Convergence"].all()
    # Interchange PDES of the ten areas adds up to zero
    assert areas.groupby("hour")["scheduled_exchange"].sum().abs().max() <= 2
