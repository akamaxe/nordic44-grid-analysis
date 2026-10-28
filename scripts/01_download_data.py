"""Download the Nordic 44 dataset (Zenodo record 162907) and the bus coordinates.

About 550 MB: the 53 weekly archives and Models.zip. Files already present with the right MD5
checksum are skipped, so the script can be re-run safely.
"""

from n44.io.dataset import download_dataset


def main() -> None:
    for path in download_dataset():
        print(f"{path.stat().st_size / 1e6:8.1f} MB  {path.name}")


if __name__ == "__main__":
    main()
