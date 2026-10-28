"""Finite local-wall-time recurrence. Month ends clamp from the original day.

Ambiguous fall-back times choose fold=0; nonexistent spring-forward times shift
forward by the DST gap. Duration is elapsed time, preserving a positive interval.
"""

from calendar import monthrange
from datetime import datetime, timedelta, timezone
from typing import Literal
from zoneinfo import ZoneInfo
from pydantic import BaseModel, ConfigDict, Field, model_validator

MAX_OCCURRENCES = 366


class Recurrence(BaseModel):
    model_config = ConfigDict(extra="forbid")
    frequency: Literal["DAILY", "WEEKLY", "MONTHLY"]
    interval: int = Field(default=1, ge=1, le=52)
    count: int | None = Field(default=None, ge=1, le=MAX_OCCURRENCES)
    until: datetime | None = None

    @model_validator(mode="after")
    def bounded(self):
        if (self.count is None) == (self.until is None):
            raise ValueError("Specify exactly one of count or until")
        if self.until is not None and self.until.tzinfo is None:
            raise ValueError("Recurrence until requires a UTC offset")
        return self


def occurrences(start, end, zone, rule):
    tz = ZoneInfo(zone)
    origin = start.astimezone(tz).replace(tzinfo=None)
    duration = end.astimezone(timezone.utc) - start.astimezone(timezone.utc)
    if rule is None:
        return [(start, end)]
    if rule.until is not None and rule.until < start:
        raise ValueError("Recurrence until must include the first occurrence")
    result = []
    for index in range(MAX_OCCURRENCES + 1):
        step = index * rule.interval
        if rule.frequency == "MONTHLY":
            month = origin.year * 12 + origin.month - 1 + step
            year, month = divmod(month, 12)
            month += 1
            if year > 9999:
                if rule.until is not None:
                    break
                raise ValueError("Recurrence extends beyond supported calendar dates")
            naive = origin.replace(
                year=year, month=month, day=min(origin.day, monthrange(year, month)[1])
            )
        else:
            try:
                naive = origin + timedelta(
                    days=step * (7 if rule.frequency == "WEEKLY" else 1)
                )
            except OverflowError:
                if rule.until is not None:
                    break
                raise ValueError(
                    "Recurrence extends beyond supported calendar dates"
                ) from None
        local = naive.replace(tzinfo=tz, fold=0)
        # Round-trip normalizes a nonexistent local time forward across the gap.
        local = local.astimezone(timezone.utc).astimezone(tz)
        if rule.until is not None and local > rule.until:
            break
        if rule.count is not None and index >= rule.count:
            break
        if index == MAX_OCCURRENCES:
            raise ValueError("Recurrence exceeds 366 occurrences; shorten until")
        try:
            finish = (local.astimezone(timezone.utc) + duration).astimezone(tz)
        except OverflowError:
            raise ValueError(
                "Recurrence end exceeds supported calendar dates"
            ) from None
        result.append((local, finish))
    return result
