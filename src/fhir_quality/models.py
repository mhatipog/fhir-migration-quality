"""Small, privacy-conscious public result types."""
from __future__ import annotations
from dataclasses import asdict, dataclass, field
from typing import Literal

Severity = Literal["error", "warning", "information"]


@dataclass(frozen=True, slots=True)
class Issue:
    code: str
    severity: Severity
    path: str
    message: str
    # Never put patient names, identifiers, or input field values in messages.

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass(slots=True)
class Summary:
    total: int = 0
    accepted: int = 0
    quarantined: int = 0
    by_resource_type: dict[str, int] = field(default_factory=dict)
    by_issue_code: dict[str, int] = field(default_factory=dict)
    warnings: int = 0

    @property
    def pass_rate(self) -> float:
        return (self.accepted / self.total) if self.total else 1.0

    def as_dict(self) -> dict:
        return {**asdict(self), "pass_rate": round(self.pass_rate, 6),
                "scope": "FHIR R4 pipeline rules; not comprehensive FHIR conformance"}
