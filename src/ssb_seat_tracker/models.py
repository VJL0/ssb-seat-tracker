"""Domain models and Temple-specific seat availability rules."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, computed_field, field_validator


class BannerModel(BaseModel):
    """Base configuration for reverse-engineered Banner JSON payloads."""

    model_config = ConfigDict(populate_by_name=True, extra="ignore", str_strip_whitespace=True)


class Term(BannerModel):
    code: str
    description: str


class Section(BannerModel):
    course_reference_number: str = Field(alias="courseReferenceNumber")
    subject: str
    course_number: str = Field(alias="courseNumber")
    sequence_number: str = Field(alias="sequenceNumber")
    course_title: str = Field(alias="courseTitle")
    campus_description: str = Field(alias="campusDescription")

    maximum_enrollment: int = Field(alias="maximumEnrollment", ge=0)
    enrollment: int = Field(ge=0)
    seats_available: int = Field(alias="seatsAvailable")

    wait_capacity: int = Field(alias="waitCapacity", ge=0)
    wait_count: int = Field(alias="waitCount", ge=0)
    wait_available: int = Field(alias="waitAvailable", ge=0)

    open_section: bool = Field(alias="openSection")

    cross_list: str | None = Field(default=None, alias="crossList")
    cross_list_capacity: int | None = Field(default=None, alias="crossListCapacity", ge=0)
    cross_list_count: int | None = Field(default=None, alias="crossListCount", ge=0)

    @computed_field
    @property
    def effective_seats(self) -> int:
        """Conservative estimate after accounting for students already waitlisted."""

        return max(self.seats_available - self.wait_count, 0)

    @computed_field
    @property
    def is_available(self) -> bool:
        """Whether Banner reports a registerable opening."""

        return self.open_section and self.effective_seats > 0


class Availability(BannerModel):
    crn: str
    available: bool
    effective_seats: int = Field(alias="effectiveSeats")
    checked_at: datetime = Field(alias="checkedAt")

    @field_validator("checked_at")
    @classmethod
    def validate_aware_datetime(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("checkedAt must include a timezone")
        return value

    @classmethod
    def from_section(cls, section: Section, *, checked_at: datetime) -> Availability:
        return cls(
            crn=section.course_reference_number,
            available=section.is_available,
            effective_seats=section.effective_seats,
            checked_at=checked_at,
        )


class Watch(BaseModel):
    """One configured CRN and its last successful observation."""

    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    crn: str
    term: str
    subject: str
    course_number: str
    enabled: bool = True
    available: bool | None = None
    effective_seats: int | None = Field(default=None, ge=0)
    last_checked_at: datetime | None = None

    @field_validator("crn")
    @classmethod
    def validate_crn(cls, value: str) -> str:
        if not value.isdigit():
            raise ValueError("CRN must contain only digits")
        return value

    @field_validator("term", "subject", "course_number")
    @classmethod
    def validate_nonempty(cls, value: str) -> str:
        if not value:
            raise ValueError("watch fields must not be empty")
        return value

    @field_validator("subject")
    @classmethod
    def normalize_subject(cls, value: str) -> str:
        return value.upper()

    @field_validator("last_checked_at")
    @classmethod
    def validate_aware_last_checked_at(cls, value: datetime | None) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("last_checked_at must include a timezone")
        return value

    def previous_availability(self) -> Availability | None:
        """Return state only after a complete successful observation exists."""

        if self.available is None or self.effective_seats is None or self.last_checked_at is None:
            return None
        return Availability(
            crn=self.crn,
            available=self.available,
            effective_seats=self.effective_seats,
            checked_at=self.last_checked_at,
        )
