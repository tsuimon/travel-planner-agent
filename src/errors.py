"""Domain-specific failures, safe to describe without leaking credentials."""


class PlannerError(Exception):
    """Base expected application failure."""


class DeadlineExceeded(PlannerError):
    """The request wall-clock budget is exhausted."""


class TokenLimit(PlannerError):
    """No room remains for a bounded model call."""


class NeedsClarification(PlannerError):
    """An essential constraint cannot be safely inferred."""


class AmbiguousArrivalTime(NeedsClarification):
    """An arrival clock must be clarified before any model can reinterpret it."""


class ProviderUnavailable(PlannerError):
    """No configured or usable authorized data provider."""
