"""持久层：草稿（带乐观锁修订号、按键合并与修订快照）、不可变发布版本、默认投递策略（基线/候选灰度）与幂等投递判定。"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

from .routing import Rule, RuleSet, Target


class RevisionConflict(Exception):
    """所见修订号与当前修订号不一致（草稿或默认投递策略）。"""

    def __init__(self, current: int, subject: str = "draft"):
        super().__init__(f"revision conflict: current {subject} revision is {current}")
        self.current = current


class RevisionSnapshotUnavailable(Exception):
    """无法取得客户端所见修订的快照（如旧库未留存该修订）：明确拒绝合并。"""

    def __init__(self, revision: int, current: int):
        super().__init__(f"revision snapshot unavailable: {revision} (current revision is {current})")
        self.revision = revision
        self.current = current


class MergeConflict(RevisionConflict):
    """落后修订合并时，本次操作涉及的键中有相对所见快照已改变的键。"""

    def __init__(self, current: int, conflict_keys: list[dict]):
        super().__init__(current)
        self.conflict_keys = conflict_keys


class DraftRuleNotFound(Exception):
    """删除操作引用了当前草稿中不存在的规则。"""

    def __init__(self, match_type: str, match_key: str):
        super().__init__(f"rule not found in draft: {match_type} {match_key}")
        self.match_type = match_type
        self.match_key = match_key


class PolicyVersionNotFound(Exception):
    """策略引用了不存在的已发布版本。"""

    def __init__(self, version: int):
        super().__init__(f"published version not found: {version}")
        self.version = version


def _row_to_rule(row: sqlite3.Row) -> Rule:
    targets = tuple(Target(kind=t["kind"], value=t["value"]) for t in json.loads(row["targets"]))
    return Rule(
        match_type=row["match_type"],
        match_key=row["match_key"],
        action=row["action"],
        targets=targets,
    )


def _rule_from_doc(doc: dict) -> Rule:
    return Rule(
        match_type=doc["match_type"],
        match_key=doc["match_key"],
        action=doc["action"],
        targets=tuple(Target(kind=t["kind"], value=t["value"]) for t in doc.get("targets", [])),
    )


def rule_to_dict(rule: Rule) -> dict:
    return {
        "match_type": rule.match_type,
        "match_key": rule.match_key,
        "action": rule.action,
        "targets": [{"kind": t.kind, "value": t.value} for t in rule.targets],
    }


def _save_draft_snapshot(conn: sqlite3.Connection, revision: int, rules: list[Rule]) -> None:
    """把某次草稿变更的完整规则快照写入 draft_revisions。须在事务内调用。"""
    created_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    conn.execute(
        "INSERT INTO draft_revisions (revision, rules, rule_count, created_at) VALUES (?, ?, ?, ?)",
        (revision, json.dumps([rule_to_dict(r) for r in rules]), len(rules), created_at),
    )


def get_draft(conn: sqlite3.Connection) -> tuple[int, list[Rule]]:
    revision = conn.execute("SELECT revision FROM draft_meta WHERE id = 1").fetchone()["revision"]
    rows = conn.execute(
        "SELECT match_type, match_key, action, targets FROM draft_rules ORDER BY match_type, match_key"
    ).fetchall()
    return revision, [_row_to_rule(r) for r in rows]


def replace_draft(conn: sqlite3.Connection, expected_revision: int, rules: list[Rule]) -> int:
    """整体替换草稿；修订号不匹配抛 RevisionConflict。须在事务内调用。

    成功后留存完整修订快照（与按键合并一样可核对）。
    """
    revision, _ = get_draft(conn)
    if revision != expected_revision:
        raise RevisionConflict(revision)
    conn.execute("DELETE FROM draft_rules")
    conn.executemany(
        "INSERT INTO draft_rules (match_type, match_key, action, targets) VALUES (?, ?, ?, ?)",
        [
            (
                r.match_type,
                r.match_key,
                r.action,
                json.dumps([{"kind": t.kind, "value": t.value} for t in r.targets]),
            )
            for r in rules
        ],
    )
    new_revision = revision + 1
    conn.execute("UPDATE draft_meta SET revision = ? WHERE id = 1", (new_revision,))
    _save_draft_snapshot(conn, new_revision, rules)
    return new_revision


def _rules_by_key(rules: list[Rule]) -> dict[tuple[str, str], Rule]:
    return {(r.match_type, r.match_key): r for r in rules}


def get_draft_at(conn: sqlite3.Connection, revision: int) -> tuple[int, list[Rule]] | None:
    """读取某个历史草稿修订的完整快照；不存在返回 None。"""
    row = conn.execute(
        "SELECT revision, rules FROM draft_revisions WHERE revision = ?", (revision,)
    ).fetchone()
    if row is None:
        return None
    return row["revision"], [_rule_from_doc(doc) for doc in json.loads(row["rules"])]


def list_draft_revisions(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT revision, rule_count, created_at FROM draft_revisions ORDER BY revision"
    ).fetchall()
    return [dict(r) for r in rows]


def merge_draft(
    conn: sqlite3.Connection,
    expected_revision: int,
    changes: list[tuple[str, str, str, Rule | None]],
) -> tuple[int, list[Rule]]:
    """把一组按键设置 / 删除操作合并进当前草稿。须在写事务内调用。

    - 取得到所见修订（expected_revision）的快照后逐键核对：仅当本次涉及的每个键
      相对该快照在当前草稿中均未改变（存在性与完整内容一致）时才合并；
      否则抛 MergeConflict（附全部冲突键与当前修订号），草稿不变。
    - 所见修订即当前修订时，键必然“未改变”，直接合并。
    - 无法取得所见修订快照（RevisionSnapshotUnavailable）时明确拒绝合并。
    - 删除当前草稿中不存在的规则抛 DraftRuleNotFound（整组拒绝，草稿不变）。
    成功则写入合并结果、修订号 +1，并留存完整修订快照。
    """
    revision, current_rules = get_draft(conn)
    current_by_key = _rules_by_key(current_rules)

    if revision != expected_revision:
        seen = get_draft_at(conn, expected_revision)
        if seen is None:
            raise RevisionSnapshotUnavailable(expected_revision, revision)
        seen_by_key = _rules_by_key(seen[1])
        conflict_keys: list[dict] = []
        for _op, match_type, key, _rule in changes:
            ident = (match_type, key)
            seen_rule = seen_by_key.get(ident)
            current_rule = current_by_key.get(ident)
            if seen_rule != current_rule:  # 含“快照存在 / 当前不存在”与反向新增
                conflict_keys.append({"match_type": match_type, "match_key": key})
        if conflict_keys:
            raise MergeConflict(revision, conflict_keys)

    # 键相对所见快照均未改变（或所见即当前）：删除前先确认规则当前存在
    for _op, match_type, key, _rule in changes:
        if _op == "delete" and (match_type, key) not in current_by_key:
            raise DraftRuleNotFound(match_type, key)

    merged = dict(current_by_key)
    for op, match_type, key, rule in changes:
        ident = (match_type, key)
        if op == "delete":
            del merged[ident]
        else:
            assert rule is not None
            merged[ident] = rule
    new_rules = sorted(merged.values(), key=lambda r: (r.match_type, r.match_key))

    conn.execute("DELETE FROM draft_rules")
    conn.executemany(
        "INSERT INTO draft_rules (match_type, match_key, action, targets) VALUES (?, ?, ?, ?)",
        [
            (
                r.match_type,
                r.match_key,
                r.action,
                json.dumps([{"kind": t.kind, "value": t.value} for t in r.targets]),
            )
            for r in new_rules
        ],
    )
    new_revision = revision + 1
    conn.execute("UPDATE draft_meta SET revision = ? WHERE id = 1", (new_revision,))
    _save_draft_snapshot(conn, new_revision, new_rules)
    return new_revision, new_rules


def publish(conn: sqlite3.Connection, expected_revision: int, rules: list[Rule]) -> int:
    """把当前草稿固化为不可变版本，返回版本号。须在事务内调用。"""
    revision, _ = get_draft(conn)
    if revision != expected_revision:
        raise RevisionConflict(revision)
    created_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    cur = conn.execute(
        "INSERT INTO versions (source_revision, created_at) VALUES (?, ?)",
        (revision, created_at),
    )
    version = cur.lastrowid
    conn.executemany(
        "INSERT INTO version_rules (version, match_type, match_key, action, targets) VALUES (?, ?, ?, ?, ?)",
        [
            (
                version,
                r.match_type,
                r.match_key,
                r.action,
                json.dumps([{"kind": t.kind, "value": t.value} for t in r.targets]),
            )
            for r in rules
        ],
    )
    return version


def list_versions(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        """
        SELECT v.version, v.source_revision, v.created_at, COUNT(r.match_key) AS rule_count
        FROM versions v LEFT JOIN version_rules r ON r.version = v.version
        GROUP BY v.version ORDER BY v.version
        """
    ).fetchall()
    return [dict(r) for r in rows]


def get_version(conn: sqlite3.Connection, version: int) -> dict | None:
    row = conn.execute(
        "SELECT version, source_revision, created_at FROM versions WHERE version = ?", (version,)
    ).fetchone()
    if row is None:
        return None
    rules = conn.execute(
        "SELECT match_type, match_key, action, targets FROM version_rules "
        "WHERE version = ? ORDER BY match_type, match_key",
        (version,),
    ).fetchall()
    return {**dict(row), "rules": [_row_to_rule(r) for r in rules]}


def load_ruleset(conn: sqlite3.Connection, version: int) -> RuleSet | None:
    found = get_version(conn, version)
    if found is None:
        return None
    return RuleSet(found["rules"])


# ---------- 默认投递策略（基线 / 候选灰度） ----------

def get_policy(conn: sqlite3.Connection) -> dict | None:
    """读取当前默认投递策略；从未配置时返回 None（修订号视为 0）。"""
    row = conn.execute(
        "SELECT revision, baseline_version, candidate_version, candidate_percent, updated_at "
        "FROM delivery_policy WHERE id = 1"
    ).fetchone()
    return dict(row) if row is not None else None


def replace_policy(
    conn: sqlite3.Connection,
    expected_revision: int,
    baseline_version: int,
    candidate_version: int,
    candidate_percent: int,
) -> dict:
    """在同一事务内核验所见策略修订号并保存新策略。

    - 未配置策略时修订号为 0，行尚不存在；
    - 修订号不匹配抛 RevisionConflict，调用方回滚，原策略保持不变；
    - 基线 / 候选必须都是已发布版本，否则抛 PolicyVersionNotFound（同样不改原策略）。
    """
    current = get_policy(conn)
    current_revision = current["revision"] if current is not None else 0
    if current_revision != expected_revision:
        raise RevisionConflict(current_revision, subject="policy")
    for version in (baseline_version, candidate_version):
        if conn.execute("SELECT 1 FROM versions WHERE version = ?", (version,)).fetchone() is None:
            raise PolicyVersionNotFound(version)
    updated_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    conn.execute(
        """
        INSERT INTO delivery_policy (id, revision, baseline_version, candidate_version, candidate_percent, updated_at)
        VALUES (1, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            revision = excluded.revision,
            baseline_version = excluded.baseline_version,
            candidate_version = excluded.candidate_version,
            candidate_percent = excluded.candidate_percent,
            updated_at = excluded.updated_at
        """,
        (current_revision + 1, baseline_version, candidate_version, candidate_percent, updated_at),
    )
    return {
        "revision": current_revision + 1,
        "baseline_version": baseline_version,
        "candidate_version": candidate_version,
        "candidate_percent": candidate_percent,
        "updated_at": updated_at,
    }


# ---------- 幂等投递判定（投递编号 → 首次判定，永久可查、原样重放） ----------

def get_decision(conn: sqlite3.Connection, delivery_id: str) -> dict | None:
    """按投递编号读取已持久化的判定；不存在返回 None。JSON 字段在此还原。"""
    row = conn.execute(
        "SELECT delivery_id, address, policy_revision, baseline_version, candidate_version, "
        "candidate_percent, selection, version, status, endpoints, error, created_at "
        "FROM delivery_decisions WHERE delivery_id = ?",
        (delivery_id,),
    ).fetchone()
    if row is None:
        return None
    decision = dict(row)
    decision["endpoints"] = json.loads(decision["endpoints"]) if decision["endpoints"] is not None else None
    decision["error"] = json.loads(decision["error"]) if decision["error"] is not None else None
    return decision


def save_decision(
    conn: sqlite3.Connection,
    *,
    delivery_id: str,
    address: str,
    policy: dict,
    selection: str,
    version: int,
    status: str,
    endpoints: list[dict] | None,
    error: dict | None,
) -> dict:
    """把首次判定整体持久化（成功含全部终点路径，失败含原错误详情）。须在事务内调用。

    判定一旦写入不再改写：策略后续变更不影响已持久化的结果。
    """
    created_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    conn.execute(
        """
        INSERT INTO delivery_decisions (
            delivery_id, address, policy_revision, baseline_version, candidate_version,
            candidate_percent, selection, version, status, endpoints, error, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            delivery_id,
            address,
            policy["revision"],
            policy["baseline_version"],
            policy["candidate_version"],
            policy["candidate_percent"],
            selection,
            version,
            status,
            json.dumps(endpoints) if endpoints is not None else None,
            json.dumps(error) if error is not None else None,
            created_at,
        ),
    )
    return get_decision(conn, delivery_id)
