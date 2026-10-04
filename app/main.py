"""HTTP API：草稿编辑（乐观锁）、发布（原子校验 + 不可变版本）、按版本解析、发布前预览、默认投递策略、幂等投递判定。"""
from __future__ import annotations

import sqlite3
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException

from . import db, preview, store
from .models import (
    DeliverRequest,
    DeliverySubmit,
    DraftChangesRequest,
    DraftUpdate,
    PolicyUpdate,
    PreviewRequest,
    PublishRequest,
    ResolveRequest,
    ValidationFailure,
    normalize_changes,
    normalize_rules,
)
from .routing import ResolutionError, RuleSet, is_valid_address, resolve, validate_ruleset
from .traffic import select_version


@asynccontextmanager
async def lifespan(_: FastAPI):
    db.init_db()  # 首次启动自动建表
    yield


app = FastAPI(title="Alias Router", version="1.0.0", lifespan=lifespan)


def _conflict(exc: store.RevisionConflict) -> HTTPException:
    return HTTPException(
        status_code=409,
        detail={"error": "revision_conflict", "current_revision": exc.current, "message": str(exc)},
    )


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.get("/draft")
def get_draft() -> dict:
    with db.connect() as conn:
        revision, rules = store.get_draft(conn)
    return {"revision": revision, "rules": [store.rule_to_dict(r) for r in rules]}


@app.put("/draft")
def put_draft(body: DraftUpdate) -> dict:
    """整体替换草稿。必须携带所见修订号，冲突拒绝（409）。"""
    try:
        rules = normalize_rules(body.rules)
    except ValidationFailure as exc:
        raise HTTPException(status_code=422, detail={"error": "invalid_rule", "message": str(exc)})
    try:
        with db.transaction() as conn:
            new_revision = store.replace_draft(conn, body.revision, rules)
    except store.RevisionConflict as exc:
        raise _conflict(exc)
    except sqlite3.OperationalError as exc:  # 并发写冲突
        raise HTTPException(status_code=409, detail={"error": "write_conflict", "message": str(exc)})
    return {"revision": new_revision, "rules": [store.rule_to_dict(r) for r in rules]}


@app.post("/draft/changes")
def patch_draft(body: DraftChangesRequest) -> dict:
    """多人协作：按规则键合并一组设置（upsert）/ 删除（delete）操作。

    规则键与内容沿用现有规范化与编辑期校验；重复操作键、删除当前不存在的规则、
    无效规则一律整组拒绝（422），草稿不变。
    所见修订落后时，仅当本次操作涉及的每个键相对所见快照在当前草稿中均未改变，
    才把整组操作合并并产生一个新修订；否则 409 返回全部冲突键与当前修订号，草稿不变。
    无法取得所见修订的快照时明确拒绝合并（409）。
    核对与写入在单个 BEGIN IMMEDIATE 事务内原子完成。
    """
    try:
        changes = normalize_changes(body.changes)
    except ValidationFailure as exc:
        raise HTTPException(status_code=422, detail={"error": exc.code, "message": str(exc)})
    try:
        with db.transaction() as conn:
            new_revision, rules = store.merge_draft(conn, body.revision, changes)
    except store.MergeConflict as exc:  # 必须先于父类 RevisionConflict 捕获
        raise HTTPException(
            status_code=409,
            detail={
                "error": "revision_conflict",
                "current_revision": exc.current,
                "conflict_keys": exc.conflict_keys,
                "message": f"revision conflict: {len(exc.conflict_keys)} involved key(s) changed since the seen revision",
            },
        )
    except store.RevisionConflict as exc:
        raise _conflict(exc)
    except store.RevisionSnapshotUnavailable as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "error": "revision_snapshot_unavailable",
                "revision": exc.revision,
                "current_revision": exc.current,
                "message": f"snapshot for seen revision {exc.revision} is unavailable; merge refused",
            },
        )
    except store.DraftRuleNotFound as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "error": "rule_not_found",
                "match_type": exc.match_type,
                "match_key": exc.match_key,
                "message": str(exc),
            },
        )
    except sqlite3.OperationalError as exc:  # 并发写冲突
        raise HTTPException(status_code=409, detail={"error": "write_conflict", "message": str(exc)})
    return {"revision": new_revision, "rules": [store.rule_to_dict(r) for r in rules]}


@app.get("/draft/revisions")
def list_draft_revisions() -> dict:
    """列出全部草稿修订（含整体替换与按键合并；revision 0 为初始空草稿）。"""
    with db.connect() as conn:
        return {"revisions": store.list_draft_revisions(conn)}


@app.get("/draft/revisions/{revision}")
def get_draft_revision(revision: int) -> dict:
    """查看某个草稿修订的完整规则快照（可核对每次草稿变更）。"""
    with db.connect() as conn:
        found = store.get_draft_at(conn, revision)
    if found is None:
        raise HTTPException(
            status_code=404,
            detail={"error": "draft_revision_not_found", "revision": revision},
        )
    rev, rules = found
    return {"revision": rev, "rules": [store.rule_to_dict(r) for r in rules]}


@app.post("/publish")
def publish(body: PublishRequest) -> dict:
    """原子核对修订号并校验全部规则目标可解析且无环；成功生成不可变版本，失败保持旧版。"""
    try:
        with db.transaction() as conn:
            revision, rules = store.get_draft(conn)
            if revision != body.revision:
                raise store.RevisionConflict(revision)
            problems = validate_ruleset(RuleSet(rules))
            if problems:
                raise HTTPException(
                    status_code=422,
                    detail={"error": "validation_failed", "message": "unresolvable targets or routing loops", "problems": problems},
                )
            version = store.publish(conn, body.revision, rules)
    except store.RevisionConflict as exc:
        raise _conflict(exc)
    except sqlite3.OperationalError as exc:
        raise HTTPException(status_code=409, detail={"error": "write_conflict", "message": str(exc)})
    return {"version": version, "source_revision": body.revision, "rule_count": len(rules)}


@app.get("/versions")
def list_versions() -> dict:
    with db.connect() as conn:
        return {"versions": store.list_versions(conn)}


@app.get("/versions/{version}")
def get_version(version: int) -> dict:
    with db.connect() as conn:
        found = store.get_version(conn, version)
    if found is None:
        raise HTTPException(status_code=404, detail={"error": "version_not_found", "version": version})
    return {
        "version": found["version"],
        "source_revision": found["source_revision"],
        "created_at": found["created_at"],
        "rules": [store.rule_to_dict(r) for r in found["rules"]],
    }


@app.post("/resolve")
def resolve_address(body: ResolveRequest) -> dict:
    """按指定已发布版本解析收件地址。任一分支拒收 / 无路由 / 成环则整次失败。"""
    address = body.address.strip().lower()
    if not is_valid_address(address):
        raise HTTPException(status_code=422, detail={"error": "invalid_address", "message": f"invalid address: {body.address!r}"})
    with db.connect() as conn:
        ruleset = store.load_ruleset(conn, body.version)
    if ruleset is None:
        raise HTTPException(status_code=404, detail={"error": "version_not_found", "version": body.version})
    try:
        endpoints = resolve(address, ruleset)
    except ResolutionError as exc:
        raise HTTPException(status_code=422, detail=exc.detail())
    return {
        "address": address,
        "version": body.version,
        "endpoints": [
            {"mailbox": mailbox, "paths": sorted(paths)}
            for mailbox, paths in sorted(endpoints.items())
        ],
    }


@app.post("/preview")
def preview_changes(body: PreviewRequest) -> dict:
    """发布前预览：当前草稿相对基线版本的投递变化。

    在同一只读事务快照内读取基线版本与草稿（不改动草稿或历史版本），
    对两侧全部精确规则地址与别名目标逐一解析，并对两侧出现过域规则的每个域
    用合成示例地址探测兜底类别；仅列出结果有变化的项，附两侧完整结果。
    草稿尚未通过发布校验时仍正常展示相应失败结果。
    """
    try:
        with db.read_transaction() as conn:
            revision, draft_rules = store.get_draft(conn)
            if revision != body.revision:
                raise store.RevisionConflict(revision)
            found = store.get_version(conn, body.base_version)
            if found is None:
                raise HTTPException(
                    status_code=404,
                    detail={"error": "version_not_found", "version": body.base_version},
                )
            base_rules = found["rules"]
    except store.RevisionConflict as exc:
        raise _conflict(exc)
    diff = preview.preview(base_rules, draft_rules)
    return {"base_version": body.base_version, "revision": revision, **diff}


# ---------- 默认投递策略（已发布版本间的灰度分桶） ----------

def _policy_doc(policy: dict) -> dict:
    return {
        "revision": policy["revision"],
        "baseline_version": policy["baseline_version"],
        "candidate_version": policy["candidate_version"],
        "candidate_percent": policy["candidate_percent"],
        "updated_at": policy["updated_at"],
    }


@app.get("/policy")
def get_policy() -> dict:
    """查看当前默认投递策略。从未配置时返回 404（策略修订号视为 0）。"""
    with db.read_transaction() as conn:
        policy = store.get_policy(conn)
    if policy is None:
        raise HTTPException(
            status_code=404,
            detail={"error": "policy_not_configured", "revision": 0,
                    "message": "no delivery policy configured"},
        )
    return _policy_doc(policy)


@app.put("/policy")
def put_policy(body: PolicyUpdate) -> dict:
    """配置默认投递策略：基线 / 候选均须为已发布版本。

    在单个 BEGIN IMMEDIATE 事务内核验所见策略修订号并保存；
    修订号冲突（409）或引用未发布版本（422）时回滚，原策略不被覆盖。
    草稿与历史版本均不会因策略变更而改写。
    """
    try:
        with db.transaction() as conn:
            policy = store.replace_policy(
                conn,
                body.revision,
                body.baseline_version,
                body.candidate_version,
                body.candidate_percent,
            )
    except store.RevisionConflict as exc:
        raise HTTPException(
            status_code=409,
            detail={"error": "revision_conflict", "current_revision": exc.current, "message": str(exc)},
        )
    except store.PolicyVersionNotFound as exc:
        raise HTTPException(
            status_code=422,
            detail={"error": "version_not_published", "version": exc.version,
                    "message": f"only published versions may be used in a delivery policy: {exc.version}"},
        )
    except sqlite3.OperationalError as exc:  # 并发写冲突
        raise HTTPException(status_code=409, detail={"error": "write_conflict", "message": str(exc)})
    return _policy_doc(policy)


@app.post("/deliver")
def deliver(body: DeliverRequest) -> dict:
    """默认投递：按当前策略与规范化地址稳定分桶，选择基线或候选已发布版本解析。

    未配置策略时明确报错（409），不使用草稿或最新版本兜底；
    同一地址在同一策略下总选同一版本；选中版本的拒收 / 无路由 / 成环
    仍整次失败（422），不自动改用另一版本。
    """
    address = body.address.strip().lower()
    if not is_valid_address(address):
        raise HTTPException(status_code=422, detail={"error": "invalid_address", "message": f"invalid address: {body.address!r}"})
    with db.read_transaction() as conn:
        policy = store.get_policy(conn)
        if policy is None:
            raise HTTPException(
                status_code=409,
                detail={"error": "policy_not_configured",
                        "message": "no delivery policy configured; specify an explicit version via /resolve"},
            )
        chosen = select_version(address, policy["candidate_percent"])
        version = policy["baseline_version"] if chosen == "baseline" else policy["candidate_version"]
        ruleset = store.load_ruleset(conn, version)
    # 策略引用的版本必然已发布（写入时有外键 + 发布版本不可变），此处仅作防御
    if ruleset is None:
        raise HTTPException(status_code=404, detail={"error": "version_not_found", "version": version})
    try:
        endpoints = resolve(address, ruleset)
    except ResolutionError as exc:
        detail = exc.detail()
        detail.update({"policy_revision": policy["revision"], "version": version, "selection": chosen})
        raise HTTPException(status_code=422, detail=detail)
    return {
        "address": address,
        "policy_revision": policy["revision"],
        "baseline_version": policy["baseline_version"],
        "candidate_version": policy["candidate_version"],
        "candidate_percent": policy["candidate_percent"],
        "selection": chosen,
        "version": version,
        "endpoints": [
            {"mailbox": mailbox, "paths": sorted(paths)}
            for mailbox, paths in sorted(endpoints.items())
        ],
    }


# ---------- 幂等投递判定（投递编号 → 首次判定持久化，重试 / 并发原样重放） ----------

def _decision_doc(decision: dict) -> dict:
    """成功判定的响应体：/deliver 的策略与终点字段 + 投递编号。重放时内容完全相同。"""
    return {
        "delivery_id": decision["delivery_id"],
        "address": decision["address"],
        "policy_revision": decision["policy_revision"],
        "baseline_version": decision["baseline_version"],
        "candidate_version": decision["candidate_version"],
        "candidate_percent": decision["candidate_percent"],
        "selection": decision["selection"],
        "version": decision["version"],
        "endpoints": decision["endpoints"],
    }


@app.post("/deliveries")
def submit_delivery(body: DeliverySubmit) -> dict:
    """按投递编号幂等提交路由判定（供可能超时重试的投递系统使用）。

    首次受理：按当前默认策略选定已发布版本，把规范化地址、策略修订号、选中版本、
    完整终点路径（或拒收 / 无路由 / 成环错误）作为一份判定持久化并返回。
    相同编号 + 相同规范化地址的并发或后续请求原样返回首次判定（含原错误状态），
    即使策略随后改变；编号相同而地址不同返回 409。
    无效地址（422）与未配置策略（409）不占用编号；选中版本失败整次失败，不切换版本。
    """
    address = body.address.strip().lower()
    if not is_valid_address(address):  # 无效地址不占用编号
        raise HTTPException(status_code=422, detail={"error": "invalid_address", "message": f"invalid address: {body.address!r}"})
    conflict = False
    try:
        with db.transaction() as conn:
            decision = store.get_decision(conn, body.delivery_id)
            if decision is not None:  # 已有判定：地址一致 → 重放；不一致 → 冲突
                conflict = decision["address"] != address
            else:
                policy = store.get_policy(conn)
                if policy is None:  # 回滚：未配置策略不占用编号
                    raise HTTPException(
                        status_code=409,
                        detail={"error": "policy_not_configured",
                                "message": "no delivery policy configured; specify an explicit version via /resolve"},
                    )
                chosen = select_version(address, policy["candidate_percent"])
                version = policy["baseline_version"] if chosen == "baseline" else policy["candidate_version"]
                ruleset = store.load_ruleset(conn, version)
                if ruleset is None:  # 防御：策略引用的版本必然已发布且不可变
                    raise HTTPException(status_code=404, detail={"error": "version_not_found", "version": version})
                try:
                    endpoints = resolve(address, ruleset)
                except ResolutionError as exc:  # 选中版本失败：整次失败并持久化，不切换版本
                    detail = exc.detail()
                    detail.update({
                        "delivery_id": body.delivery_id,
                        "policy_revision": policy["revision"],
                        "version": version,
                        "selection": chosen,
                    })
                    decision = store.save_decision(
                        conn, delivery_id=body.delivery_id, address=address, policy=policy,
                        selection=chosen, version=version, status="failed", endpoints=None, error=detail,
                    )
                else:
                    endpoint_docs = [
                        {"mailbox": mailbox, "paths": sorted(paths)}
                        for mailbox, paths in sorted(endpoints.items())
                    ]
                    decision = store.save_decision(
                        conn, delivery_id=body.delivery_id, address=address, policy=policy,
                        selection=chosen, version=version, status="ok", endpoints=endpoint_docs, error=None,
                    )
    except sqlite3.OperationalError as exc:  # 并发写冲突
        raise HTTPException(status_code=409, detail={"error": "write_conflict", "message": str(exc)})
    if conflict:
        raise HTTPException(
            status_code=409,
            detail={"error": "delivery_id_conflict", "delivery_id": body.delivery_id,
                    "message": f"delivery id {body.delivery_id!r} already submitted with a different address"},
        )
    if decision["status"] == "failed":  # 首次失败与重放都返回原错误状态
        raise HTTPException(status_code=422, detail=decision["error"])
    return _decision_doc(decision)


@app.get("/deliveries/{delivery_id}")
def get_delivery(delivery_id: str) -> dict:
    """查询已持久化的投递判定（含失败判定与原错误详情）；未知编号返回 404。"""
    with db.read_transaction() as conn:
        decision = store.get_decision(conn, delivery_id)
    if decision is None:
        raise HTTPException(status_code=404, detail={"error": "delivery_not_found", "delivery_id": delivery_id})
    return {
        "delivery_id": decision["delivery_id"],
        "address": decision["address"],
        "status": decision["status"],
        "policy_revision": decision["policy_revision"],
        "baseline_version": decision["baseline_version"],
        "candidate_version": decision["candidate_version"],
        "candidate_percent": decision["candidate_percent"],
        "selection": decision["selection"],
        "version": decision["version"],
        "endpoints": decision["endpoints"],
        "error": decision["error"],
        "created_at": decision["created_at"],
    }
