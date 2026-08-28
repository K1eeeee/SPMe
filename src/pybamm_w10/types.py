"""Small explicit result types shared by protocol, diagnostics, and output."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any, Mapping
import re


class RunStatus(StrEnum):
    COMPLETED = "COMPLETED"
    PAUSED = "PAUSED"
    PHYSICAL_PROTOCOL_FAILURE = "PHYSICAL_PROTOCOL_FAILURE"
    NUMERICAL_FAILURE = "NUMERICAL_FAILURE"


class ProtocolPhase(StrEnum):
    INITIAL_RPT = "INITIAL_RPT"
    STANDARD_CHARGE = "STANDARD_CHARGE"
    RPT_PRECONDITIONING = "RPT_PRECONDITIONING"
    RPT_CAPACITY_DISCHARGE = "RPT_CAPACITY_DISCHARGE"
    POST_RPT_RECOVERY = "POST_RPT_RECOVERY"
    STEP5_C4_DISCHARGE = "STEP5_C4_DISCHARGE"
    STEP6_UDDS = "STEP6_UDDS"
    CYCLE_COMPLETED = "CYCLE_COMPLETED"
    RUN_COMPLETED = "RUN_COMPLETED"


class TerminationKind(StrEnum):
    CAPACITY = "CAPACITY"
    VOLTAGE = "VOLTAGE"
    CURRENT = "CURRENT"
    FINAL_TIME = "FINAL_TIME"
    MODEL_PHYSICAL_EVENT = "MODEL_PHYSICAL_EVENT"
    UNKNOWN = "UNKNOWN"


class FailureReason(StrEnum):
    PHYSICAL_EVENT_BEFORE_TARGET = "PHYSICAL_EVENT_BEFORE_TARGET"
    UNEXPECTED_FINAL_TIME = "UNEXPECTED_FINAL_TIME"
    UNKNOWN_TERMINATION = "UNKNOWN_TERMINATION"
    SOLVER_FAILURE = "SOLVER_FAILURE"
    INVALID_STATE = "INVALID_STATE"
    CAPACITY_TOLERANCE_FAILURE = "CAPACITY_TOLERANCE_FAILURE"
    OUTPUT_FAILURE = "OUTPUT_FAILURE"
    MISSING_MODEL_VARIABLE = "MISSING_MODEL_VARIABLE"


class ChargeEfficiencyStatus(StrEnum):
    VALID = "VALID"
    BALANCE_WARNING = "BALANCE_WARNING"
    CHARGE_BALANCE_FAILURE = "CHARGE_BALANCE_FAILURE"
    PREEXISTING_PLATED_LITHIUM_RELEASED = "PREEXISTING_PLATED_LITHIUM_RELEASED"
    SOC_ANCHOR_INVALID = "SOC_ANCHOR_INVALID"
    SOC_UPPER_BOUND_NOT_REACHED = "SOC_UPPER_BOUND_NOT_REACHED"
    NON_MONOTONIC_SOC = "NON_MONOTONIC_SOC"
    INVALID_EXTERNAL_CHARGE = "INVALID_EXTERNAL_CHARGE"
    INVALID_INTERCALATED_CHARGE = "INVALID_INTERCALATED_CHARGE"
    MISSING_MODEL_VARIABLE = "MISSING_MODEL_VARIABLE"
    PLATING_INVENTORY_CROSSCHECK_FAILURE = "PLATING_INVENTORY_CROSSCHECK_FAILURE"
    STANDARD_CHARGE_SKIPPED_AFTER_RPT = "STANDARD_CHARGE_SKIPPED_AFTER_RPT"
    CHARGE_EFFICIENCY_CORE_FAILURE = "CHARGE_EFFICIENCY_CORE_FAILURE"


@dataclass(frozen=True)
class ChargeEfficiencyAssessment:
    primary_status: ChargeEfficiencyStatus
    status_flags: tuple[ChargeEfficiencyStatus, ...]
    is_valid_for_efficiency_analysis: bool
    is_valid_for_mechanism_analysis: bool


@dataclass(frozen=True)
class ChargeIntegrationResult:
    external_charge_ah: float
    cc_charge_ah: float
    cv_charge_ah: float


@dataclass(frozen=True)
class ChargeBalanceResult:
    external_charge_ah: float
    intercalated_charge_increment_ah: float
    reversible_plating_increment_ah: float
    reversible_plating_depletion_ah: float
    dead_lithium_increment_ah: float
    sei_increment_ah: float
    useful_charge_efficiency_pct: float | None
    reversible_retention_pct: float | None
    accounted_charge_ah: float | None
    charge_balance_error_ah: float | None
    charge_balance_error_pct: float | None
    charge_balance_abs_error_pct: float | None


@dataclass(frozen=True)
class MergedChargeTrace:
    stage_names: tuple[str, ...]
    time_s: tuple[float, ...]
    current_a: tuple[float, ...]


@dataclass(frozen=True)
class SocBoundaryCrossing:
    target_soc_pct: float
    time_s: float | None
    crossing_count: int
    selection_rule: str = "FIRST_UPWARD_CROSSING"


@dataclass(frozen=True)
class ChargeStateSnapshot:
    time_s: float
    values: Mapping[str, float]
    state_hash: str


@dataclass(frozen=True)
class ChargeStageTrace:
    stage_name: str
    time_s: tuple[float, ...]
    values: Mapping[str, tuple[float, ...]]
    global_time_offset_s: float = 0.0


@dataclass(frozen=True)
class ChargeStageMeasurement:
    stage_name: str
    time_start_s: float
    time_end_s: float
    duration_s: float
    external_charge_ah: float


@dataclass(frozen=True)
class ChargeTraceArtifact:
    relative_path: str
    sha256: str
    row_count: int
    start_time_s: float
    end_time_s: float


@dataclass(frozen=True)
class ChargeEfficiencySummary:
    cycle: int
    mode: str
    values: Mapping[str, Any]


@dataclass(frozen=True)
class ChargeSocBinResult:
    cycle: int
    mode: str
    soc_bin_id: str
    values: Mapping[str, Any]


@dataclass(frozen=True)
class ChargeAnalysisBundle:
    summary: ChargeEfficiencySummary
    soc_bins: tuple[ChargeSocBinResult, ...]
    status: ChargeEfficiencyAssessment
    trace_rows: tuple[Mapping[str, Any], ...] = ()
    trace_artifact: ChargeTraceArtifact | None = None


@dataclass(frozen=True)
class StageSpec:
    phase: ProtocolPhase
    expected_termination: TerminationKind
    allowed_physical_terminations: tuple[TerminationKind, ...] = ()
    expected_event_names: tuple[str, ...] = ()
    allowed_physical_event_names: tuple[str, ...] = ()


@dataclass(frozen=True)
class StageOutcome:
    termination_kind: TerminationKind
    raw_termination: str | None
    termination_time_s: float | None
    termination_value: float | None
    terminal_voltage_v: float | None
    terminal_temperature_k: float | None
    terminal_discharge_capacity_ah: float | None
    state_hash: str | None


@dataclass(frozen=True)
class SolverProfile:
    name: str
    rtol: float
    atol: float
    dt_init_s: float
    max_step_s: float
    max_num_steps: int
    max_error_test_failures: int
    max_order_bdf: int
    suppress_algebraic_error: bool


RETRYABLE_SUNDIALS_ERRORS = frozenset({"IDA_ERR_FAIL", "IDA_CONV_FAIL", "IDA_LSETUP_FAIL"})


def classify_sundials_error(message: str) -> tuple[str | None, bool]:
    """Return one normalized SUNDIALS code and its retry eligibility."""
    match = re.search(r"\b(IDA_[A-Z0-9_]+)\b", str(message))
    code = None if match is None else match.group(1)
    return code, code in RETRYABLE_SUNDIALS_ERRORS


class SolverStepFailure(RuntimeError):
    """A structured solver failure tied to one standard-charge step."""

    def __init__(
        self,
        message: str,
        *,
        sundials_error_code: str | None,
        failed_step_index: int,
        charge_stage: str,
        retryable: bool,
        original_exception: BaseException | None = None,
        attempt_failures: tuple["SolverAttemptFailure", ...] = (),
    ) -> None:
        self.raw_message = str(message)
        self.sundials_error_code = sundials_error_code
        self.failed_step_index = failed_step_index
        self.charge_stage = charge_stage
        self.retryable = retryable
        self.original_exception = original_exception
        self.attempt_failures = attempt_failures
        super().__init__(self.raw_message)

    @classmethod
    def from_exception(
        cls, exc: BaseException, failed_step_index: int, charge_stage: str
    ) -> "SolverStepFailure":
        code, retryable = classify_sundials_error(str(exc))
        return cls(
            str(exc),
            sundials_error_code=code,
            failed_step_index=failed_step_index,
            charge_stage=charge_stage,
            retryable=retryable,
            original_exception=exc,
        )


@dataclass(frozen=True)
class SolverAttemptFailure:
    attempt: int
    solver_profile: str
    sundials_error_code: str | None
    failed_step_index: int
    charge_stage: str
    message: str


@dataclass(frozen=True)
class StandardChargeSequenceResult:
    outcomes: tuple[StageOutcome, ...]
    traces: tuple[ChargeStageTrace, ...]
    stage_durations_s: Mapping[str, float]
    stage_wall_clock_durations_s: Mapping[str, float]
    terminal_snapshot: Any
    attempt_count: int
    solver_profile: str
    initial_failure_code: str | None = None
    attempt_failures: tuple[SolverAttemptFailure, ...] = ()
    pre_charge_state_hash: str | None = None


@dataclass(frozen=True)
class FailureContext:
    reason: FailureReason
    run_id: str | None = None
    mode: str | None = None
    cycle: int | None = None
    rpt_node: int | None = None
    phase: ProtocolPhase | None = None
    q_ref_ah: float | None = None
    q_ref_node: int | None = None
    q_window_start_ah: float | None = None
    step5_target_ah: float | None = None
    step5_actual_ah: float | None = None
    step5_relative_error: float | None = None
    window_target_ah: float | None = None
    window_actual_ah: float | None = None
    window_relative_error: float | None = None
    planned_udds_remaining_ah: float | None = None
    actual_udds_remaining_target_ah: float | None = None
    udds_guard_ah: float | None = None
    udds_profile_available_ah: float | None = None
    udds_actual_ah: float | None = None
    termination_kind: TerminationKind | None = None
    raw_termination: str | None = None
    termination_time_s: float | None = None
    termination_value: float | None = None
    terminal_voltage_v: float | None = None
    terminal_temperature_k: float | None = None
    terminal_discharge_capacity_ah: float | None = None
    state_hash: str | None = None
    last_checkpoint: str | None = None
    result_transaction: int | None = None
    completed_cycles: int | None = None
    exception_type: str | None = None
    message: str | None = None
    traceback: str | None = None
    charge_stage: str | None = None
    soc_bin_id: str | None = None
    charge_efficiency_status: str | None = None
    charge_trace_temp_path: str | None = None
    forensic_snapshot_path: str | None = None
    resume_eligible: bool = False
    failed_step_index: int | None = None
    solver_attempt: int | None = None
    solver_profile: str | None = None
    sundials_error_code: str | None = None
    pre_charge_state_hash: str | None = None
    last_successful_stage: str | None = None
    last_committed_checkpoint: str | None = None
    resume_checkpoint: str | None = None
    attempt_failures: tuple[Mapping[str, Any], ...] = ()

    def to_json(self) -> dict[str, Any]:
        value = asdict(self)
        for key, item in value.items():
            if isinstance(item, StrEnum):
                value[key] = item.value
        return value


class PhysicalProtocolFailure(RuntimeError):
    """A valid PyBaMM physical boundary prevented completion of the protocol."""

    def __init__(self, context: FailureContext | str | None = None) -> None:
        if not isinstance(context, FailureContext):
            context = FailureContext(
                reason=FailureReason.PHYSICAL_EVENT_BEFORE_TARGET,
                message=context,
            )
        self.context = context
        super().__init__(context.message or context.reason.value)


class NumericalFailure(RuntimeError):
    """A solver, state-vector, or output problem prevented a valid result."""

    def __init__(self, context: FailureContext | str | None = None) -> None:
        if not isinstance(context, FailureContext):
            context = FailureContext(reason=FailureReason.SOLVER_FAILURE, message=context)
        self.context = context
        super().__init__(context.message or context.reason.value)


@dataclass(frozen=True)
class CapacityTargets:
    q_ref_ah: float
    step5_target_ah: float
    window_target_ah: float
    udds_remaining_ah: float


@dataclass(frozen=True)
class DriveWindowPlan:
    event_target_ah: float
    remaining_ah: float
    guard_ah: float
    profile_available_ah: float
    profile: Any = field(repr=False)
    event_time_s: float = 0.0
    profile_fingerprint: str = ""


@dataclass
class RPTResult:
    node: int
    q_rpt_start_ah: float
    q_rpt_end_ah: float
    capacity_ah: float
    soh_initial_pct: float | None
    soh_nominal_pct: float
    mode: str
    start_time_s: float
    end_time_s: float
    diagnostic_duration_s: float
    changed_main_state: bool
    main_state_hash_before: str
    main_state_hash_after: str
    main_time_before_s: float
    main_time_after_s: float
    main_capacity_before_ah: float
    main_capacity_after_ah: float
    became_q_ref: bool
    targets: CapacityTargets | None = None
    timeseries: dict[str, Any] = field(default_factory=dict, repr=False)

    @property
    def next_step5_target_ah(self) -> float | None:
        return None if self.targets is None else self.targets.step5_target_ah

    @property
    def next_window_target_ah(self) -> float | None:
        return None if self.targets is None else self.targets.window_target_ah

    @property
    def planned_udds_remaining_ah(self) -> float | None:
        if self.next_step5_target_ah is None or self.next_window_target_ah is None:
            return None
        return self.next_window_target_ah - self.next_step5_target_ah


@dataclass
class CycleResult:
    cycle: int
    mode: str
    q_ref_ah: float
    q_ref_node: int
    step5_target_ah: float
    window_target_ah: float
    delta_q5_actual_ah: float
    actual_udds_remaining_target_ah: float
    udds_profile_available_ah: float
    udds_guard_ah: float
    udds_actual_ah: float
    window_actual_ah: float
    start_time_s: float
    end_time_s: float
    stage_durations_s: dict[str, float] = field(default_factory=dict)
    stage_wall_clock_durations_s: dict[str, float] = field(default_factory=dict)
    termination_event: str = "W10 capacity window"
    termination_time_s: float | None = None
    termination_value: float | None = None
    termination_classification: str = "EXPECTED_PROTOCOL_EVENT"
    metrics: dict[str, float] = field(default_factory=dict)
    configured_nominal_charge_rate_c: float = 3.0
    effective_charge_rate_c: float | None = None
    useful_charge_efficiency_pct: float | None = None
    reversible_retention_pct: float | None = None
    charge_efficiency_status: str = ""
    complete_soc_bin_count: int = 0
    charge_analysis: ChargeAnalysisBundle | None = field(default=None, repr=False)
    solver_attempt_count: int = 1
    solver_profile: str = "general_protocol"
    initial_solver_failure_code: str | None = None
    solver_attempt_failures: tuple[SolverAttemptFailure, ...] = field(default=(), repr=False)


@dataclass(frozen=True)
class AppendFileCommit:
    relative_path: str
    byte_offset: int
    data_rows: int
    prefix_sha256: str


@dataclass(frozen=True)
class ArtifactCommit:
    relative_path: str
    size: int
    sha256: str


@dataclass(frozen=True)
class OutputCommitManifest:
    transaction: int
    append_files: dict[str, AppendFileCommit]
    artifacts: dict[str, ArtifactCommit]
    last_completed_cycle: int
    last_rpt_node: int | None
    last_charge_efficiency_cycle: int | None = None
    last_complete_soc_bin_cycle: int | None = None


@dataclass(frozen=True)
class InitialStateRecord:
    initial_soc: float
    method: str
    method_arguments: dict[str, Any]
    state_size: int
    state_sha256: str
    model_sha256: str
    parameter_sha256: str
    mesh_sha256: str
    main_time_s: float
    calendar_time_s: float
    discharge_capacity_ah: float
    negative_stoichiometry: float
    positive_stoichiometry: float
    versions: dict[str, str]

    @property
    def fingerprint(self) -> str:
        import hashlib
        import json

        return hashlib.sha256(
            json.dumps(self.__dict__, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()


@dataclass
class Checkpoint:
    schema_version: int
    state: Any
    aging_cycle: int
    main_time_s: float
    mode: str
    q_ref_ah: float | None
    q_ref_node: int | None
    initial_capacity_ah: float | None
    protocol_phase: ProtocolPhase
    capacity_targets: CapacityTargets | None
    config_fingerprint: str
    input_fingerprint: str
    udds_fingerprint: str
    initial_state_fingerprint: str
    environment_fingerprint: str
    result_transaction: int
    output_manifest: OutputCommitManifest
    protocol_algorithm_version: str = ""
    output_schema_version: int = 0
    guard_config_fingerprint: str = ""
    last_successful_boundary: str | None = None
    last_successful_stage: ProtocolPhase | None = None
    effective_parameters_fingerprint: str = ""
    charge_efficiency_algorithm_version: str = ""
    charge_efficiency_variable_inventory_sha256: str = ""
    last_charge_efficiency_cycle: int | None = None
    last_complete_soc_bin_cycle: int | None = None
    solver_execution_version: str = ""
    run_context_fingerprint: str = ""
