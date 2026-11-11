"""Candidate evidence and decisions; release predicates are unchanged.

Candidate: inputs -> tagged -> validated -> assessing -> assessed -> published.
Assessed -> assessing permits final reassessment; known self-replays are allowed.
Selection: accepted iff compliant, nonregressing and sufficiently improved;
otherwise rejected. Check verdicts pass/fail/warning/skip/error may be reassessed
into any known verdict. Adjudication: not_requested -> pending;
pending -> confirmed/cleared/error/not_requested; confirmed/cleared/error ->
pending for reassessment. Release/hold remains in release_policy.py.
"""

from enum import StrEnum

from app.services.job_transitions import guard_transition


class CandidatePhase(StrEnum):
    INPUTS = "inputs"
    TAGGED = "tagged"
    VALIDATED = "validated"
    ASSESSING = "assessing"
    ASSESSED = "assessed"
    PUBLISHED = "published"


CANDIDATE_TRANSITIONS = {
    CandidatePhase.INPUTS: {CandidatePhase.TAGGED},
    CandidatePhase.TAGGED: {CandidatePhase.VALIDATED},
    CandidatePhase.VALIDATED: {CandidatePhase.ASSESSING},
    CandidatePhase.ASSESSING: {CandidatePhase.ASSESSED},
    CandidatePhase.ASSESSED: {CandidatePhase.ASSESSING, CandidatePhase.PUBLISHED},
    CandidatePhase.PUBLISHED: set(),
}


def candidate_phase(current: CandidatePhase, phase: CandidatePhase) -> CandidatePhase:
    guard_transition(current, phase, CANDIDATE_TRANSITIONS, entity="candidate")
    return phase


def advance_candidate(candidate, phase: CandidatePhase):
    candidate.phase = candidate_phase(candidate.phase, phase)


class CandidateDecision(StrEnum):
    ACCEPTED = "accepted"
    REJECTED = "rejected"


def candidate_decision(
    *, compliant: bool, nonregressing: bool, improvement: bool
) -> CandidateDecision:
    return (
        CandidateDecision.ACCEPTED
        if compliant and nonregressing and improvement
        else CandidateDecision.REJECTED
    )


class CheckVerdict(StrEnum):
    PASS = "pass"
    FAIL = "fail"
    WARNING = "warning"
    SKIP = "skip"
    ERROR = "error"


CHECK_TRANSITIONS = {state: set(CheckVerdict) for state in CheckVerdict}


def set_check_verdict(check, verdict):
    guard_transition(check.get("status", verdict), verdict, CHECK_TRANSITIONS, entity="check")
    check["status"] = verdict


class AdjudicationState(StrEnum):
    NOT_REQUESTED = "not_requested"
    PENDING = "pending"
    CONFIRMED = "confirmed"
    CLEARED = "cleared"
    ERROR = "error"


ADJUDICATION_TRANSITIONS = {
    AdjudicationState.NOT_REQUESTED: {AdjudicationState.PENDING},
    AdjudicationState.PENDING: {
        AdjudicationState.NOT_REQUESTED,
        AdjudicationState.CONFIRMED,
        AdjudicationState.CLEARED,
        AdjudicationState.ERROR,
    },
    AdjudicationState.CONFIRMED: {AdjudicationState.PENDING},
    AdjudicationState.CLEARED: {AdjudicationState.PENDING},
    AdjudicationState.ERROR: {AdjudicationState.PENDING},
}


def set_adjudication_state(report, state):
    current = report.get("adjudication_state", AdjudicationState.NOT_REQUESTED)
    guard_transition(current, state, ADJUDICATION_TRANSITIONS, entity="adjudication")
    report["adjudication_state"] = state
