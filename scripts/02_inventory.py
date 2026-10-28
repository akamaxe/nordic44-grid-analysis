"""Inventory of the downloaded dataset: snapshot files, RAW headers and daily Excel files.

Writes data/processed/inventory_files.csv (one row per PSS/E snapshot file) and prints the
summary used in data/README.md. The days around the 2015 clock changes are inspected in
detail, because Nord Pool reports 23 and 25 hours on those days while the dataset has 24
files per day.
"""

from collections import Counter

import pandas as pd

from n44.config import configured_path
from n44.io.dataset import Dataset, read_member
from n44.io.nordpool import read_daily_sheet
from n44.io.psse_parser import tokenize

CLOCK_CHANGE_DAYS = (pd.Timestamp("2015-03-29").date(), pd.Timestamp("2015-10-25").date())


def raw_headers(files: pd.DataFrame) -> pd.DataFrame:
    """First line of every RAW file: revision, MVA base, frequency and export note."""
    records = []
    for archive, member in zip(files["archive"], files["member"], strict=True):
        first = read_member(archive, member)[:256].decode("latin-1").splitlines()[0]
        fields = tokenize(first)
        records.append(
            (
                int(fields[2]),
                float(fields[1]),
                float(fields[5]),
                first.split("/", 1)[1].strip() if "/" in first else "",
            )
        )
    return pd.DataFrame(
        records, columns=["rev", "sbase", "basfrq", "export_note"], index=files.index
    )


def main() -> None:
    ds = Dataset()
    files = ds.files.copy()
    files = files.join(raw_headers(files))
    out = configured_path("processed_dir") / "inventory_files.csv"
    out.parent.mkdir(parents=True, exist_ok=True)
    files.drop(columns=["archive"]).to_csv(out, index=False)

    days = sorted(files["date"].unique())
    print(f"Days: {len(days)} ({days[0]} to {days[-1]})")
    for kind in ("before", "after"):
        have = set(files.loc[files["kind"] == kind, "h"])
        missing = sorted(set(range(8760)) - have)
        print(f"{kind}_PF files: {len(have)}; missing hours: {len(missing)} {missing[:20]}")
    print(
        "RAW revisions:",
        dict(Counter(files["rev"])),
        "| SBASE:",
        dict(Counter(files["sbase"])),
        "| frequency:",
        dict(Counter(files["basfrq"])),
    )
    stamps = files["export_note"].str.extract(r"(\w{3}, \w{3} \d{1,2} \d{4})")[0]
    print("Export dates (PSS/E stamp):", dict(Counter(stamps).most_common(5)))

    excel = ds.excel_files()
    per_day = excel.groupby("date").size()
    print(
        f"Excel files: {len(excel)}; per day: {dict(Counter(per_day))}; kinds:",
        dict(Counter(excel["kind"])),
    )

    for day in CLOCK_CHANGE_DAYS:
        print(f"\n--- {day} ---")
        for _, row in excel[(excel["date"] == day) & excel["country"].notna()].iterrows():
            sheet = read_daily_sheet(read_member(row["archive"], row["member"]))
            problems = sheet.problems() or "no problems"
            print(
                f"{row['kind']:<11} {row['country']}: {len(sheet.labels)} hour rows "
                f"({sheet.labels[0]} ... {sheet.labels[-1]}); {problems}"
            )


if __name__ == "__main__":
    main()
