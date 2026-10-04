"""历史投递核对报表（GET /delivery-reports）：

- 左闭右开的带时区时间窗、可选策略修订号筛选；成功与已持久化的失败都计入，同一编号只计一次；
- 分组按选中版本 / 分流侧 / 成功或失败原因，分组总数与明细一致；
- 首次查询固定边界（当时已提交判定的最大 rowid），翻页沿用筛选与边界，
  并发新投递不进入旧报表；键集分页页间不漏不重；
- 空结果零计数 + 空页；无效时间窗 / 非法页大小 / 无效游标 / 游标与筛选不匹配一律 422；
- 既有投递判定、按编号查询与原样重放语义不受影响。
"""
import base64
import json
import os
import tempfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

# 必须在导入 app 之前设置，config 在导入时读取
os.environ.setdefault("ROUTER_DB", os.path.join(tempfile.mkdtemp(prefix="router-test-"), "test.db"))

import pytest
from fastapi.testclient import TestClient

from app import db
from app.main import app


@pytest.fixture()
def client():
    db.init_db()
    conn = db.connect()
    conn.executescript(
        "DELETE FROM draft_rules;"
        "DELETE FROM delivery_policy;"
        "DELETE FROM delivery_decisions;"
        "DELETE FROM version_rules;"
        "DELETE FROM versions;"
        "DELETE FROM sqlite_sequence WHERE name = 'versions';"
        "DELETE FROM draft_revisions;"
        "INSERT INTO draft_revisions (revision, rules, rule_count, created_at)"
        " VALUES (0, '[]', 0, strftime('%Y-%m-%dT%H:%M:%S+00:00', 'now'));"
        "UPDATE draft_meta SET revision = 0 WHERE id = 1;"
    )
    conn.close()
    with TestClient(app) as c:
        yield c


BASE_RULES = [
    {"match_type": "exact", "match_key": "shared@corp.example.com", "action": "route",
     "targets": [{"kind": "mailbox", "value": "base@mail.example.com"}]},
    {"match_type": "domain", "match_key": "corp.example.com", "action": "route",
     "targets": [{"kind": "mailbox", "value": "base-catchall@mail.example.com"}]},
]
CANDIDATE_RULES = [
    {"match_type": "exact", "match_key": "shared@corp.example.com", "action": "route",
     "targets": [{"kind": "mailbox", "value": "cand@mail.example.com"}]},
    {"match_type": "exact", "match_key": "blocked@corp.example.com", "action": "reject", "targets": []},
]


def put_rules(client, revision, rules):
    return client.put("/draft", json={"revision": revision, "rules": rules})


def publish(client, revision):
    return client.post("/publish", json={"revision": revision})


def publish_two_versions(client):
    assert put_rules(client, 0, BASE_RULES).status_code == 200
    assert publish(client, 1).status_code == 200
    assert put_rules(client, 1, CANDIDATE_RULES).status_code == 200
    assert publish(client, 2).status_code == 200
    return 1, 2


def set_policy(client, revision, baseline, candidate, percent):
    return client.put("/policy", json={
        "revision": revision, "baseline_version": baseline,
        "candidate_version": candidate, "candidate_percent": percent,
    })


def submit(client, delivery_id, address):
    return client.post("/deliveries", json={"delivery_id": delivery_id, "address": address})


def report(client, start, end, **params):
    return client.get("/delivery-reports", params={"start": start, "end": end, **params})


# 判定 created_at 为 UTC 秒级；测试窗直接围绕当前时刻取，避免与系统时钟错位
def window_around_now(padding_seconds=300):
    now = datetime.now(timezone.utc)
    return (
        (now - timedelta(seconds=padding_seconds)).isoformat(timespec="seconds"),
        (now + timedelta(seconds=padding_seconds)).isoformat(timespec="seconds"),
    )


def collect_all_pages(client, start, end, page_size, **params):
    """翻完全部页，返回 (groups, total, items, first_body)。"""
    items, groups, total = [], None, None
    cursor, first_body = None, None
    seen_boundaries = set()
    while True:
        p = {"page_size": page_size, **params}
        if cursor is not None:
            p["cursor"] = cursor
        body = report(client, start, end, **p)
        assert body.status_code == 200, body.text
        data = body.json()
        if first_body is None:
            first_body = data
        groups, total = data["groups"], data["total"]
        seen_boundaries.add(data["boundary"])
        items.extend(data["items"])
        cursor = data["next_cursor"]
        if cursor is None:
            break
    assert len(seen_boundaries) == 1  # 所有页同一固定边界
    return groups, total, items, first_body


# ---------- 空结果 ----------

def test_empty_window_returns_zero_counts_and_empty_page(client):
    v1, v2 = publish_two_versions(client)
    set_policy(client, 0, v1, v2, 100)
    submit(client, "msg-1", "shared@corp.example.com")
    start, end = "2000-01-01T00:00:00+00:00", "2000-01-02T00:00:00+00:00"
    body = report(client, start, end)
    assert body.status_code == 200
    data = body.json()
    assert data["groups"] == []
    assert data["total"] == 0
    assert data["items"] == []
    assert data["next_cursor"] is None
    assert data["page_size"] == 50  # 默认页大小
    assert data["boundary"] >= 1


def test_policy_revision_filter_without_match_is_empty(client):
    v1, v2 = publish_two_versions(client)
    set_policy(client, 0, v1, v2, 100)
    submit(client, "msg-1", "shared@corp.example.com")
    start, end = window_around_now()
    data = report(client, start, end, policy_revision=99).json()
    assert data["groups"] == [] and data["total"] == 0 and data["items"] == []


# ---------- 分组计数：版本 / 分流侧 / 成功或失败原因 ----------

def test_groups_split_by_version_selection_and_outcome(client):
    v1, v2 = publish_two_versions(client)
    set_policy(client, 0, v1, v2, 100)  # 全候选（v2）：shared 成功，blocked 拒收
    assert submit(client, "ok-1", "shared@corp.example.com").status_code == 200
    assert submit(client, "ok-2", "shared@corp.example.com").status_code == 200
    assert submit(client, "fail-1", "blocked@corp.example.com").status_code == 422
    # 候选无域兜底 → no_route 失败
    assert submit(client, "fail-2", "stranger@corp.example.com").status_code == 422
    assert set_policy(client, 1, v1, v2, 0).status_code == 200  # 全基线（v1）
    assert submit(client, "ok-3", "shared@corp.example.com").status_code == 200

    start, end = window_around_now()
    data = report(client, start, end).json()
    groups = {(g["version"], g["selection"], g["outcome"]): g["count"] for g in data["groups"]}
    assert groups == {
        (v1, "baseline", "ok"): 1,
        (v2, "candidate", "ok"): 2,
        (v2, "candidate", "rejected"): 1,
        (v2, "candidate", "no_route"): 1,
    }
    assert data["total"] == 5  # 成功与已持久化失败都计入


def test_item_fields_include_required_columns(client):
    v1, v2 = publish_two_versions(client)
    set_policy(client, 0, v1, v2, 100)
    submit(client, "ok-1", "  Shared@Corp.Example.COM ")
    submit(client, "fail-1", "blocked@corp.example.com")
    start, end = window_around_now()
    items = report(client, start, end).json()["items"]
    by_id = {i["delivery_id"]: i for i in items}
    ok = by_id["ok-1"]
    assert ok["address"] == "shared@corp.example.com"  # 规范化地址
    assert ok["policy_revision"] == 1
    assert ok["version"] == v2
    assert ok["selection"] == "candidate"
    assert ok["status"] == "ok"
    assert ok["error_type"] is None
    assert ok["created_at"]
    failed = by_id["fail-1"]
    assert failed["status"] == "failed"
    assert failed["error_type"] == "rejected"
    assert failed["version"] == v2 and failed["selection"] == "candidate"


def test_replayed_submission_counted_once(client):
    v1, v2 = publish_two_versions(client)
    set_policy(client, 0, v1, v2, 100)
    for _ in range(3):  # 同一编号重复提交（原样重放）
        submit(client, "dup", "shared@corp.example.com")
    start, end = window_around_now()
    data = report(client, start, end).json()
    assert data["total"] == 1
    assert [i["delivery_id"] for i in data["items"]] == ["dup"]


def test_policy_revision_filter(client):
    v1, v2 = publish_two_versions(client)
    set_policy(client, 0, v1, v2, 100)
    submit(client, "rev1", "shared@corp.example.com")
    assert set_policy(client, 1, v1, v2, 0).status_code == 200
    submit(client, "rev2", "shared@corp.example.com")
    start, end = window_around_now()
    data = report(client, start, end, policy_revision=1).json()
    assert data["total"] == 1
    assert [i["delivery_id"] for i in data["items"]] == ["rev1"]
    assert all(i["policy_revision"] == 1 for i in data["items"])


# ---------- 左闭右开时间窗（带时区） ----------

def test_window_is_left_closed_right_open_and_timezone_aware(client):
    v1, v2 = publish_two_versions(client)
    set_policy(client, 0, v1, v2, 100)
    submit(client, "anchor", "shared@corp.example.com")
    created_at = client.get("/deliveries/anchor").json()["created_at"]  # UTC 秒级
    t = datetime.fromisoformat(created_at)
    before = (t - timedelta(seconds=1)).isoformat(timespec="seconds")
    after = (t + timedelta(seconds=1)).isoformat(timespec="seconds")

    # [t, after) 含该判定
    assert report(client, created_at, after).json()["total"] == 1
    # [before, t) 不含（右开）
    assert report(client, before, created_at).json()["total"] == 0
    # 用非 UTC 时区表达同样的边界也成立
    plus8 = t.astimezone(timezone(timedelta(hours=8)))
    start8 = plus8.isoformat(timespec="seconds")
    end8 = (plus8 + timedelta(seconds=1)).isoformat(timespec="seconds")
    assert report(client, start8, end8).json()["total"] == 1
    # 响应中的 start/end 被规范化为 UTC
    body = report(client, start8, end8).json()
    assert body["start"] == created_at and body["end"] == after


# ---------- 分页：页间不漏不重，分组总数与明细一致 ----------

def test_pagination_covers_all_without_gaps_or_duplicates(client):
    v1, v2 = publish_two_versions(client)
    set_policy(client, 0, v1, v2, 50)
    total_n = 7
    for n in range(total_n):
        submit(client, f"msg-{n:03d}", f"user{n}@corp.example.com")
    start, end = window_around_now()

    groups, total, items, first = collect_all_pages(client, start, end, page_size=3)
    assert total == total_n
    assert len(items) == total_n
    ids = [i["delivery_id"] for i in items]
    assert len(ids) == len(set(ids)) == total_n  # 不重不漏
    assert ids == sorted(ids)  # 按 rowid 升序
    assert sum(g["count"] for g in groups) == total_n
    # 首页正好 page_size 且给出游标
    assert len(first["items"]) == 3 and first["next_cursor"]
    # 每页分组计数与总数都一致（分组不随翻页位置变化）
    cursor = None
    seen_groups = []
    while True:
        params = {"page_size": 3}
        if cursor:
            params["cursor"] = cursor
        data = report(client, start, end, **params).json()
        seen_groups.append((data["groups"], data["total"]))
        cursor = data["next_cursor"]
        if cursor is None:
            break
    assert all(g == seen_groups[0] for g in seen_groups)


def test_last_page_shorter_and_terminates(client):
    v1, v2 = publish_two_versions(client)
    set_policy(client, 0, v1, v2, 50)
    for n in range(5):
        submit(client, f"m{n}", f"u{n}@corp.example.com")
    start, end = window_around_now()
    first = report(client, start, end, page_size=5).json()
    assert len(first["items"]) == 5 and first["next_cursor"] is None
    first = report(client, start, end, page_size=2).json()
    second = report(client, start, end, page_size=2, cursor=first["next_cursor"]).json()
    assert len(second["items"]) == 2 and second["next_cursor"]
    third = report(client, start, end, page_size=2, cursor=second["next_cursor"]).json()
    assert len(third["items"]) == 1 and third["next_cursor"] is None


def test_page_size_may_change_between_pages(client):
    v1, v2 = publish_two_versions(client)
    set_policy(client, 0, v1, v2, 50)
    for n in range(5):
        submit(client, f"m{n}", f"u{n}@corp.example.com")
    start, end = window_around_now()
    first = report(client, start, end, page_size=4).json()
    assert len(first["items"]) == 4
    second = report(client, start, end, page_size=10, cursor=first["next_cursor"]).json()
    assert len(second["items"]) == 1 and second["next_cursor"] is None


# ---------- 固定边界：并发新投递不进入旧报表 ----------

def test_concurrent_new_deliveries_never_enter_old_report(client):
    v1, v2 = publish_two_versions(client)
    set_policy(client, 0, v1, v2, 50)
    for n in range(3):
        submit(client, f"old-{n}", f"old{n}@corp.example.com")
    start, end = window_around_now()

    first = report(client, start, end, page_size=2)
    assert first.status_code == 200
    first_data = first.json()
    boundary = first_data["boundary"]
    assert first_data["total"] == 3

    # 首次查询之后到达的新投递
    submit(client, "new-after-query", "new@corp.example.com")
    cursor = first_data["next_cursor"]
    second = report(client, start, end, page_size=2, cursor=cursor)
    assert second.status_code == 200
    second_data = second.json()
    assert second_data["boundary"] == boundary
    assert second_data["total"] == 3  # 分组总数仍是旧边界
    all_ids = [i["delivery_id"] for i in first_data["items"] + second_data["items"]]
    assert "new-after-query" not in all_ids

    # 不带游标的全新查询会取新边界，新投递进入新报表
    fresh = report(client, start, end, page_size=10).json()
    assert fresh["boundary"] > boundary and fresh["total"] == 4


def test_concurrent_submissions_during_report_stay_consistent(client):
    v1, v2 = publish_two_versions(client)
    set_policy(client, 0, v1, v2, 50)
    for n in range(4):
        submit(client, f"old-{n}", f"old{n}@corp.example.com")
    start, end = window_around_now()
    first = report(client, start, end, page_size=2).json()

    def work(n):
        return submit(client, f"parallel-{n}", f"parallel{n}@corp.example.com").status_code

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(work, range(4)))

    second = report(client, start, end, page_size=2, cursor=first["next_cursor"]).json()
    assert second["total"] == first["total"] == 4
    ids = [i["delivery_id"] for i in first["items"] + second["items"]]
    assert sorted(ids) == [f"old-{n}" for n in range(4)]


# ---------- 参数校验：时间窗 / 页大小 / 游标 ----------

@pytest.mark.parametrize("bad_start,bad_end", [
    ("not-a-time", "2026-10-05T00:00:00+00:00"),          # 无法解析
    ("2026-10-04T00:00:00", "2026-10-05T00:00:00+00:00"),  # 裸时间（无时区）
    ("2026-10-05T00:00:00+00:00", "2026-10-05T00:00:00+00:00"),  # start == end
    ("2026-10-06T00:00:00+00:00", "2026-10-05T00:00:00+00:00"),  # start > end
    ("2026-10-04T25:00:00+00:00", "2026-10-05T00:00:00+00:00"),  # 非法钟点
])
def test_invalid_time_window_rejected(client, bad_start, bad_end):
    r = report(client, bad_start, bad_end)
    assert r.status_code == 422
    assert r.json()["detail"]["error"] == "invalid_time_window"


def test_missing_start_or_end_rejected(client):
    assert client.get("/delivery-reports", params={"end": "2026-10-05T00:00:00+00:00"}).status_code == 422
    assert client.get("/delivery-reports", params={"start": "2026-10-04T00:00:00+00:00"}).status_code == 422


@pytest.mark.parametrize("bad_size", ["0", "-1", "201", "abc", "1.5", ""])
def test_invalid_page_size_rejected(client, bad_size):
    r = report(client, "2026-10-04T00:00:00+00:00", "2026-10-05T00:00:00+00:00", page_size=bad_size)
    assert r.status_code == 422
    assert r.json()["detail"]["error"] == "invalid_page_size"


def test_invalid_cursor_rejected(client):
    r = report(client, "2026-10-04T00:00:00+00:00", "2026-10-05T00:00:00+00:00", cursor="not-base64!")
    assert r.status_code == 422
    assert r.json()["detail"]["error"] == "invalid_cursor"

    # base64 但内容不是游标
    token = base64.urlsafe_b64encode(json.dumps({"hello": "world"}).encode()).decode()
    r = report(client, "2026-10-04T00:00:00+00:00", "2026-10-05T00:00:00+00:00", cursor=token)
    assert r.status_code == 422
    assert r.json()["detail"]["error"] == "invalid_cursor"


def test_cursor_with_mismatched_filter_rejected(client):
    v1, v2 = publish_two_versions(client)
    set_policy(client, 0, v1, v2, 50)
    for n in range(3):
        submit(client, f"m{n}", f"u{n}@corp.example.com")
    start, end = window_around_now()
    cursor = report(client, start, end, page_size=2).json()["next_cursor"]
    other_start = (datetime.fromisoformat(start) + timedelta(seconds=1)).isoformat(timespec="seconds")
    other_end = (datetime.fromisoformat(end) + timedelta(seconds=1)).isoformat(timespec="seconds")

    r1 = report(client, other_start, end, page_size=2, cursor=cursor)
    assert r1.status_code == 422 and r1.json()["detail"]["error"] == "cursor_filter_mismatch"
    r2 = report(client, start, other_end, page_size=2, cursor=cursor)
    assert r2.status_code == 422 and r2.json()["detail"]["error"] == "cursor_filter_mismatch"
    r3 = report(client, start, end, page_size=2, cursor=cursor, policy_revision=7)
    assert r3.status_code == 422 and r3.json()["detail"]["error"] == "cursor_filter_mismatch"


def test_invalid_policy_revision_rejected(client):
    r = client.get("/delivery-reports", params={
        "start": "2026-10-04T00:00:00+00:00", "end": "2026-10-05T00:00:00+00:00",
        "policy_revision": 0,
    })
    assert r.status_code == 422  # Query(ge=1) 层拒绝


# ---------- 既有语义兼容 ----------

def test_existing_flows_unaffected(client):
    v1, v2 = publish_two_versions(client)
    set_policy(client, 0, v1, v2, 100)
    first = submit(client, "msg-x", "shared@corp.example.com")
    assert first.status_code == 200
    # 原样重放
    replay = submit(client, "msg-x", "SHARED@CORP.EXAMPLE.COM")
    assert replay.status_code == 200 and replay.json() == first.json()
    # 按编号查询
    stored = client.get("/deliveries/msg-x")
    assert stored.status_code == 200 and stored.json()["delivery_id"] == "msg-x"
    # /deliver 即时解析不落库
    assert client.post("/deliver", json={"address": "shared@corp.example.com"}).status_code == 200
    # /resolve 显式版本不受影响
    r = client.post("/resolve", json={"address": "shared@corp.example.com", "version": v1})
    assert r.status_code == 200 and [e["mailbox"] for e in r.json()["endpoints"]] == ["base@mail.example.com"]
