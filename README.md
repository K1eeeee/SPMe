# PyBaMM W10 3C SPMe aging model

This is an isolated SPMe copy of the W10 aging workflow. The source DFN
workspace at `E:\battery\new` is not modified. Experimental data remains a
read-only input supplied with `--data-root E:\battery\data`; every SPMe output
must remain under `E:\SPMe`.

The model uses PyBaMM 26.7.1, `OKane2022`, SPMe, lumped thermal behavior, SEI,
partially reversible lithium plating, particle cracking and stress-driven loss
of active material. The W10 profile, cell overrides, initial SOC, solver
tolerances, RPT schedule and 350-cycle protocol are inherited unchanged from
the DFN workflow.

## Charge-efficiency schema 3 and checkpoint schema 5

New runs use output schema 3 and checkpoint schema 5. Each ordinary standard
charge writes a full-window record, four fixed reference-SOC bins (20–40,
40–60, 60–80, 80–100%), and an auditable `charge_timeseries/cycle-XXX.csv`.
The efficiency numerator is the negative-particle-lithium inventory delta;
the denominator is the stage-local integral of `max(-Current [A], 0)` across
only `3c_cc`, `4v_cv`, `c4_cc`, and `4p2v_cv`. Charge rest, Step 5, and UDDS
are excluded.

Existing schema-2 output and schema-3/4 checkpoints are intentionally not
appendable/resumable by schema 3/5. Schema-4 checkpoints may be read only by
explicit diagnostic tools; a formal schema-5 run starts at cycle 0 in a new
output directory. Existing artifacts remain untouched until an explicitly
approved archive operation.

### Standard-charge solver resilience without a protocol change

An ordinary standard charge is solved as one continuous four-step PyBaMM
experiment: 3C CC to 4.0 V, 4.0 V CV to 0.05 A, C/4 CC to 4.2 V, and 4.2 V CV
to 0.05 A. The order, currents, voltages, cutoff current, rest, Step 5, UDDS,
RPT schedule, model parameters, and production tolerances are unchanged.

If SUNDIALS reports a classified retryable numerical failure, the code retries
the complete four-step charge once from the unchanged pre-charge snapshot with
a conservative transition profile. Only this second attempt uses a smaller
initial step, a lower BDF order, a larger error-test-failure allowance, and
suppression of algebraic-variable local-error testing. Algebraic equations and
voltage/current constraints are still solved. No partial failed attempt is
committed, and there is no third attempt or physical-cutoff bypass.

Every successful or failed charge writes an append-only record to
`solver_attempts.jsonl`. Heartbeats and failure reports include the attempt,
profile, SUNDIALS error code, failed charge stage, pre-charge state hash, and
nearest validated resume checkpoint.

Run the bounded cycle-1 pre-gate in a new directory before any cycle 0–25
regression:

    C:\Users\Lenovo\anaconda3\envs\battery\python.exe scripts\run_cycle0_25_solver_resilience_regression.py --workspace E:\SPMe --data-root E:\battery\data --calibration-params E:\SPMe\inputs\spme_transferred_parameters.json --output-dir E:\SPMe\tmp\solver-resilience-cycle1-gate-v2 --max-aging-cycles 1

Compare it with the read-only legacy result using the approved strict limits:

    C:\Users\Lenovo\anaconda3\envs\battery\python.exe scripts\compare_cycle_regression.py --old E:\SPMe\outputs\pybamm_spme\w10-350-spme-uncalibrated-v1\cycle_summary.csv --new E:\SPMe\tmp\solver-resilience-cycle1-gate-v2\cycle_summary.csv --output E:\SPMe\tmp\solver-resilience-cycle1-gate-v2\regression_comparison.json --max-cycle 1

The full bounded run uses `--max-aging-cycles 25` and another new output
directory. Do not start it if the pre-gate report is `FAILED`; this bounded
entry point accepts only 1 or 25 cycles and cannot launch the 350-cycle run.

Run the user-authorized cycle-122 capacity validation in a separate output
directory. It starts at cycle 0, stops after cycle 122, and records virtual
RPT capacity at nodes 0, 25, 75, and 122; it cannot launch or resume a
350-cycle run. This bounded trial uses the user-requested legacy charge solver
values (`1e-5 / 1e-7 / 1.0 s`) and is not a strict numerical certification:

    C:\Users\Lenovo\anaconda3\envs\battery\python.exe scripts\run_cycle0_122_capacity_validation.py --workspace E:\SPMe --data-root E:\battery\data --calibration-params E:\SPMe\inputs\spme_transferred_parameters.json --output-dir E:\SPMe\outputs\pybamm_spme\w10-122-stage-local-time-v1-legacy-charge-udds-legacy-general

To resume that same bounded run from one of its validated checkpoints, keep the
same output directory and add the checkpoint owned by that directory:

    C:\Users\Lenovo\anaconda3\envs\battery\python.exe scripts\run_cycle0_122_capacity_validation.py --workspace E:\SPMe --data-root E:\battery\data --calibration-params E:\SPMe\inputs\spme_transferred_parameters.json --output-dir E:\SPMe\outputs\pybamm_spme\w10-122-stage-local-time-v1-legacy-charge-udds-legacy-general --resume-checkpoint E:\SPMe\outputs\pybamm_spme\w10-122-stage-local-time-v1-legacy-charge-udds-legacy-general\checkpoints\cycle-009.pkl

After a completed run, compare its cycle-122 RPT capacity with W10 node 122:

    C:\Users\Lenovo\anaconda3\envs\battery\python.exe scripts\compare_w10_cycle122_capacity.py --run-dir E:\SPMe\outputs\pybamm_spme\w10-122-stage-local-time-v1-legacy-charge-udds-legacy-general --data-root E:\battery\data

Run the isolated real four-stage validation without an aging cycle:

    C:\Users\Lenovo\anaconda3\envs\battery\python.exe scripts\run_pybamm_w10.py --workspace E:\SPMe --data-root E:\battery\data --mode virtual --charge-efficiency-smoke --output-dir E:\SPMe\outputs\pybamm_spme\charge-efficiency-v3-smoke

## Parameter provenance

`inputs/dfn_calibrated_parameters.json` is an unchanged copy of the parameter
artifact used by the active DFN run. It is retained only as provenance.

`inputs/spme_transferred_parameters.json` contains the same numerical scale
factors, targets SPMe, and is explicitly marked `TRANSFERRED_FROM_DFN`. It is
not represented as a new SPMe calibration.

## Verify without running aging cycles

Run these commands from `E:\SPMe`:

    C:\Users\Lenovo\anaconda3\envs\battery\python.exe -m pytest -q -p no:cacheprovider
    C:\Users\Lenovo\anaconda3\envs\battery\python.exe scripts\run_pybamm_w10.py --workspace E:\SPMe --data-root E:\battery\data --prepare

`--prepare` builds and processes the SPMe initial state but does not start an
aging cycle.

## Start the matching 350-cycle SPMe run later

This command is documented only; creating this copy does not execute it:

    C:\Users\Lenovo\anaconda3\envs\battery\python.exe scripts\run_pybamm_w10.py --workspace E:\SPMe --data-root E:\battery\data --mode virtual --verify-repaired-aging --calibration-params E:\SPMe\inputs\spme_transferred_parameters.json --output-dir E:\SPMe\outputs\pybamm_spme\w10-350-transfer-v1

Do not point `--resume` at a DFN checkpoint. SPMe and DFN state vectors are
different and are intentionally isolated by their workspace and output paths.

## Monitor a future SPMe run

    powershell -NoProfile -ExecutionPolicy Bypass -File E:\SPMe\scripts\watch_pybamm_w10.ps1 -OutputDir E:\SPMe\outputs\pybamm_spme\RUN_DIRECTORY -ProcessId PROCESS_ID

## Compare simulated and experimental SOH

Every completed 350-cycle run now evaluates the 15 simulated RPT capacities
against the matching W10 experimental capacity diagnostics. Each curve is
normalised by its own cycle-0 capacity. The run directory receives:

- `soh_comparison.csv`, with aligned capacity, SOH, and node-wise errors;
- `soh_accuracy.json`, with SOH MAE/RMSE, maximum and final errors, capacity
  RMSE, R-squared, and provenance hashes;
- `figures/soh_sim_vs_experiment.png`, with the two SOH curves and a residual
  panel. SOH errors are reported as simulated minus experimental in percentage
  points.

Re-evaluate an existing completed run without repeating the aging simulation:

    C:\Users\Lenovo\anaconda3\envs\battery\python.exe scripts\run_pybamm_w10.py --workspace E:\SPMe --data-root E:\battery\data --evaluate-soh E:\SPMe\outputs\pybamm_spme\RUN_DIRECTORY
