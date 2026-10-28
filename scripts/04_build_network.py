"""Build the base pandapower network and draw the zone map.

The static model is the same in all 8,760 snapshots (one parameter signature in the level-A
check), so the base network comes from the base case N44_BC. Hourly values are applied on top
of it by ``n44.snapshots.load_snapshot``.

Writes data/processed/n44_base.json and results/figures/n44_zones.png.
"""

import pandapower as pp

from n44.config import configured_path, load_config
from n44.io.build_net import build_net
from n44.io.dataset import Dataset
from n44.io.psse_parser import parse_raw
from n44.plotting import plot_zone_map, read_coordinates


def main() -> None:
    net = build_net(parse_raw(Dataset().base_case_text()))
    out = configured_path("processed_dir") / "n44_base.json"
    pp.to_json(net, str(out))
    print(
        f"{out.name}: {len(net.bus)} buses, {len(net.line)} lines, {len(net.trafo)} "
        f"transformers, {len(net.gen)} gens + {len(net.ext_grid)} ext_grid"
    )
    url = load_config()["dataset"]["geo_coordinates_url"]
    coordinates = read_coordinates(configured_path("github_dir") / url.rsplit("/", 1)[-1])
    figure = plot_zone_map(
        net, coordinates, configured_path("results_dir") / "figures" / "n44_zones.png"
    )
    print(f"figure: {figure}")


if __name__ == "__main__":
    main()
