"""PowerFactory's DC-OPF against this project's.

Reads the exports of powerfactory/run_opf_batch.py and solves the same hours with
``n44.opf.dc_opf``. PowerFactory's DC load flow leaves out the active power of the line-end
shunts, so the comparison uses the same demand.

It compares:
- total cost;
- dispatch per technology;
- the congestion component of the nodal prices;
- the energy component, which the two tools define differently (validation log, entry 8).

Writes results/validation/powerfactory_opf_summary.csv.
"""

import numpy as np
import pandas as pd

from n44.config import configured_path, load_config, repo_path
from n44.contingency import branch_table, dc_model
from n44.opf import cost_assumptions, dc_opf, demand_vector, machine_table
from n44.snapshots import SnapshotStore, base_net, load_snapshot


def machine_cost(bus: int, machine: str, costs: dict) -> float:
    plant = costs["plants"][int(bus)]
    return costs["technologies"][plant.get("machines", {}).get(str(machine), plant["technology"])]


def main() -> None:
    folder = repo_path(load_config()["powerfactory"]["exports_dir"])
    buses = pd.read_csv(folder / "opf_buses.csv")
    machines = pd.read_csv(folder / "opf_machines.csv", dtype={"id": str})
    costs = cost_assumptions()
    machines["cost"] = [
        machine_cost(b, i, costs) for b, i in zip(machines.bus, machines.id, strict=True)
    ]
    net, store = base_net(), SnapshotStore()
    table = branch_table(net)
    dc = dc_model(net)
    units = machine_table(net)
    rows = []
    for h in sorted(buses.h.unique()):
        load_snapshot(h, store, net)
        shunt = net.shunt[net.shunt.in_service].p_mw.groupby(net.shunt.bus).sum()
        demand = demand_vector(net).sub(shunt, fill_value=0.0)  # as PowerFactory's DC load flow
        own = dc_opf(dc, units, demand, table.rate_a)
        pf = machines[machines.h == h]
        pf_bus = buses[buses.h == h].set_index("bus")
        own_tech = pd.Series(own.dispatch.to_numpy(), index=units.technology).groupby(level=0).sum()
        pf_tech = pf.groupby(
            pf.cost.map({v: k for k, v in costs["technologies"].items()})
        ).p_mw.sum()
        congestion_pf = pf_bus.lmp - pf_bus.system_lambda
        congestion_own = (own.lmp - own.energy).loc[pf_bus.index]
        rows.append(
            {
                "h": h,
                "cost_own_eur": own.cost,
                "cost_powerfactory_eur": float((pf.p_mw * pf.cost).sum()),
                "max_dtech_mw": float(
                    (own_tech - pf_tech.reindex(own_tech.index, fill_value=0)).abs().max()
                ),
                "max_dcongestion_eur": float(np.abs(congestion_pf - congestion_own).max()),
                "energy_own_eur": own.energy,
                "energy_powerfactory_eur": float(pf_bus.system_lambda.iloc[0]),
                "binding_branches": int((own.shadow > 1e-6).sum()),
                "max_congestion_eur": float(congestion_own.abs().max()),
            }
        )
        print(rows[-1], flush=True)
    out = configured_path("results_dir") / "validation"
    pd.DataFrame(rows).to_csv(
        out / "powerfactory_opf_summary.csv", index=False, float_format="%.6g"
    )


if __name__ == "__main__":
    main()
