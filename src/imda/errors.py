"""Domain errors shared by the domain layer and the API."""

from __future__ import annotations


class InvalidInput(ValueError):
    """The caller supplied a value the domain cannot accept (a client error, HTTP 422).

    A plain ``ValueError`` from internal code is a bug, not a client error, and surfaces as 500.
    """


class RangeTooLarge(InvalidInput):
    """A date range is longer than the allowed span."""

    def __init__(self, days: int, limit: int) -> None:
        self.days = days
        self.limit = limit
        super().__init__(f"Range of {days} days exceeds the {limit}-day limit")
