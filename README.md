# Nordic 44 grid analysis

Load flow, N-1 security, optimal power flow and redispatch for every hour of 2015 on
Nordic 44, a 44-bus equivalent of the Nordic synchronous grid (Sweden, Norway, Finland),
using hourly snapshots matched to Nord Pool market data. The analysis runs in Python on
[pandapower](https://www.pandapower.org/) and an AC solver written for the project. The
PSS/E solutions that come with the dataset and a separate DIgSILENT PowerFactory model of
the same data are the references, so three tools solve the same network. The
PowerFactory projects are included and open without Python.

![Where the network limits the market dispatch under N-1](results/figures/n1_bottlenecks.png)

The full write-up, with every table and figure, is in [docs/report.md](docs/report.md).

## Main results

- The three tools agree. With exact transformer ratios, pandapower and PowerFactory 2024
  match to 5·10⁻⁸ p.u. and 3·10⁻⁴ MW, and both reproduce PSS/E to the rounding of its
  files. The N-1 and DC-OPF results match as well.
- The market dispatch of the equivalent is N-1 secure in only 324 hours of 2015. A full AC
  N-1 of every hour (8,760 h × 71 outages, about 622,000 cases at 0.5 ms each) finds 11.8 %
  of the cases insecure.
- Snitt 2 (SE2->SE3) limits the north-south transfer. Its N-1 transfer capability is 3.2 GW
  in the median hour, and the market flow is above it in 5,991 hours. The TSOs offered the
  market a median of 6.5 GW on this cut in 2015, so the equivalent overstates the
  congestion there.
- Making the market dispatch N-1 secure takes 19.2 TWh of redispatch (5.6 % of demand),
  almost all of it countertrade from the north into SE3. One outage that only exists
  because of how the equivalent aggregates plants accounts for 8.1 TWh of it.
- A second Hjälta-Grundfors circuit, at the N-1 bottleneck, cuts the N-1 redispatch by 23 %
  and lifts snitt 2's median N-1 capability from 3.2 to 5.0 GW. A third Hjälta-Ringhals
  circuit, where the intact network binds, changes almost nothing (+0.3 %).
- The 2015 grid cannot carry the flows of 2025 securely. With 2025 snapshots built from
  ENTSO-E data, snitt 2's mean flow rises from 3.6 to 5.6 GW and the dispatch is N-1
  secure in 121 hours instead of 324.

The report also covers new industrial load in the north, a DC screening of the N-1 cases
and a comparison with ENTSO-E's 2015 capacities, flows and prices.

## Results in figures

Each figure links to its section of the report.

<table>
<tr>
<td width="50%"><a href="docs/report.md#p1-how-well-does-n44-reproduce-nord-pools-inter-zonal-flows"><img src="results/figures/border_errors.png" alt="Border flow errors against Nord Pool"></a><br>Border flows against Nord Pool. The large errors come from a loop round the NO1-NO2-NO5 triangle and two ties that exist only in the equivalent.</td>
<td width="50%"><a href="docs/report.md#p2-which-corridors-limit-the-north-south-transfer"><img src="results/figures/cuts_duration.png" alt="Duration curves of the Swedish cuts"></a><br>Duration curves of the Swedish cuts. Snitt 2 is the binding cut, and snitt 4 almost never binds.</td>
</tr>
<tr>
<td><a href="docs/report.md#p3-which-contingencies-are-critical-and-how-much-n-1-margin-is-there"><img src="results/figures/n1_ranking.png" alt="Outages that break N-1"></a><br>The outages that make the most hours insecure, out of 621,960 AC contingency cases.</td>
<td><a href="docs/report.md#n-1-transfer-capability-of-the-cuts"><img src="results/figures/transfer_margins.png" alt="N-1 transfer capability against the market flow"></a><br>N-1 transfer capability of the cuts against the market flow.</td>
</tr>
<tr>
<td><a href="docs/report.md#p4-nodal-prices-and-the-redispatch-the-market-dispatch-needs"><img src="results/figures/redispatch.png" alt="Redispatch over the year and by zone"></a><br>N-1 redispatch over 2015 and by zone: down in the north and western Norway, up in SE3.</td>
<td><a href="docs/report.md#p5-reinforcements-and-new-northern-demand"><img src="results/figures/scenarios_reinforcement.png" alt="Redispatch with each reinforcement"></a><br>Redispatch and snitt 2's N-1 capability with each reinforcement option.</td>
</tr>
<tr>
<td><a href="docs/report.md#p7-the-2015-grid-with-the-flows-of-2025"><img src="results/figures/year2025_cuts.png" alt="Swedish cuts in 2015 and 2025"></a><br>Transfer through the Swedish cuts in 2015 and with the flows of 2025.</td>
<td><a href="docs/report.md#against-powerfactory"><img src="results/figures/validation_powerfactory.png" alt="PowerFactory against pandapower"></a><br>PowerFactory against pandapower on 60 hours, as imported and with exact transformer ratios.</td>
</tr>
</table>

## How it works

The dataset holds a PSS/E case for every hour of 2015, scaled to Nord Pool's hourly
production, consumption and exchanges. A parser written for the project reads the RAW
files, and two models are built from them: a reference admittance matrix written straight
from the PSS/E branch equations, and a pandapower network whose admittance matrix equals
it. pandapower solves every hour in AC from a flat start. The full AC N-1 runs on a
Newton-Raphson solver written on the reference model, at about 0.5 ms per case. The DC-OPF
and the redispatch are linear programs in PTDF form solved with HiGHS, under assumed
generation costs (nuclear 10, hydro 20, thermal 35 EUR/MWh). The equations are in
[docs/methods.md](docs/methods.md).

Every step is a numbered script in `scripts/`, and the library is the `n44` package in
`src/n44/`.

| Step | Scripts | What they do |
| --- | --- | --- |
| Data | `01_download_data.py`, `02_inventory.py`, `03_check_psse_consistency.py` | download the dataset, list its files and check every PSS/E solution |
| Model | `04_build_network.py`, `05_build_snapshots.py` | build the pandapower network and the hourly tables |
| Checks | `06_validate_psse.py`, `07_nordpool_dc_comparison.py` | pandapower against PSS/E, the borders against Nord Pool, AC against DC |
| Year | `08_powerflow_8760.py`, `09_annual_analysis.py` | AC load flow of every hour, cuts and borders |
| N-1 | `11_n1_security.py`, `12_transfer_margins.py` | AC N-1 of every hour and the cuts' N-1 capability |
| Market | `14_opf_prices.py`, `15_redispatch.py` | DC-OPF with nodal prices, and the N-1 redispatch |
| Scenarios | `16_scenarios.py` | reinforcements, new load and new wind |
| ENTSO-E | `18_download_entsoe.py`, `19_market_comparison.py` | download the ENTSO-E data and compare 2015's capacities, flows and prices with the model |
| 2025 | `20_fit_snapshots.py`, `21_year_2025.py` | rebuild the snapshots with the dataset's method, check it on 2015, and run 2025 on the 2015 grid |
| PowerFactory | `10_validate_powerfactory.py`, `13_validate_powerfactory_n1.py`, `17_validate_powerfactory_opf.py` | load flow, N-1 and DC-OPF against PowerFactory |

## Validation

Each reference is checked on its own, so a mistake in one layer cannot hide in another.

| Level | Check | Result |
| --- | --- | --- |
| Data | power balance at PSS/E's own solution, from the RAW data and the PSS/E branch equations, 8,760 hours | every bus within the bound set by the file's rounding (max 0.55 MW, 2.8 Mvar) |
| Conversion | pandapower's admittance matrix against the PSS/E reference | 4·10⁻¹⁴ p.u. |
| Load flow | pandapower from a flat start against PSS/E, 8,760 hours | \|ΔV\| ≤ 3.3·10⁻⁵ p.u., \|Δθ\| ≤ 1.1·10⁻³°, slack within 0.12 MW, and branch overloads in exactly the 4,794 hours PSS/E flagged |
| Second tool | PowerFactory 2024 with its own PSS/E importer, 60 hours | 5·10⁻⁸ p.u., 3·10⁻⁴ MW and 7·10⁻⁴ Mvar against pandapower with exact transformer ratios, and the same reactive-limited plants |
| N-1 | PowerFactory's contingency analysis on 5 critical hours × 71 outages, and pandapower's AC N-1 on 100 hours | the same outages without a solution, flows to 8·10⁻⁴ MW and voltages to 5·10⁻⁷ p.u., and the same ten worst outages in the same order |
| OPF | PowerFactory's DC-OPF on 3 hours and pandapower's `rundcopp` on 10 | cost to the cent, same nodal prices |

Nine discrepancies came up during the validation. None was resolved by tuning, and each is
written up with its cause and evidence in the [validation log](docs/validation_log.md).

## Limitations

- N44 is a 44-bus equivalent. Its branch ratings are not the real corridor limits, and
  each generator bus aggregates many plants. Overloads, N-1 violations and redispatch
  volumes show where the equivalent concentrates flow, not real operating violations. On
  snitt 2 the TSOs offered the market twice the equivalent's median N-1 capability in 2015
  (6.5 against 3.2 GW), while on snitt 1 and snitt 4 the two are within 13 %.
- The annual run re-analyses PSS/E's solution of each hour. Taps, area-slack outputs and
  voltage setpoints come from PSS/E, so the run is not an independent dispatch.
- Costs are assumptions (nuclear 10, hydro 20, thermal 35 EUR/MWh). The nodal prices are
  therefore only structural, and the redispatch is reported as volume.
- ENTSO-E holds no Nordic redispatch or countertrade volumes for 2015, so the redispatch
  has nothing real to be compared with.
- The 2025 snapshots keep the 2015 grid. New lines such as SydVästlänken and the Aurora
  Line are missing, and each zone's 2025 generation, wind included, sits on the 2015
  equivalent's machines in proportion to their output.

## Reproducing the results

The pipeline needs Python 3.12. Three commands set up the environment, run the pipeline
and run the tests.

```powershell
# Windows
python -m venv .venv; .venv\Scripts\python -m pip install -r requirements.txt -e .
.venv\Scripts\python scripts/run_pipeline.py
.venv\Scripts\python -m pytest
```

```bash
# Linux and macOS
python3 -m venv .venv && .venv/bin/python -m pip install -r requirements.txt -e .
.venv/bin/python scripts/run_pipeline.py
.venv/bin/python -m pytest
```

On a fresh clone the pipeline takes about three hours with the default three worker
processes, each of which can take about 1.2 GB of memory. It downloads the dataset (about
530 MB, MD5-checked), checks every PSS/E solution, builds the hourly tables and solves the
year (`--workers N`, 3 by default). Then it runs the N-1 analysis, the OPF, the
redispatch, the transfer capability and the scenarios, and regenerates every figure and
table. Steps already done are skipped, and `--force` runs them again.

PowerFactory is optional. The comparisons read the PowerFactory results committed in
`powerfactory/exports/`, and `--powerfactory` re-runs PowerFactory first.

The steps on ENTSO-E data (18 to 21) need a free token from the ENTSO-E Transparency
Platform in the `ENTSOE_API_KEY` environment variable, set up as described in
[data/README.md](data/README.md#entso-e-data). Without it they are skipped and everything
else runs. The first download takes about half an hour, and fitting and analysing 2025
about another hour.

## PowerFactory projects

`powerfactory/Nordic44_2015.pfd` holds the base case and twelve hours of 2015 as
PowerFactory projects, imported with PowerFactory's own PSS/E importer. They open in
PowerFactory 2024 SP2 or newer.

1. Import the file with File > Import > Data.
2. Activate a project, for example `N44_h0538`, the winter peak of 2015.
3. Run the load flow, the contingency analysis (71 outages) and the DC optimal power flow
   from the Calculation menu. The settings of the comparison are already in place.

Each project has the exact transformer ratios of its RAW file, the cost assumptions and the
same single-line diagram, laid out like the map with each bidding zone in its own colour.
[powerfactory/README.md](powerfactory/README.md) shows the diagram, lists the projects and
the values each study should give, and explains how the projects and the exported results
were made.

## Repository layout

```text
config/        paths, solver options, tolerances, cost assumptions, scenarios
data/          dataset documentation (raw and processed data are downloaded or generated)
docs/          full report, methods and validation log
src/n44/       PSS/E parser, reference model, pandapower builder, AC solver, N-1, OPF,
               redispatch, scenarios, ENTSO-E client, snapshot fitting, validation, figures
scripts/       numbered pipeline steps and run_pipeline.py
powerfactory/  PowerFactory projects (.pfd), the scripts that drive PowerFactory, and its results
results/       figures and summary tables
tests/         pytest suite on small fixtures from the dataset
```

## Data, licences and references

- The code is under the MIT licence (see `LICENSE`).
- The dataset is L. Vanfretti et al., *Nordic44 – 2015 Powerflow Data*, Zenodo,
  [doi:10.5281/zenodo.162907](https://doi.org/10.5281/zenodo.162907), licensed
  CC BY-NC 4.0. The pipeline downloads it. This repository redistributes only what is
  derived from it in small form, the test fixtures in `tests/data/`, the PowerFactory
  projects and the PowerFactory results in `powerfactory/`, all under the same licence.
  Details are in [data/README.md](data/README.md).
- The market data of 2015 and 2025 come from the
  [ENTSO-E Transparency Platform](https://transparency.entsoe.eu), under its terms of use.
  The pipeline downloads them with the user's own token, and only tables and figures
  derived from them are committed.
- Bus coordinates for the maps come from the original toolbox,
  [ALSETLab/Nordic44-Nordpool](https://github.com/ALSETLab/Nordic44-Nordpool) (GPL-3.0).
  They are fetched at run time and not stored here.
- If you use this work, please cite the dataset paper and the paper that first presented
  the model.
  - L. Vanfretti et al., "An open data repository and a data processing software toolset of
    an equivalent Nordic grid model matched to historical electricity market data",
    *Data in Brief* 11 (2017) 349-357, doi:10.1016/j.dib.2017.02.021.
  - L. Vanfretti, T. Rabuzin, M. Baudette, M. Murad, "iTesla Power Systems Library (iPSL):
    A Modelica library for phasor time-domain simulations", *SoftwareX* (2016),
    doi:10.1016/j.softx.2016.05.001.
