from __future__ import annotations
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from app.errors import DomainError


def parse_iso(value: str) -> datetime:
    """解析为带时区的 datetime；Z 视作 UTC。"""
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def normalize_utc(value: str, zone: str | None = None, fold: int = 0) -> tuple[str, int]:
    """把事件时间归一到 UTC，返回 (UTC ISO 字符串, fold)。

    带偏移的时间直接换算；朴素时间必须给 zone，DST 间隙（spring forward 不存在的
    墙上时间）拒绝，DST 重叠（fall back 折叠小时）按 fold 区分两个瞬间。
    """
    dt = parse_iso(value)
    used_fold = int(fold)
    if dt.tzinfo is None:
        if not zone:
            raise DomainError("TIMEZONE_REQUIRED", "朴素时间缺少时区")
        tz = ZoneInfo(zone)
        dt = dt.replace(tzinfo=tz, fold=used_fold)
        # 往返校验：间隙时间换算回来墙上时间会错位
        back = dt.astimezone(timezone.utc).astimezone(tz)
        if back.replace(tzinfo=None) != parse_iso(value):
            raise DomainError("DST_GAP", "DST 间隙内的墙上时间不存在")
    utc_dt = dt.astimezone(timezone.utc)
    return utc_dt.isoformat().replace("+00:00", "Z"), used_fold


def hour_key(utc_ts: str) -> str:
    """UTC 小时桶，左闭右开，输出形如 2026-11-01T05:00:00Z。"""
    dt = parse_iso(utc_ts).astimezone(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:00:00Z")


def advance_watermark(current: str | None, observed: list[str], minutes: int = 15) -> dict:
    """水位只能前进：new = max(old, max(观测时间) - 窗口)。无历史时以提议值初始化。"""
    parsed = [parse_iso(x).astimezone(timezone.utc) for x in observed]
    if current is None:
        if not parsed:
            raise DomainError("WATERMARK_REQUIRED", "首次推进需要观测事件")
        new = max(parsed) - timedelta(minutes=minutes)
        return {"old": None, "new": new.isoformat().replace("+00:00", "Z"),
                "advanced": True}
    old = parse_iso(current).astimezone(timezone.utc)
    newest = max([old, *parsed])
    proposed = newest - timedelta(minutes=minutes)
    new = max(old, proposed)
    return {"old": old.isoformat().replace("+00:00", "Z"),
            "new": new.isoformat().replace("+00:00", "Z"),
            "advanced": new > old}
