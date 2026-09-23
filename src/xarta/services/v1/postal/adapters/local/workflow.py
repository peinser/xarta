from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class PrintJobKind(StrEnum):
    LETTER = "letter"


class PrintJobState(StrEnum):
    PENDING = "pending"
    SUBMITTING = "submitting"
    PRINTING = "printing"
    COMPLETED = "completed"
    UNCERTAIN = "uncertain"
    FAILED = "failed"


class PhysicalState(StrEnum):
    WAITING_FOR_PRINT = "waiting_for_print"
    READY_FOR_PROCESSING = "ready_for_processing"
    PROCESSING = "processing"
    READY_FOR_HANDOVER = "ready_for_handover"
    HANDED_OVER = "handed_over"


class ScanMode(StrEnum):
    START_PRODUCTION = "start_production"
    READY_FOR_HANDOVER = "ready_for_handover"
    CONFIRM_HANDOVER = "confirm_handover"


class PrintAttemptEvent(StrEnum):
    SUBMIT = "submit"
    ACCEPTED = "accepted"
    COMPLETED = "completed"
    FAILED = "failed"
    RESULT_UNKNOWN = "result_unknown"


@dataclass(frozen=True, slots=True)
class PrintJob:
    id: str
    run_id: str
    sequence: int
    kind: PrintJobKind
    generation: int
    state: PrintJobState = PrintJobState.PENDING

    def __post_init__(self) -> None:
        if not self.id or not self.run_id:
            raise ValueError("Print job and run IDs must be non-empty")
        if any(
            character
            not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_"
            for character in self.run_id
        ):
            raise ValueError(
                "Print run IDs may contain only letters, digits, hyphens, and underscores"
            )
        if self.sequence < 1 or self.generation < 1:
            raise ValueError("Print job sequence and generation must be positive")


@dataclass(frozen=True, slots=True)
class PrintAttempt:
    id: str
    job_id: str
    attempt: int
    state: PrintJobState

    def __post_init__(self) -> None:
        if not self.id or not self.job_id:
            raise ValueError("Print attempt and job IDs must be non-empty")
        if self.attempt < 1:
            raise ValueError("Print attempt number must be positive")


@dataclass(frozen=True, slots=True)
class ScanReduction:
    state: PhysicalState
    outcomes: tuple[str, ...]
    duplicate: bool = False


def printer_job_name(job: PrintJob, attempt: int) -> str:
    if attempt < 1:
        raise ValueError("Print attempt number must be positive")
    return f"postal/{job.run_id}/{job.sequence:06d}/{job.kind.value}/{job.generation}/{attempt}"


def reduce_print_state(state: PrintJobState, event: PrintAttemptEvent) -> PrintJobState:
    transitions = {
        (PrintJobState.PENDING, PrintAttemptEvent.SUBMIT): PrintJobState.SUBMITTING,
        (PrintJobState.SUBMITTING, PrintAttemptEvent.ACCEPTED): PrintJobState.PRINTING,
        (PrintJobState.SUBMITTING, PrintAttemptEvent.FAILED): PrintJobState.FAILED,
        (
            PrintJobState.SUBMITTING,
            PrintAttemptEvent.RESULT_UNKNOWN,
        ): PrintJobState.UNCERTAIN,
        (PrintJobState.PRINTING, PrintAttemptEvent.COMPLETED): PrintJobState.COMPLETED,
        (PrintJobState.PRINTING, PrintAttemptEvent.FAILED): PrintJobState.FAILED,
        (
            PrintJobState.PRINTING,
            PrintAttemptEvent.RESULT_UNKNOWN,
        ): PrintJobState.UNCERTAIN,
    }
    try:
        return transitions[(state, event)]
    except KeyError as ex:
        raise ValueError(
            f"Print event {event.value} is invalid while job is {state.value}"
        ) from ex


def reduce_scan(
    state: PhysicalState,
    mode: ScanMode,
    *,
    already_recorded: bool = False,
    handover_batch_matches: bool = True,
    generation: int = 1,
    current_generation: int = 1,
) -> ScanReduction:
    transitions = {
        (PhysicalState.READY_FOR_PROCESSING, ScanMode.START_PRODUCTION): (
            PhysicalState.PROCESSING,
            (),
        ),
        (PhysicalState.PROCESSING, ScanMode.READY_FOR_HANDOVER): (
            PhysicalState.READY_FOR_HANDOVER,
            ("prepared",),
        ),
        (PhysicalState.READY_FOR_HANDOVER, ScanMode.CONFIRM_HANDOVER): (
            PhysicalState.HANDED_OVER,
            ("handed_over",),
        ),
    }
    if already_recorded:
        return ScanReduction(state, (), duplicate=True)
    if generation < 1 or current_generation < 1 or generation != current_generation:
        raise ValueError("Scan generation does not match the task's current generation")
    if mode is ScanMode.CONFIRM_HANDOVER and not handover_batch_matches:
        raise ValueError("Handover scan does not match the task's handover batch")
    try:
        next_state, outcomes = transitions[(state, mode)]
    except KeyError as ex:
        raise ValueError(
            f"Scan mode {mode.value} is invalid while task is {state.value}"
        ) from ex
    return ScanReduction(next_state, outcomes)
