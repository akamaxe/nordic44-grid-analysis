# Methods

How each step of the analysis is computed, from the PSS/E files to the scenarios. The results
are in the [README](../README.md), and the discrepancies found on the way are in the
[validation log](validation_log.md). Values are per unit on the PSS/E system base of
1000 MVA and on the bus base voltages unless stated otherwise.

- [PSS/E parser and reference model](#psse-parser-and-reference-model)
- [Conversion to pandapower](#conversion-to-pandapower)
- [Annual load flow](#annual-load-flow)
- [N-1 security](#n-1-security)
- [Transfer capability of the cuts](#transfer-capability-of-the-cuts)
- [DC optimal power flow and nodal prices](#dc-optimal-power-flow-and-nodal-prices)
- [Redispatch](#redispatch)
- [Scenarios](#scenarios)

## PSS/E parser and reference model

`n44.io.psse_parser` reads the revision-33 RAW format, with its section order, record
defaults, quoted identifiers, the four-line transformer records and the conventions of the
CW, CZ and CM codes. It rejects what the model cannot represent (three-winding transformers,
non-unity CW/CZ/CM, voltage-dependent loads) instead of approximating it.

`n44.ybus` builds a reference admittance matrix straight from the PSS/E branch equations,
independently of any power-flow tool. A line is a π model with series admittance
$`y = 1/(R + jX)`$, half the charging at each end and the line shunts $`G_I + jB_I`$ and
$`G_J + jB_J`$. A two-winding transformer has an ideal transformer
$`t_1 = W_1 e^{j\theta_1}`$ at bus I, the series impedance $`Z = R + jX`$ and an ideal
transformer $`t_2 = W_2`$ at bus J, which gives

```math
Y_{II} = \frac{y}{|t_1|^2}, \qquad Y_{IJ} = -\frac{y}{t_1^{*}\,t_2}, \qquad
Y_{JI} = -\frac{y}{t_1\,t_2}, \qquad Y_{JJ} = \frac{y}{t_2^{2}}, \qquad y = \frac{1}{Z}.
```

Flows computed from these two-ports are terminal flows, so they include the line shunts.
Loading follows PSS/E. With NXFRAT = 1, line ratings are currents expressed as MVA, and the
loading of a line is $`|I| \, S_\text{base} / \text{RATE}`$. With XFRRAT = 0, transformer
ratings are MVA, and the loading is $`|S| / \text{RATE}`$. Both are taken at the more loaded
end. With this convention, "some branch at or above 100 % of RATE A" matches PSS/E's own
overload flag in all 8,760 hours.

## Conversion to pandapower

`n44.io.build_net` turns a PSS/E case into a pandapower network. Every rule below is exact for
the Nordic 44 data, and the tests check that pandapower's admittance matrix equals the
reference one to machine precision.

### Per-unit system

- PSS/E per-unit values are on SBASE = **1000 MVA** (not the usual 100) and on the bus base
  voltage BASKV.
- The pandapower network uses `net.sn_mva = 1000` and `bus.vn_kv = BASKV`, so its internal
  per-unit system is the same and the two admittance matrices can be compared entry by entry.
- The bus index of the pandapower network is the PSS/E bus number.

### Lines

PSS/E gives R, X and the total charging B in per unit, plus a line shunt at each end
(`GI + jBI` at bus I, `GJ + jBJ` at bus J).

| PSS/E | pandapower `line` |
|---|---|
| R, X | `r_ohm_per_km`, `x_ohm_per_km` = R·Zb, X·Zb with `length_km = 1` and Zb = BASKV²/SBASE |
| B | `c_nf_per_km` = B / (Zb · 2πf) · 10⁹ (pandapower splits it into halves too) |
| RATEA | `max_i_ka` = RATEA / (√3 · BASKV) |
| GI+jBI, GJ+jBJ | one `shunt` per end, with `p_mw` = G·SBASE, `q_mvar` = −B·SBASE, `parent_line` and `parent_end` |

NXFRAT = 1, so line ratings are *current expressed as MVA* (√3 · BASKV · I), which is exactly
what `max_i_ka` encodes.

Line-end shunts matter. On 5101–5501, BI = −0.974 and BJ = +0.974 p.u., about ±974 Mvar, and
without them the power balance at bus 5501 is off by 991.5 Mvar. This is one of the
negative-control tests. pandapower lines cannot hold asymmetric end shunts, so they are
separate shunts, and every status change goes through `set_branch_status`, which switches
them together with their line.

### Two-winding transformers

All 12 transformers have CW = CZ = CM = 1, with turns ratios in per unit of the bus base
voltage and impedance in per unit on SBASE, and none has a magnetizing admittance. All have
NOMV = 0 and WINDV2 = 1. The builder rejects anything else.

pandapower (`build_branch.py`, version 3.5.6) runs the branch from the HV bus to the LV bus,
places the off-nominal ratio on the HV side,
`n = (vn_hv_kv / vn_lv_kv) / (Vb_hv / Vb_lv)`, and refers the short-circuit impedance to the
LV side, `z = vk_percent/100 · (sn_net / sn_mva) · (vn_lv_kv / Vb_lv)²`.

Setting each winding's rated voltage to `vn_w_kv = t_w · Vb_w` (turns ratio × bus base) makes
the ratio `n = t_hv / t_lv` and the per-unit impedance `z = |Z| · t_lv²`, provided
`vk_percent = 100·|Z|·sn_mva/SBASE` (and likewise `vkr_percent` from R). In MATPOWER form
(from = HV) this gives `Y_HH = y/t_hv²`, `Y_LL = y/t_lv²` and `Y_HL = −y/(t_hv·t_lv)`.

This is the PSS/E matrix above, **whichever side winding 1 is on**. In N44 the tap is on the
420 kV side for some transformers (5101–5100) and on the 300 kV side for others (5300–5301).
The phase shift is applied on the HV side, so `shift_degree = ANG1` when winding 1 is HV and
`−ANG1` otherwise. All ANG1 are zero in N44.

| PSS/E | pandapower `trafo` |
|---|---|
| WINDV1, WINDV2, BASKV | `vn_hv_kv`, `vn_lv_kv` = WINDV·BASKV of each winding |
| RATA1 | `sn_mva` = RATA1, so pandapower's loading refers to RATE A |
| R1-2, X1-2 | `vkr_percent` = 100·R·sn/SBASE, `vk_percent` = 100·\|Z\|·sn/SBASE |
| ANG1 | `shift_degree` = ±ANG1 |
| MAG1, MAG2 (= 0) | `pfe_kw = i0_percent = 0` |
| taps chosen by PSS/E | written into `vn_*_kv`, with no tap changer (`tap_changer_type = None`) |

XFRRAT = 0, so transformer ratings are MVA, and the power flow runs with
`trafo_loading="power"`. The default, "current", would scale by `vn_*_kv`, which here include
the turns ratio. Putting the ratio on the wrong winding unbalances bus 6001 by 2,247 Mvar,
which is the second negative-control test. `trafo_model` "t" and "pi" give identical results,
because there is no magnetizing branch.

### Machines

The swing bus (3300 OSKARSHAMN) is one `ext_grid` that aggregates its six identical machines.
Its voltage setpoint is their VS, and its angle is the bus's angle in the case. pandapower
does not enforce the ext_grid's summed limits in a power flow, and they are kept for OPF.
There is never a `gen` on that bus, because pandapower would give all of the bus's reactive
power to the gen and all of its active power to the ext_grid.

Every other machine is a `gen` (74 of them), with P, VS, the QT/QB and PT/PB limits, and
MBASE. All machines regulate their own bus (IREG = 0), and machines on the same bus share VS.
Reactive output is compared per plant (per bus), because pandapower and PSS/E split it among
identical units differently.

Reactive limits are enforced in two steps. The power flow is first solved with every plant on
voltage control, and the limits are then enforced from that solution. pandapower's own limit
loop already works this way. It converges first, then switches every plant beyond a limit to
PQ at once and never back. On 200 random hours it matched an explicit two-step solve within
1e-12, with the same limited plants, and on the representative hours the limited plants are
the ones PSS/E limited. `run_ac` then checks each limited plant. A plant at Qmax must be below
its setpoint, and one at Qmin above it.

### Loads, flows and interchange

Loads are constant power (all IP, IQ, YP, YQ are zero). The 13 loads that stand for exchanges
are tagged `kind = "exchange"` (see `n44.zones.EXCHANGE_LOADS` and the
[data README](../data/README.md)).

Branch flows are reported in PSS/E orientation (I → J) and, for lines, include the shunt of
each end. On 5101–5501 this changes the loading from 21.5 % (series part only) to 58.8 %.
Losses are ΣPG − ΣPL, so line-end shunt conductances count as branch losses.

Area interchange and border flows use each tie's metered end. For non-transformer branches
MET names the metered end, but for transformers **NMETR names the non-metered winding**.
Reading it the other way round shifted the NO2/NO5 interchange by up to 3.4 MW
([validation log, entry 2](validation_log.md#2-transformer-metered-end-read-backwards-nmetr)).
With the right convention, the interchange computed at PSS/E's voltages matches the
interchange PSS/E reported (`PSSE_in_out.xlsx`) within the rounding of that file.

### Verification

| Level | Check | Result |
|---|---|---|
| A, data | Power balance at PSS/E's solution, built from the RAW data with the PSS/E equations, all 8,760 `after_PF` files | Every bus within the bound explained by the file's rounding (max 0.55 MW, 2.8 Mvar) |
| B, conversion | pandapower's internal Ybus against the reference Ybus | Max difference 4·10⁻¹⁴ p.u. |
| C, solution | pandapower from a flat start against PSS/E (2015-03-25 h18) | \|ΔVm\| 7·10⁻⁶ p.u., \|Δθ\| 1·10⁻⁴°, swing P within 0.01 MW, plant Q within 0.04 Mvar |
| Regression | Base case from a flat start, against an independent Newton-Raphson in numpy | Swing P 4,547.213 MW, losses 342.603 MW, identical |

The target tolerances of level C are 10⁻³ p.u., 0.1°, 1 MW or 1 % and 2 Mvar or 2 % per branch
end (`validation` in `config/base.yaml`).

## Annual load flow

The hourly snapshots differ from the base network only in loads, generation setpoints, voltage
setpoints and transformer taps. `n44.snapshots` stores them as tables and applies one hour at a
time to one base network (`load_snapshot`). Each hour is solved from a flat start, with
fallbacks to a DC start and to PSS/E's voltages that were never needed (script 08).

Branch flows are aggregated into borders between bidding zones, at the metered end as PSS/E
measures area interchange, and into the Swedish transfer sections, defined as flows between
sets of zones (`n44.zones`).

| Cut | From | To |
|---|---|---|
| snitt 1 | SE1 | SE2, SE3, SE4 |
| snitt 2 | SE1, SE2 | SE3, SE4 |
| snitt 4 | SE1, SE2, SE3 | SE4 |

## N-1 security

`n44.contingency` and `n44.acpf`, scripts 11 to 13.

### Contingencies and criteria

Every line and transformer is a contingency, except the eight radial connections of the HVDC
terminal buses (3020, 3360, 5610, 5620, 7010, 7020, 8600, 8700). Losing one of those isolates a
bus and means losing the link's power, which is a frequency question outside the scope of a
load-flow study. That leaves 71 contingencies. The topology is the same in all 8,760
snapshots, so one set of sensitivities serves the whole year.

In the intact network every branch must stay within RATE A. After an outage, a case is
insecure if it has no AC solution, if a bus falls below 0.90 p.u., or if a branch exceeds
RATE B (the short-term emergency rating) while it was within RATE B before the outage. That
last case is an *induced* overload. Overloads already present in the intact network are
reported separately and are not blamed on the outage.

Loading follows PSS/E's convention, terminal current with end shunts included for lines and
MVA for transformers. PowerFactory defines loading differently
([validation log, entry 7](validation_log.md#7-powerfactory-and-psse-measure-branch-loading-differently)).

### Sensitivities

PTDF and LODF come from pandapower's DC model. They are mapped from its internal branch order
to the PSS/E branch keys and oriented from bus I to bus J. In DC, the flow on branch $`m`$
after the outage of branch $`k`$ is

```math
F_{m|k} = F_m + \mathrm{LODF}_{mk}\, F_k .
```

LODF assumes fixed injections, but a line's end shunts trip with it. On line 5101–5501 they
draw +22.3 MW at one end and −21.6 MW at the other, and that change is added with the
post-outage PTDF,

```math
\mathrm{PTDF}^{(k)} = \mathrm{PTDF} + \mathrm{LODF}_{:,k}\, \mathrm{PTDF}_{k,:} .
```

Both corrections are tested against explicit DC outages, to 10⁻⁸ MW.

### Screening

The screening estimates post-contingency flows from each hour's AC flows plus the LODF change
in active power ("AC base, DC delta"), with each end's reactive flow and voltage held at their
pre-contingency values. All 8,760 hours × 71 outages × 79 branches take about five seconds.

### AC contingency analysis

pandapower needs about 0.1 s per contingency on this network, almost all of it converting
tables. `n44.acpf` is a Newton–Raphson solver written on the reference admittance matrix of
`n44.ybus`, which is identical to pandapower's. An outage is a rank-two update of Ybus, so a
line leaves with its end shunts, and reactive limits are applied as pandapower applies them.
The solver handles a contingency in about 0.5 ms, which makes a full AC N-1 of every hour
possible, 622,000 cases.

It is checked twice. pandapower's AC N-1 (with `set_branch_status`) runs on the 100 hours of
highest snitt-2 transfer, and PowerFactory's own contingency analysis (ComSimoutage) on five
critical hours, imported with PowerFactory's PSS/E importer and the exact transformer ratios
([powerfactory/README.md](../powerfactory/README.md)). Post-contingency flows, voltages and
convergence are compared outage by outage.

## Transfer capability of the cuts

Script 12.

The market dispatch is N-1 insecure in almost every hour, so a margin measured from it would
be negative for reasons that have nothing to do with the cut. The margin is therefore measured
from each hour's *secure state*, the market dispatch after the N-1 redispatch of script 15.

The transfer goes between the two zones on either side of the cut (SE1→SE2, SE2→SE3,
SE3→SE4), with generation shift keys proportional to each bus's mean 2015 output. Shifting
generation in zones further away would let their own constraints set the cut's limit.

The margin is the extra transfer the network takes before a critical element reaches its
limit, RATE A in the intact network and RATE B after any outage. Critical elements are those
whose flow changes by at least 5 % of the transfer in that state, as in flow-based capacity
calculation. With $`s_{m|k}`$ the sensitivity of branch $`m`$ to the transfer in state
$`k`$ and $`L_m`$ the active-power limit (the rating reduced by the branch's reactive flow, at
its voltage), the margin is

```math
T^{*} = \min_{(m,k)\ \text{with}\ |s_{m|k}| \ge 0.05} \;
        \frac{L_m - \operatorname{sign}(s_{m|k})\, F_{m|k}}{|s_{m|k}|} .
```

The capability is the cut's flow in the secure state plus the part of $`T^{*}`$ that crosses
the cut, 60 % for snitt 1, 98 % for snitt 2 and 100 % for snitt 4. The rest of a transfer
loops through Norway and Finland.

The capability and the market flow it is compared with are both the mean of the two ends of
each crossing branch, lossless like the DC estimate. The cut flows of P1 and P2 are metered at
one end, as PSS/E meters area interchange, and run 20–50 MW higher in the median hour (half
the crossing branches' losses). Mixing the two would count hours that are above the
capability only by those losses.

DC margins are computed for every hour. As a check, an AC search on the representative hours
walks the transfer up, or down, from the secure state with the reference-model solver.

## DC optimal power flow and nodal prices

`n44.opf`, script 14.

The DC-OPF is a linear program in PTDF form, solved with HiGHS through scipy,

```math
\min_{P} \sum_g c_g P_g
\quad \text{s.t.} \quad
\sum_g P_g = \sum_n D_n, \qquad
\bigl|\mathrm{PTDF}\,(C_g P - D)\bigr| \le F^{\max}, \qquad
P^{\min} \le P \le P^{\max} .
```

The demand $`D`$ holds the loads, the exchange loads (links to neighbours outside the model,
fixed), the line-end shunts and each branch's AC losses split between its ends. Nodal prices
come from the duals. With $`\lambda`$ the dual of the balance and $`\mu^{\pm}`$ those of the
flow limits,

```math
\mathrm{LMP}_n = \lambda + \sum_m \mathrm{PTDF}_{mn}\left(\mu^{+}_m - \mu^{-}_m\right),
```

an energy component (the price at the slack bus) plus a congestion component. The LP matches
pandapower's `rundcopp` to 10⁻⁹ EUR/MWh, and pandapower's AC-OPF (`runopp`) on the
representative hours shows the loss component.

N44 carries no costs, so `config/costs.yaml` gives each generator bus the technology that
dominates the part of the system it stands for, with 2015-typical marginal costs of
10 EUR/MWh for nuclear, 20 EUR/MWh for hydro (as a water value) and 35 EUR/MWh for thermal.
They are assumptions for a structural study, not a price model. Comparing nodal prices with
real 2015 zonal prices needs the ENTSO-E data.

## Redispatch

`n44.redispatch`, script 15.

The starting point is each snapshot's dispatch, which follows the zonal market. The
redispatch is the smallest change that makes it secure. With $`u_g`$ and $`d_g`$ the moves of
unit $`g`$ up and down,

```math
\begin{aligned}
\min \ & \sum_g \left(\rho + w c_g\right) u_g + \left(\rho - w c_g\right) d_g + \pi \sum s \\
\text{s.t.} \ & \sum_g \left(u_g - d_g\right) = 0, \qquad
  0 \le u_g \le P^{\max}_g - P_g, \qquad 0 \le d_g \le P_g - P^{\min}_g, \\
& \Bigl| F_{m|k} + \sum_g \mathrm{PTDF}^{(k)}_{mg} \left(u_g - d_g\right) \Bigr|
  \le L^{(k)}_m + s
\end{aligned}
```

for the intact state ($`k = 0`$, RATE A) and every outage (RATE B). $`F_{m|k}`$ are the hour's
AC flows moved by LODF, and $`L`$ is the limit in active power with reactive flow and voltage
held. Post-contingency constraints are added as they are violated, which converges in a few
rounds. An overload that cannot be removed stays as slack $`s`$ at a penalty
$`\pi`$ = 10,000 EUR/MWh and is reported.

The objective is the volume moved, at a premium $`\rho`$ = 2 EUR/MWh, and costs only break
ties ($`w`$ = 0.02). The cost assumptions do not reproduce the market dispatch, so a
cost-minimising redispatch would also re-optimise the market, moving some 10 GW an hour for
profit rather than for security.

The redispatch is reported as volume, split into an intra-zonal reshuffle and an inter-zonal
countertrade, the net change per zone. The cost range in the results prices the countertrade
at an assumed spread between up- and down-regulation.

As an AC check, 110 redispatched hours are re-checked with a full AC N-1. Where overloads
remain, the program is re-linearised around the solved AC states (each state's AC flow and
limit replace the LODF estimate) and solved again, for up to three rounds.

HVDC links add their set-point as a free variable, and their own trip as one more
contingency. A branch that no unit or link can relieve, such as the radial connection of a
link with a fixed exchange, is left out of the program and its overload is reported apart.

## Scenarios

`n44.scenarios`, script 16.

Each scenario in `config/scenarios/` changes every hourly case before it is solved.

| Scenario | Change |
|---|---|
| `ac_line` | a third Hjälta–Ringhals circuit (3100–3359), a copy of circuit 2, on the corridor where snitt 2 binds in the intact network (P2) |
| `hvdc` | a 1,200 MW VSC link on the same corridor, whose converters hold their bus voltage and whose set-point the redispatch chooses |
| `ac_line_north` | a second Hjälta–Grundfors circuit (3100–3249), at the N-1 bottleneck north of Hjälta (P3, P4) |
| `hvdc_north` | the same VSC link from Grundfors (SE2) to Ringhals (SE3), bypassing the SE1/SE2 lines |
| `north_load` | 1 to 10 GW of new industrial load at Porjus (SE1) and Grundfors (SE2), with the energy produced by the other zones |
| `north_wind` | 5 GW of new load with 4 or 8 GW of new northern wind, as fixed output (no wind profiles until the ENTSO-E data) |

The two `_north` options were added after the first two showed that reinforcing where the
intact network binds barely changes the N-1 redispatch. On a sample of hours, the link from
Grundfors to Ringhals relieved the N-1 redispatch more than links from Grundfors or Porjus to
Forsmark.

Every scenario goes through the same chain on every seventh hour of 2015 (1,252 hours).

1. An AC load flow.
2. The N-0 and N-1 redispatch, with HVDC set-points as controls and the trip of a link as a
   contingency.
3. The N-1 transfer capability of each cut from the redispatched state, as in script 12. A
   link keeps its set-point while the transfer grows, and its trip is one more contingency.
4. A full AC N-1 after the redispatch, on every 13th of those hours (97).

The hours of each variant are split into six chunks, each saved as soon as it is done
(`results/scenarios/parts/`). A new run skips the saved chunks, so an interrupted run resumes
where it stopped. Each worker process takes about 1.2 GB of memory.

Seven is prime to the 24 hours of a day, so the sample covers every hour of the day, and it
falls on every day of the week as well. On the 2015 network it reproduces the annual N-1
redispatch of script 15 to 0.4 %. Every sixth hour, by contrast, would sample only 00, 06, 12
and 18 h and miss the N-0 volume by 9 %.
