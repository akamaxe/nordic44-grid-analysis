# Validation log

Each entry is a discrepancy found while validating the model, with its symptom, hypothesis,
cause, fix and the evidence behind it. Entries are in the order they were found.

## 1. The published base case `N44_BC.raw` is not a solved power flow

Found during the dataset analysis, before the network builder was written.

**Symptom.** Evaluating the power balance at the voltages stored in `N44_BC.raw`, with an
admittance matrix built from the PSS/E branch equations, shows non-generator buses that
balance to 0.17 MW and 0.51 Mvar, the rounding level of the file. Q balances at generator
buses too. P, however, is off by up to 65 MW at generator buses, −342 MW in total, and total
generation minus total load is 20 MW while the stored voltages imply 362 MW of losses.

**Hypothesis.** An error in the admittance model would also show up at load buses and in Q,
and neither happens. The P mismatch must therefore come from the generator outputs
themselves.

**Cause.** The mismatch is a constant percentage of output within each area, −3.83 % in
NO1, −1.75 % in NO2, −1.47 % in SE2, −0.16 % in SE3, +0.23 % in FI1 and so on. This is the
footprint of `psspy.scal_2`, the per-area scaling the toolbox applies before every solution.
The base case was saved after a rescaling and before solving again.

**Fix.** The model needs no change. `N44_BC` is excluded from the PSS/E comparison and kept
only for regression tests of the parser and the builder. The PSS/E reference is always an
`hN_after_PF.raw` file, and those balance at every bus. In all 8,760 hours every bus is within
the bound explained by the file's rounding, with a maximum of 0.55 MW and 2.8 Mvar.

**Evidence.** The `n44.ybus` mismatch report and the test `test_base_case_is_not_solved`.

## 2. Transformer metered end read backwards (NMETR)

Found comparing the area interchange computed from PSS/E's voltages with the interchange
PSS/E itself reported in `PSSE_in_out.xlsx`, over all 8,760 hours.

**Symptom.** Eight areas agreed within the rounding of the Excel file. NO2 and NO5 were off by
up to 3.4 MW, in 4,071 hours.

**Hypothesis.** NO2-NO5 is the only border crossed by transformers (5601-6001 and 6000-6001),
so the metered end of transformers was suspect.

**Cause.** In PSS/E, the transformer field NMETR names the non-metered winding, while the
branch field MET names the metered end. The code read NMETR as the metered end.

**Fix.** The metered end is now the winding 2 bus if NMETR = 1 and the winding 1 bus
otherwise, in both `n44.ybus` and `n44.io.build_net`. The largest difference dropped to
1.02 MW, and `PSSE_in_out` rounds to whole MW.

**Evidence.** The summary of `scripts/03_check_psse_consistency.py`.

## 3. PSS/E's overload flag was partly hidden by the toolbox

Found comparing the hours in which some branch is loaded at or above 100 % of RATE A with
PSS/E's limit check in `PSSE_in_out.xlsx`.

**Symptom.** In 1,780 hours the loading computed here was at or above 100 % while
`PSSE_in_out` had no "Rate A" message. It never happened the other way round.

**Hypothesis.** The gap might come from a different loading convention. Seven variants were
tested on 450 sampled hours, combining one end only or both ends, with or without the
line-end shunts, and current or MVA for transformers. None of them closed the gap.

**Cause.** The toolbox writes the Rate A, Rate B and Rate C messages into the same cell, so a
later message overwrites an earlier one. In those hours some branch was also above Rate B
(1,678 h) or Rate C (102 h).

**Fix.** The overload flag is read as "any Rate A/B/C message" (`branch_overload` in
`n44.io.nordpool.read_psse_in_out`). Agreement is then 8,760 of 8,760 hours, with the
loading convention unchanged (terminal current for lines, MVA for transformers, more loaded
end).

**Evidence.** The cross-table in the summary of `scripts/03_check_psse_consistency.py`.

## 4. Area interchange targets not met in some hours (a property of the dataset)

Found by the same check, which compares each area's interchange with its target PDES.

**Symptom.** In 1,086 hours an area was more than 10 MW (PSS/E's tolerance PTOL) away from its
target. It was almost always SE3, and the worst case was 573 MW at h = 8189
(2015-12-08 05:00).

**Analysis.** Two separate effects add up. In 48 hours the ten PDES derived from Nord Pool do
not add up to zero, by up to 575 MW, and SE3 holds the system swing bus (3300), so it absorbs
the sum. In every hour, each of the other nine areas is within 10.2 MW of its target, which is
PSS/E's tolerance, and SE3 collects their residuals, up to 24 MW once the PDES sum is
accounted for.

**Cause.** The Nord Pool exchange data are inconsistent in a few hours, on top of the
interchange tolerance. This is not a reading or modelling error, since the interchange
computed here matches PSS/E's own value within 1 MW in every hour.

**Consequence.** Net positions follow Nord Pool by construction, except for SE3 in those
hours. Comparisons with Nord Pool are made per border and per cut, and these hours are
flagged.

## 5. Larger voltage deviations at two buses over the year (explained, no change)

Found in the annual run. On the ten representative hours the largest voltage difference
against PSS/E was 7.5·10⁻⁶ p.u., and over all 8,760 hours it reached 3.3·10⁻⁵ p.u.

**Analysis.** Of the 53 hours above 10⁻⁵ p.u., the worst bus is always 3100 HJALTA (32 h) or
3701 AJAURE (19 h). These are the two most weakly connected buses, and AJAURE hangs on a line
with X = 2.0 p.u. and a transformer with X = 0.5 p.u. At a weak bus a reactive mismatch moves
the voltage more, and PSS/E's own convergence tolerance of 0.1 MVA corresponds to a few
10⁻⁵ p.u. there. The worst five hours all fall at 00:00, the hour the toolbox solved from the
base case, but only 11 of the 53 do, so the time of day is not the explanation.

**Consequence.** None. The deviation stays 30 times inside the 10⁻³ p.u. tolerance and is a
property of the PSS/E reference, not of the model.

## 6. PowerFactory's PSS/E importer rounds transformer ratios to the tap grid

Found in the first PowerFactory load flow of the imported base case, and then on the 60
validation hours (`powerfactory/run_ldf_batch.py`, `scripts/10_validate_powerfactory.py`).

**Symptom.** Against pandapower, losses and slack power agreed within 0.03 MW, but voltages
differed by up to 8.8·10⁻⁴ p.u. at bus 5601 and by about 2·10⁻⁴ p.u. at 3360 and 6701.
Branch reactive flows differed by up to 2.6 Mvar on four branches around buses 5601 and
5402. That is more than the 2 Mvar (and 2 %) tolerance in all 60 hours, so the model "as
imported" failed it every time. The differences were nearly constant from hour to
hour, 8.81·10⁻⁴ ± 2·10⁻⁶ p.u. at 5601.

**Hypothesis.** After the network data, transformers are the usual suspect when two tools
disagree, and all the buses that stood out sit behind a transformer.

**Cause.** PowerFactory stores a transformer's ratio as an integer tap position. The N44
transformers define a grid from RMA = 1.4 to RMI = 0.6 in NTP = 127 positions, which is
0.63492 % per step, and PowerFactory's importer takes the nearest position. The RAW ratios
WINDV1 are not on that grid. Transformer 5601-6001 has WINDV1 = 1.0054, which becomes
position +1 = 1.00635, a ratio error of 9.5·10⁻⁴. Transformers 3359-3360 (0.9998 rounded to 1.0)
and 6700-6701 (1.0125 rounded to 1.01270) are off by 2·10⁻⁴. The ratios start off-grid in the base case,
and PSS/E's tap stepping moves them by whole steps, so the offset survives in every hour. The
importer prints no warning.

**Fix (for the validation).** A second variant of each hour gives every transformer its exact
RAW ratio. Each imported transformer has a type of its own, so `pf_n44.set_exact_ratios`
changes that type's tap step until position × step = WINDV1 − 1. The model as imported is
still solved and reported, because it is what an engineer gets from a plain import.

**Result.** Over the 60 hours, PowerFactory against pandapower gives the differences below.

| | as imported | exact ratios |
|---|---|---|
| max \|ΔV\| | 8.8·10⁻⁴ p.u. | 5.2·10⁻⁸ p.u. |
| max \|Δθ\| | 5.5·10⁻³° | 1.3·10⁻⁵° |
| max branch \|ΔP\| | 0.60 MW | 3.1·10⁻⁴ MW |
| max branch \|ΔQ\| | 2.6 Mvar | 7.5·10⁻⁴ Mvar |
| max slack \|ΔP\| | 0.025 MW | 8.2·10⁻⁴ MW |
| hours within the tolerances | 0 of 60 | 60 of 60 |

With exact ratios, two independent model builds (this project's parser and builder against
PowerFactory's importer) and two solvers agree to the convergence tolerance of the solvers.
They also agree on which plants end at a reactive limit, in all 60 hours.

**Evidence.** `powerfactory/exports/transformer_ratios.csv` (RAW ratio and imported ratio of
every transformer and hour), `results/validation/powerfactory_triangle.csv`,
`results/figures/validation_powerfactory.png` and the test `test_powerfactory_exports_match`.

## 7. PowerFactory and PSS/E measure branch loading differently

Found with the flows already agreeing to 10⁻³ Mvar, when the loading reported by
PowerFactory (`c:loading`) still differed from PSS/E's. On line 5101-5501 the gap was 33
percentage points (27.5 % against 60.7 % at h = 538), and on transformers it was up to 1.5
points.

**Cause.** There are two definitions, both legitimate. For lines, PowerFactory computes the
loading from the current in the line's series branch and leaves out the end shunts
(`ElmLnecomp`), while PSS/E (NXFRAT = 1) uses the terminal current, end shunts included. Line
5101-5501 carries a 974 Mvar reactor at one end, so its terminal current is more than twice
its series current. Recomputed from pandapower's series currents, PowerFactory's figure
matches to 10⁻⁵ points on every line. For transformers, PowerFactory uses current relative to
rated current and PSS/E (XFRRAT = 0) uses MVA. The two differ by the per-unit voltage at the
more loaded end, 141.4 % against 142.4 % on 5400-5402 at 0.993 p.u.

**Fix.** Neither model needs one. `solution_from_powerfactory` recomputes the loading in
PSS/E's convention from PowerFactory's terminal flows and voltages. That figure agrees with
pandapower's to 10⁻⁵ points, and PowerFactory's own figure is kept as `loading_pf_pct`.

**Consequence.** Overload counts and N-1 rankings depend on the convention, and which
branch is "overloaded" can change between tools with identical flows. Comparisons with
PowerFactory's contingency analysis must therefore use flows, or loading recomputed in one
convention.

## 8. PowerFactory prices energy at its reference machine in the DC-OPF

Found comparing PowerFactory's DC optimal power flow (ComOpf, linear programming,
`powerfactory/run_opf_batch.py`) with `n44.opf.dc_opf` on three hours
(`scripts/17_validate_powerfactory_opf.py`).

**Symptom.** With the first cost table, which counted all of Oskarshamn as nuclear at
10 EUR/MWh, cost, dispatch per technology and branch flows agreed, but every nodal price was
10 EUR/MWh lower in PowerFactory. Where the network is uncongested, PowerFactory's price was
10 and the LP's 20.

**Analysis.** Both prices are an energy component plus a congestion component. The congestion
components agreed to 10⁻¹³ EUR/MWh, so the gap sat in the energy component. PowerFactory's
"system lambda" was 10 EUR/MWh, the cost of its reference machine at Oskarshamn. The LP's dual
was 20 EUR/MWh, because that machine was at its maximum and an extra megawatt had to come
from hydro. PowerFactory takes the reference machine as the marginal unit even when it sits
at its limit. With the final cost table, Oskarshamn (the swing bus, standing for SE3's
flexible plants) is marginal, and the two tools agree on the full nodal prices.

**Other observations.** PowerFactory's DC load flow leaves out the active power of the
line-end shunts. With the same demand in the LP, the two costs agree to the cent, for example
1,172,005.02 against 1,172,005.10 EUR/h at h = 538. PowerFactory adds branch constraints in
outer loops and ends each run with "OPF result does not satisfy all constraints". Its own
check finds line 5301-6001 at 100.5 %, yet its cost equals the LP optimum. The LP has all 79
branch constraints from the start.

**Consequence.** Congestion components and price differences between buses compare directly.
Absolute nodal prices compare only when the reference machine is free to move. Rerunning the
batch with Oskarshamn as nuclear reproduces the gap.

**Evidence.** `results/validation/powerfactory_opf_summary.csv`, where costs, dispatch and
prices agree under the final cost table.

## 9. pandapower reports solutions that do not exist after some outages

Found cross-checking the reference-model N-1 solver (`n44.acpf`) against pandapower's AC
N-1 on the 100 hours of highest snitt-2 transfer (`scripts/11_n1_security.py`).

**Symptom.** Post-contingency loading agreed to 10⁻⁵ points, but in 6 of the 100 hours
pandapower solved one more outage than the reference solver. It was always the Sima
transformer (5300-5301). PowerFactory, in the one of those hours it also ran (h = 6949), found
no solution either.

**Analysis.** Without reactive limits, pandapower does not converge either. It converges only
with them, because its limit loop does not check that the first, all-PV Newton pass
converged. From a diverged pass it reads the plants' reactive output and moves the plants
beyond a limit to PQ, and the second pass then converges. The "solution" holds Blåfalli (6100)
and Kvilldal (6000) at their reactive maximum while their voltages are above set-point,
1.18 p.u. against 1.00 at Blåfalli. A plant at Qmax can only sit below its set-point, so this
one would be back on voltage control.

**Cause.** pandapower's `_run_ac_pf_with_qlims_enforced` applies limits from a diverged pass.
The stored state solves the power-flow equations, but with an inconsistent set of PQ plants.
The cases are near the transfer limit of the remaining path, where no consistent solution
exists.

**Fix.** `n44.contingency.run_n1` rejects a pandapower solution whose limited plants
contradict their limit (`n44.powerflow._q_limit_report`). `n44.acpf` applies limits only after
a converged pass and flags inconsistent plants, and it found none in the 100 hours. With the
check, pandapower and the reference solver agree on convergence in 100 of 100 hours.

**Evidence.** `results/n1_crosscheck.csv` (convergence counts, `fast_inconsistent`).
