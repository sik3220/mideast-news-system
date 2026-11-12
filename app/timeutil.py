"""시간 처리. 저장은 UTC, 화면·기간 경계·보고서 기준은 Asia/Baghdad.

시각 정밀도(precision):
  datetime  발표 시각이 확인됨
  observed  수집 서비스가 기사를 관측한 시각(발표 시각이 아님)
  date      날짜만 확인됨 — 임의의 시각을 만들어 붙이지 않는다
  unknown   발표일 미상
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

BAGHDAD = ZoneInfo("Asia/Baghdad")
UTC = timezone.utc


def now_utc() -> datetime:
    return datetime.now(UTC)


def iso(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)


def baghdad_date(dt: datetime) -> str:
    return dt.astimezone(BAGHDAD).strftime("%Y-%m-%d")


def today_baghdad() -> date:
    return now_utc().astimezone(BAGHDAD).date()


def day_bounds_utc(d_from: str, d_to: str) -> tuple[str, str]:
    """바그다드 날짜 구간 [d_from, d_to] 를 UTC 반개구간 [start, end) 로 바꾼다."""
    start = datetime.combine(date.fromisoformat(d_from), time.min, BAGHDAD)
    end = datetime.combine(date.fromisoformat(d_to) + timedelta(days=1), time.min, BAGHDAD)
    return iso(start), iso(end)


def in_period(published_at: str | None, published_date: str | None, precision: str,
              d_from: str, d_to: str) -> bool | None:
    """기간 안이면 True, 밖이면 False, 발표일 미상이면 None."""
    if precision in ("datetime", "observed") and published_at:
        start, end = day_bounds_utc(d_from, d_to)
        return start <= published_at < end
    if precision == "date" and published_date:
        return d_from <= published_date <= d_to
    return None


def display(published_at: str | None, published_date: str | None, precision: str) -> str:
    if precision == "datetime" and published_at:
        return parse_iso(published_at).astimezone(BAGHDAD).strftime("%m.%d %H:%M") + " (바그다드)"
    if precision == "observed" and published_at:
        return parse_iso(published_at).astimezone(BAGHDAD).strftime("%m.%d %H:%M") + " 관측 (발표 시각 아님)"
    if precision == "date" and published_date:
        return published_date[5:].replace("-", ".") + " (시각 미상)"
    return "발표일 미상"


def md(date_str: str) -> str:
    """'2026-11-09' -> '11.9' (원본 양식의 날짜 표기)."""
    d = date.fromisoformat(date_str)
    return f"{d.month}.{d.day}"
