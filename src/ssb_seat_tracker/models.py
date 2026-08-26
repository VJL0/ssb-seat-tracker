"""Small immutable models for CRN enrollment tracking."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True, slots=True)
class EnrollmentInfo:
    enrollment: int
    capacity: int
    seats_available: int
    waitlist_capacity: int
    waitlist_count: int
    waitlist_available: int

    @property
    def is_open(self) -> bool:
        return self.seats_available > 0


@dataclass(frozen=True, slots=True)
class TrackedSection:
    crn: str
    enabled: bool = True
    seats_available: int | None = None
    updated_at: datetime | None = None

    def __post_init__(self) -> None:
        if not self.crn.isdigit():
            raise ValueError("CRN must contain only digits")
        if self.updated_at is not None and (
            self.updated_at.tzinfo is None or self.updated_at.utcoffset() is None
        ):
            raise ValueError("updated_at must include a timezone")
