import numpy as np
import pandas as pd
import pytest
import requests

from n44.io import entsoe
from n44.snapshots import calendar

NS = "urn:iec62325.351:tc57wg16:451-3:publicationdocument:7:0"


def _document(*series: str) -> str:
    return (
        f'<Publication_MarketDocument xmlns="{NS}">{"".join(series)}</Publication_MarketDocument>'
    )


def _series(start, end, resolution, points, curve="A01", value="quantity", extra=""):
    body = "".join(
        f"<Point><position>{p}</position><{value}>{v}</{value}></Point>" for p, v in points
    )
    return (
        f"<TimeSeries><mRID>1</mRID><curveType>{curve}</curveType>{extra}"
        f"<Period><timeInterval><start>{start}</start><end>{end}</end></timeInterval>"
        f"<resolution>{resolution}</resolution>{body}</Period></TimeSeries>"
    )


def test_step_curve_holds_each_value_until_the_next_point():
    text = _document(
        _series("2015-01-22T23:00Z", "2015-01-23T23:00Z", "PT60M", [(1, 6400), (10, 6500)], "A03")
    )
    values = entsoe.hourly(entsoe.parse(text))
    assert len(values) == 24
    assert (values.iloc[:9] == 6400).all() and (values.iloc[9:] == 6500).all()
    assert values.index[0] == pd.Timestamp("2015-01-22 23:00", tz="UTC")


def test_quarter_hours_are_averaged_and_missing_points_stay_missing():
    points = [(1, 100), (2, 200), (3, 300), (4, 400), (5, 10), (6, 10), (7, 10), (8, 10)]
    text = _document(_series("2025-11-11T23:00Z", "2025-11-12T02:00Z", "PT15M", points))
    values = entsoe.hourly(entsoe.parse(text))
    assert values.tolist()[:2] == [250.0, 10.0]
    assert np.isnan(values.iloc[2])  # positions 9-12 were not sent


def test_prices_over_two_periods_and_series_split_by_type():
    days = _series("2015-01-01T23:00Z", "2015-01-02T23:00Z", "PT60M", [(1, 20.5)], "A03")
    later = _series("2015-01-02T23:00Z", "2015-01-03T23:00Z", "PT60M", [(1, 25.0)], "A03")
    prices = entsoe.hourly(entsoe.parse(_document(days, later).replace("quantity", "price.amount")))
    assert len(prices) == 48 and prices.iloc[0] == 20.5 and prices.iloc[-1] == 25.0
    wind = "<MktPSRType><psrType>B19</psrType></MktPSRType>"
    nuclear = "<MktPSRType><psrType>B14</psrType></MktPSRType>"
    start, end = "2015-01-01T23:00Z", "2015-01-02T01:00Z"
    text = _document(
        _series(start, end, "PT60M", [(1, 5), (2, 6)], extra=wind),
        _series(start, end, "PT60M", [(1, 900), (2, 910)], extra=nuclear),
    )
    frame = entsoe.hourly(entsoe.parse(text), key="psrType")
    assert list(frame.columns) == ["B14", "B19"]
    assert frame.B19.tolist() == [5.0, 6.0]


def test_acknowledgements():
    def ack(reason):
        return (
            "<Acknowledgement_MarketDocument><Reason><code>999</code>"
            f"<text>{reason}</text></Reason></Acknowledgement_MarketDocument>"
        )

    assert entsoe.parse(ack("No matching data found for Data item X")) == []
    with pytest.raises(entsoe.EntsoeError, match="not valid"):
        entsoe.parse(ack("The combination is not valid"))


def test_hours_follow_the_project_calendar():
    for year, spring in ((2015, 2090), (2025, 24 * 88 + 2)):
        cal = calendar(year)
        utc = pd.date_range(cal.utc_time.min(), cal.utc_time.max(), freq="h")
        values = pd.Series(np.arange(len(utc), dtype=float), index=utc)
        hours = entsoe.to_hours(values, year)
        assert len(hours) == 8760 and hours.notna().all()
        assert cal.synthetic.idxmax() == spring
        assert hours[spring] == pytest.approx((hours[spring - 1] + hours[spring + 1]) / 2)
        assert hours[0] == 0.0  # h0 is 23:00 UTC on 31 December


def test_short_gaps_are_filled_and_long_ones_reported():
    s = pd.Series([1.0, np.nan, np.nan, 4.0, np.nan, np.nan, np.nan, np.nan, 9.0])
    filled, missing = entsoe.fill_gaps(s.to_frame("x"), limit=3)
    assert filled.x.iloc[1:3].tolist() == [2.0, 3.0]
    assert filled.x.iloc[4:8].isna().all() and missing.x == 4


def test_the_token_never_appears_in_errors(monkeypatch, tmp_path):
    secret = "0123abcd-not-a-real-token"
    monkeypatch.setenv(entsoe.TOKEN_VARIABLE, secret)
    monkeypatch.setattr(entsoe.time, "sleep", lambda s: None)

    def fail(url, params, timeout):
        raise requests.ConnectionError(
            f"cannot reach {url}?securityToken={params['securityToken']}"
        )

    monkeypatch.setattr(entsoe.requests, "get", fail)
    with pytest.raises(entsoe.EntsoeError) as err:
        entsoe.fetch({"documentType": "A44"}, tmp_path / "x.xml.gz", retries=2)
    assert secret not in str(err.value) and "<token>" in str(err.value)
    assert not (tmp_path / "x.xml.gz").exists()
    monkeypatch.delenv(entsoe.TOKEN_VARIABLE)
    with pytest.raises(entsoe.EntsoeError, match=entsoe.TOKEN_VARIABLE):
        entsoe.fetch({"documentType": "A44"})
