"""Optional stage recording for the query pipeline."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


@dataclass(frozen=True)
class Stage:
    """One pipeline step and the SQL it produced."""

    name: str
    description: str
    sql: str
    changed: bool


class Recorder(Protocol):
    def record(self, name: str, description: str, sql: str) -> None: ...


class NullRecorder:
    """Default. Does nothing, allocates nothing."""

    def record(self, name: str, description: str, sql: str) -> None:
        """No-op."""


NULL_RECORDER = NullRecorder()


@dataclass
class StageRecorder:
    """Collects each stage, marking the ones that actually rewrote the SQL.

    ``changed`` is what makes a trace readable, and it is measured against
    ``original`` so the first stage is judged like every other one: the checks
    rewrite nothing, and a query over a public table passes through governance
    untouched. Saying so out loud is the point.
    """

    original: str = ""
    stages: list[Stage] = field(default_factory=list)

    def record(self, name: str, description: str, sql: str) -> None:
        previous = self.stages[-1].sql if self.stages else self.original
        self.stages.append(Stage(name=name, description=description, sql=sql, changed=sql != previous))
