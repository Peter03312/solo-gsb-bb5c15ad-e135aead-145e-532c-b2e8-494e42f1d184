"""历史投递判定核对报表。

按带时区的起止时刻（左闭右开）与可选策略修订号筛选已持久化的投递判定（成功与失败都计入，
同一编号只有一行），返回按选中版本 / 分流侧 / 成功或失败原因分组的数量，并键集分页列出明细。

首次查询固定“截至当时已提交判定”的边界：边界值取建报表瞬间 delivery_decisions 的最大
rowid，之后即使有并发新投递落库，也不会进入本报表（明细翻页携带该边界）。分组总数与明细
使用同一筛选、同一边界、同一只读快照，二者必然一致；页间以 rowid 严格递增游标翻页，不漏不重。
"""
from __future__ import annotations

import base64
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime

from . import store

# 分页上限与 /deliveries 编号长度一样属于调用方契约；非法页大小在请求层明确拒绝。
DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 200


class ReportError(Exception):
    """报表请求非法（无效时间窗 / 非法页大小 / 无效游标），映射为 422。"""

    def __init__(self, code: str, message: str, **extra: object):
        super().__init__(message)
        self.code = code
        self.extra = extra


# ---------- 带时区的时间窗 ----------

def parse_instant(value: str) -> datetime:
    """解析带时区偏移的时刻；裸时间或无法解析一律拒绝。"""
    try:
        instant = datetime.fromisoformat(value)
    except ValueError:
        raise ReportError("invalid_time_window", f"not a valid ISO-8601 timestamp with timezone: {value!r}")
    if instant.tzinfo is None or instant.utcoffset() is None:
        raise ReportError("invalid_time_window", f"timestamp must carry an explicit timezone offset: {value!r}")
    return instant


def canonical_instant(value: str) -> str:
    """把任意带时区的 ISO 时刻规范化为 UTC 可字典序比较的秒级字符串（与 created_at 同形）。"""
    return store.utc_second_key(parse_instant(value))


# ---------- 不透明翻页游标 ----------

@dataclass(frozen=True)
class Cursor:
    # 游标内固化首次查询的全部筛选与边界；翻页请求只能改 page_size
    start: str
    end: str
    policy_revision: int | None
    boundary: int
    after_rowid: int
    page_size: int


def encode_cursor(cursor: Cursor) -> str:
    raw = json.dumps(
        {
            "v": 1,
            "start": cursor.start,
            "end": cursor.end,
            "policy_revision": cursor.policy_revision,
            "boundary": cursor.boundary,
            "after_rowid": cursor.after_rowid,
            "page_size": cursor.page_size,
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode_cursor(token: str) -> Cursor:
    try:
        padding = "=" * (-len(token) % 4)
        raw = base64.urlsafe_b64decode(token + padding)
        payload = json.loads(raw)
    except (ValueError, json.JSONDecodeError):
        raise ReportError("invalid_cursor", "malformed pagination cursor")
    if not isinstance(payload, dict) or payload.get("v") != 1:
        raise ReportError("invalid_cursor", "unsupported pagination cursor")
    try:
        cursor = Cursor(
            start=payload["start"],
            end=payload["end"],
            policy_revision=payload["policy_revision"],
            boundary=int(payload["boundary"]),
            after_rowid=int(payload["after_rowid"]),
            page_size=int(payload["page_size"]),
        )
    except (KeyError, TypeError, ValueError):
        raise ReportError("invalid_cursor", "pagination cursor is missing fields or has bad types")
    if (
        not isinstance(cursor.start, str)
        or not isinstance(cursor.end, str)
        or cursor.boundary < 0
        or cursor.after_rowid < 0
        or cursor.page_size < 1
        or cursor.page_size > MAX_PAGE_SIZE
        or (cursor.policy_revision is not None and cursor.policy_revision < 1)
    ):
        raise ReportError("invalid_cursor", "pagination cursor has invalid field values")
    return cursor


# ---------- 报表读取 ----------

def _window_filters(start: str, end: str, policy_revision: int | None, boundary: int) -> tuple[str, list]:
    """分组计数与明细共用的窗口筛选（左闭右开时间窗 + 首次查询边界），不含翻页位置。"""
    clauses = ["created_at >= ?", "created_at < ?", "rowid <= ?"]
    params: list = [start, end, boundary]
    if policy_revision is not None:
        clauses.append("policy_revision = ?")
        params.append(policy_revision)
    return " AND ".join(clauses), params


def run_report(
    conn: sqlite3.Connection,
    *,
    start_raw: str,
    end_raw: str,
    policy_revision: int | None,
    page_size: int,
    cursor_token: str | None,
) -> dict:
    """在单个只读快照事务内完成“边界 + 分组计数 + 当页明细”。须在只读事务内调用。

    首次查询（无游标）：解析并校验时间窗与页大小，固定当前已提交判定的 rowid 上界；
    后续翻页（带游标）：筛选与边界以游标为准，仅允许 page_size 变化；显式传入与游标
    不一致的筛选参数会被明确拒绝，防止新旧筛选混用。
    """
    # 时间窗始终用本次请求参数校验；游标翻页时还须与游标内固化的窗口一致
    start = canonical_instant(start_raw)
    end = canonical_instant(end_raw)
    if not start < end:
        raise ReportError("invalid_time_window", "start must be strictly before end", start=start, end=end)
    if policy_revision is not None and policy_revision < 1:
        raise ReportError("invalid_policy_revision", "policy_revision must be a positive integer")
    if page_size < 1 or page_size > MAX_PAGE_SIZE:
        raise ReportError("invalid_page_size", f"page_size must be between 1 and {MAX_PAGE_SIZE}", page_size=page_size)

    if cursor_token is None:
        boundary = store.max_decision_rowid(conn)
        after_rowid = 0
        window_size = page_size
    else:
        cursor = decode_cursor(cursor_token)
        if (
            cursor.start != start
            or cursor.end != end
            or cursor.policy_revision != policy_revision
        ):
            raise ReportError(
                "cursor_filter_mismatch",
                "cursor was issued for a different filter; repeat the original start/end/policy_revision",
            )
        boundary = cursor.boundary
        after_rowid = cursor.after_rowid
        window_size = page_size

    where, params = _window_filters(start, end, policy_revision, boundary)

    # 分组计数与明细共用同一筛选；outcome：成功为 ok，失败为首次判定的错误类型
    group_rows = conn.execute(
        f"""
        SELECT version, selection,
               CASE WHEN status = 'ok' THEN 'ok' ELSE json_extract(error, '$.error') END AS outcome,
               COUNT(*) AS count
        FROM delivery_decisions
        WHERE {where}
        GROUP BY version, selection, outcome
        ORDER BY version, selection, outcome
        """,
        params,
    ).fetchall()
    groups = [
        {"version": row["version"], "selection": row["selection"], "outcome": row["outcome"], "count": row["count"]}
        for row in group_rows
    ]

    rows = conn.execute(
        f"""
        SELECT rowid AS _rowid, delivery_id, address, policy_revision, selection, version, status,
               json_extract(error, '$.error') AS error_type, created_at
        FROM delivery_decisions
        WHERE {where} AND rowid > ?
        ORDER BY rowid
        LIMIT ?
        """,
        [*params, after_rowid, window_size + 1],
    ).fetchall()

    items = [
        {
            "delivery_id": row["delivery_id"],
            "address": row["address"],
            "policy_revision": row["policy_revision"],
            "version": row["version"],
            "selection": row["selection"],
            "status": row["status"],
            "error_type": row["error_type"],  # status='ok' 时为 null
            "created_at": row["created_at"],
        }
        for row in rows[:window_size]
    ]
    has_next = len(rows) > window_size
    next_cursor: str | None = None
    if has_next:
        next_cursor = encode_cursor(
            Cursor(
                start=start,
                end=end,
                policy_revision=policy_revision,
                boundary=boundary,
                after_rowid=rows[window_size - 1]["_rowid"],
                page_size=window_size,
            )
        )

    return {
        "start": start,
        "end": end,
        "policy_revision": policy_revision,
        "boundary": boundary,
        "groups": groups,
        "total": sum(g["count"] for g in groups),
        "page_size": window_size,
        "items": items,
        "next_cursor": next_cursor,
    }
