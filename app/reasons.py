REASONS = {
    "BAD_COLUMNS": "字段数量不符", "EMPTY_VALUE": "必要值为空",
    "DUPLICATE_CONFLICT": "同一业务键取值冲突", "TRUNCATED": "尾记录不完整",
    "INVALID_VALUE": "值不符合定义", "STALE_VERSION": "提交版本已过期",
    "TIMEZONE_REQUIRED": "朴素时间缺少时区", "DST_GAP": "DST 间隙内的墙上时间不存在",
    "WATERMARK_REQUIRED": "签收前必须先推进水位",
    "WATERMARK_REGRESSION": "水位不允许倒退",
    "CONSERVATION_BROKEN": "桶合计与权威事件数不一致",
    "VERSION_NOT_FOUND": "指定的签收版本不存在",
}

def reason(code: str) -> str:
    return REASONS.get(code, "未登记原因")
