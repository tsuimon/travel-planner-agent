"""Request-scoped wall-clock and conservative token accounting."""

from dataclasses import dataclass, field
from time import monotonic
from src.errors import DeadlineExceeded, TokenLimit


@dataclass
class Budget:
    seconds: float = 30
    token_limit: int = 8000
    used: int = 0
    actual: int = 0
    started: float = field(default_factory=monotonic)

    @property
    def deadline(self) -> float:
        return self.started + self.seconds

    def remaining(self) -> float:
        value = self.deadline - monotonic()
        if value <= 0:
            raise DeadlineExceeded()
        return value

    def reserve(self, prompt: str, completion: int) -> int:
        # UTF-8 byte count is a conservative upper bound for byte-level tokenizers.
        estimate = len(prompt.encode("utf-8")) + 128 + completion
        if self.used + estimate > self.token_limit:
            raise TokenLimit()
        self.used += estimate
        return estimate

    def reconcile(self, reservation: int, actual: int | None) -> None:
        if actual is not None:
            self.actual += actual
            # Replace this request's reservation with provider-reported usage.
            # Unknown/failed requests retain their full reservation.
            self.used += actual - reservation
