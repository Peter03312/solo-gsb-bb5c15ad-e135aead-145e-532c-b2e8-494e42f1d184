# Alias Router — 邮件投递前别名路由判定服务

基于 **Python + FastAPI + SQLite** 的 HTTP API：维护一份路由草稿（精确地址规则 + 域兜底规则），
发布为不可变版本后，对给定收件地址做逐跳别名展开，返回去重后的终点邮箱及各自到达路径。

## 功能要点

- **规则模型**：每条规则匹配一个完整邮箱地址（`exact`）或整个域（`domain`），动作二选一：
  - `reject`：拒收（不可带目标）；
  - `route`：指向若干目标，目标分为 `alias`（内部别名，继续逐跳解析）与 `mailbox`（终点邮箱，不再递归）。
- **解析语义**：每一跳先查精确规则、再查域兜底；展开所有分支后按终点去重，返回每个终点的全部到达路径。
  任一分支**拒收 / 无路由 / 成环** → 整次解析失败（422），不返回部分结果。
- **草稿编辑**：整体替换，必须携带所见修订号（乐观锁），冲突返回 409。
  无效地址、重复规则、空目标（`route` 无目标 / `reject` 带目标）在编辑期即拒绝（422）。
- **多人协作的按键合并**：`POST /draft/changes` 按规则键提交一组 `upsert`（设置）/ `delete`（删除）
  操作并携带所见草稿修订号；键与内容沿用整体替换的同一套规范化与编辑期校验。
  所见修订落后时，**仅当本次操作涉及的每个键相对所见快照在当前草稿中均未改变**
  （存在性与完整内容一致）才把整组操作合并并产生一个新修订；否则返回 `409`，附**全部冲突键**与
  当前修订号，草稿不变。重复操作键、删除当前不存在的规则、无效规则一律**整组拒绝（422）**；
  **无法取得所见修订的快照时明确拒绝合并**（409 `revision_snapshot_unavailable`）。
  核对与写入在单个 `BEGIN IMMEDIATE` 事务内原子完成。
- **草稿修订快照**：每次草稿变更（整体替换与按键合并）都留存一份完整规则快照，
  初始空草稿为修订 `0`；`GET /draft/revisions` 与 `GET /draft/revisions/{n}` 可逐一核对。
  旧版本程序生成的数据库没有历史快照，升级后不会臆造：只有升级后新发生的变更（及当前修订上的操作）
  可合并，引用缺失快照的旧所见修订号会被明确拒绝。
- **发布**：在单个事务（`BEGIN IMMEDIATE`）内原子核对修订号，并校验**所有规则目标可解析且无环**；
  成功则生成不可变版本，失败则保持旧版本不变。
- **历史版本**：所有发布版本永久可查，按任意历史版本解析可复现当时的结果。
- **发布前预览**：`POST /preview` 在同一只读快照内对比当前草稿与某个已发布版本，
  列出投递结果有变化的入口地址与域兜底类别（附两侧完整结果）；只读，不改动草稿或历史版本。
- **默认投递策略（灰度）**：管理员用 `PUT /policy` 提交**基线版本、候选版本、候选流量比例（整数 0～100）
  与所见策略修订号**；两个版本都必须是已发布版本，在单个 `BEGIN IMMEDIATE` 事务内核验修订号并保存，
  修订号冲突（409）或引用未发布版本（422）都不会覆盖原策略。
  `POST /deliver` 只输入收件地址：按**规范化地址稳定分桶**（SHA-256，仅依赖地址本身），
  同一地址在同一策略下总选同一版本；`0%` / `100%` 分别全选基线 / 候选。
  选中版本中的**拒收 / 无路由 / 成环仍整次失败，不自动改用另一版本**；
  未配置策略时明确报错（409），**不使用草稿或最新版本兜底**。
  策略只决定“默认用哪个已发布版本”，不影响显式版本解析，草稿与历史版本也不会被策略改写。
- **幂等投递判定**：`POST /deliveries` 由调用方提交**唯一投递编号 + 收件地址**（供可能超时重试的
  投递系统使用）。首次受理时按当前默认策略选定已发布版本，把**规范化地址、策略修订号、选中版本、
  完整终点路径（或拒收 / 无路由 / 成环错误）作为一份判定持久化**并返回。
  相同编号 + 相同规范化地址的并发或后续请求**原样返回首次判定（含原错误状态）**，即使策略随后改变；
  编号相同而地址不同返回 409；**无效地址或未配置策略不占用编号**。
  `GET /deliveries/{id}` 可随时查询已持久化的判定。
- **历史投递判定核对报表**：`GET /deliveries/report` 按**带时区的起止时刻（左闭右开）与可选策略
  修订号**筛选已持久化的判定，返回按**选中版本 × 分流侧（基线/候选）× 结果（成功或失败原因）**
  分组的数量，并分页列出投递编号、规范化地址、策略修订号、选中版本、分流侧与状态或错误类型；
  已持久化的失败同样计入，同一编号只计一次。首次查询在单只读快照内固定**截至当时已提交判定的
  边界**并随 `next_cursor` 返回；后续翻页只凭游标，沿用同一筛选与边界——并发新投递不进入旧报表，
  分组总数与明细一致，页间不漏不重。空结果返回零计数与空页；无效时间窗、非法页大小、无效游标
  均明确拒绝（422）。

## 一键启动（Docker Compose）

```bash
docker compose up --build
```

服务监听 `0.0.0.0:8000`，SQLite 数据落在命名卷 `router-data`（容器内 `/data/router.db`），
首次启动自动建表，无需手工初始化。

也可以只用 Docker：

```bash
docker build -t alias-router .
docker run -p 8000:8000 -v router-data:/data alias-router
```

## 本地开发启动

```bash
pip install -r requirements-dev.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000   # 默认库文件 ./router.db
pytest tests/ -q                                   # 运行测试
```

## 配置（环境变量）

| 变量          | 默认值（容器内）     | 说明                 |
| ------------- | -------------------- | -------------------- |
| `ROUTER_DB`   | `/data/router.db`    | SQLite 数据库文件路径 |
| `ROUTER_HOST` | `0.0.0.0`            | 监听地址             |
| `ROUTER_PORT` | `8000`               | 监听端口             |

地址与端口：`http://localhost:8000`（Compose 已映射 8000:8000），交互式文档见 `http://localhost:8000/docs`。

## API 一览

| 方法 | 路径              | 说明                                   |
| ---- | ----------------- | -------------------------------------- |
| GET  | `/health`         | 健康检查                               |
| GET  | `/draft`          | 查看草稿（含当前修订号）               |
| PUT  | `/draft`          | 整体替换草稿，需携带所见修订号         |
| POST | `/draft/changes`  | 多人协作：按规则键合并一组 upsert/delete，需所见修订号 |
| GET  | `/draft/revisions` | 列出全部草稿修订（每次变更的快照）   |
| GET  | `/draft/revisions/{n}` | 查看某个草稿修订的完整规则快照     |
| POST | `/publish`        | 核对修订号 + 全量校验后发布不可变版本  |
| GET  | `/versions`       | 列出全部已发布版本                     |
| GET  | `/versions/{n}`   | 查看某个历史版本的完整规则             |
| POST | `/resolve`        | 按指定版本解析收件地址（显式版本，兼容） |
| POST | `/preview`        | 预览当前草稿相对基线版本的投递变化     |
| GET  | `/policy`         | 查看当前默认投递策略（未配置时 404）   |
| PUT  | `/policy`         | 配置基线/候选版本与候选比例，需策略修订号 |
| POST | `/deliver`        | 默认投递：按策略对收件地址稳定分桶解析 |
| POST | `/deliveries`     | 幂等投递：投递编号 + 地址，首次判定持久化、重试原样返回 |
| GET  | `/deliveries/report` | 历史判定核对报表：时间窗 + 分组计数 + 游标分页     |
| GET  | `/deliveries/{id}` | 按投递编号查询已持久化的判定（含失败） |

## 调用示例

### 1. 编辑草稿（当前修订号 0）

```bash
curl -X PUT http://localhost:8000/draft -H 'Content-Type: application/json' -d '{
  "revision": 0,
  "rules": [
    {"match_type": "exact", "match_key": "boss@corp.example.com", "action": "route", "targets": [
      {"kind": "alias",   "value": "assistant@corp.example.com"},
      {"kind": "mailbox", "value": "boss-backup@mail.example.com"}
    ]},
    {"match_type": "exact", "match_key": "assistant@corp.example.com", "action": "route", "targets": [
      {"kind": "mailbox", "value": "alice@mail.example.com"},
      {"kind": "alias",   "value": "team@corp.example.com"}
    ]},
    {"match_type": "exact", "match_key": "team@corp.example.com", "action": "route", "targets": [
      {"kind": "mailbox", "value": "alice@mail.example.com"},
      {"kind": "mailbox", "value": "bob@mail.example.com"}
    ]},
    {"match_type": "exact", "match_key": "blocked@corp.example.com", "action": "reject"},
    {"match_type": "domain", "match_key": "corp.example.com", "action": "route", "targets": [
      {"kind": "mailbox", "value": "catchall@mail.example.com"}
    ]}
  ]}'
# => {"revision": 1, "rules": [...]}
```

修订号过期时返回 `409`：`{"detail": {"error": "revision_conflict", "current_revision": 1, ...}}`。

### 1.1 多人协作：按规则键合并（POST /draft/changes）

多个维护者各自只改动自己负责的规则键时，不必整体回传整份草稿。提交一组操作，每个操作一个规则键：

- `{"op": "upsert", "match_type": ..., "match_key": ..., "action": ..., "targets": ...}`：设置一条规则
  （不存在则新增，已存在则整条覆盖）；规则字段形状与 `/draft` 的规则完全一致。
- `{"op": "delete", "match_type": ..., "match_key": ...}`：删除一条规则，**不可**携带 `action` / `targets`。

规则键（去空白、小写；`exact` 须为合法地址、`domain` 须为合法域）与规则内容沿用上面同一套
规范化与编辑期校验。请求同样携带**所见草稿修订号**：

```bash
curl -X POST http://localhost:8000/draft/changes -H 'Content-Type: application/json' -d '{
  "revision": 1,
  "changes": [
    {"op": "upsert", "match_type": "exact", "match_key": " BOSS@CORP.EXAMPLE.COM ", "action": "route",
     "targets": [{"kind": "alias", "value": "assistant@corp.example.com"}]},
    {"op": "delete", "match_type": "domain", "match_key": "old-corp.example.com"}
  ]}'
# => {"revision": 2, "rules": [...合并后的整份草稿...]}
```

**落后修订的合并判定**（事务内原子完成）：

- 若所见修订已落后，服务取出所见修订的完整快照，逐个比对本次操作涉及的键：
  该键在**所见快照与当前草稿中的完整规则一致**（两边都不存在也算一致）才算“未改变”。
  - 涉及键**全部未改变** → 整组操作合并进当前草稿，产生一个新修订（与他人不相交的改动互不阻塞）；
  - 任一涉及键已被他人改动（内容变化、被删除或被新增）→ `409`，返回**全部冲突键**与当前修订号，
    草稿保持不变，整组操作不落任何一项：

    ```json
    {"detail": {"error": "revision_conflict", "current_revision": 2,
                "conflict_keys": [{"match_type": "exact", "match_key": "boss@corp.example.com"}],
                "message": "revision conflict: 1 involved key(s) changed since the seen revision"}}
    ```

  拿到冲突后，调用方重新 `GET /draft` 拉取当前修订号，解决冲突后带新修订号重试即可。

**整组拒绝（422，不消耗修订号、草稿不变）**：

- 重复操作键（规范化后相同，即使一个 upsert 一个 delete）→
  `{"error": "duplicate_change_key", ...}`；
- 删除当前草稿中不存在的规则 → `{"error": "rule_not_found", "match_type": ..., "match_key": ...}`；
- 无效规则（非法地址 / 域、`route` 无目标、`reject` 带目标、非法目标地址等）→
  `{"error": "invalid_rule", ...}`；`changes` 为空同样 422。

**快照缺失**：所见修订号既不是当前修订、又取不到它的修订快照（如旧版程序遗留的数据库未留存该修订）
→ `409 {"error": "revision_snapshot_unavailable", "revision": ..., "current_revision": ...}`，
绝不按当前状态盲合并。

并发提交在单个 `BEGIN IMMEDIATE` 事务内完成“核对 + 写入”：不相交键的两组操作串行化后各自成功，
相交键的后到者得到冲突；任何失败都整体回滚。合并后的草稿与整体替换后的草稿完全等价，
照旧走 `/publish` 发布，再用 `/resolve`、`/deliver` 等接口解析。

### 1.2 草稿修订快照（GET /draft/revisions）

每次草稿变更（`PUT /draft` 或 `POST /draft/changes`）都产生一个修订号并留存完整规则快照，
初始空草稿为修订 `0`：

```bash
curl http://localhost:8000/draft/revisions
# => {"revisions": [{"revision": 0, "rule_count": 0, "created_at": "..."},
#                   {"revision": 1, "rule_count": 5, "created_at": "..."}, ...]}

curl http://localhost:8000/draft/revisions/1
# => {"revision": 1, "rules": [...该修订的完整规则...]}
```

未知修订号返回 `404 {"error": "draft_revision_not_found", "revision": n}`。

### 2. 发布（原子核对修订号 + 校验目标可解析、无环）

```bash
curl -X POST http://localhost:8000/publish -H 'Content-Type: application/json' -d '{"revision": 1}'
# => {"version": 1, "source_revision": 1, "rule_count": 5}
```

校验失败（如目标别名无路由、成环）返回 `422` 且旧版本不受影响：

```json
{"detail": {"error": "validation_failed", "problems": [{"seed": "...", "error": "cycle", "chain": [...]}]}}
```

### 3. 解析收件地址

```bash
curl -X POST http://localhost:8000/resolve -H 'Content-Type: application/json' \
  -d '{"address": "boss@corp.example.com", "version": 1}'
```

```json
{
  "address": "boss@corp.example.com",
  "version": 1,
  "endpoints": [
    {"mailbox": "alice@mail.example.com", "paths": [
      ["boss@corp.example.com", "assistant@corp.example.com", "alice@mail.example.com"],
      ["boss@corp.example.com", "assistant@corp.example.com", "team@corp.example.com", "alice@mail.example.com"]
    ]},
    {"mailbox": "bob@mail.example.com", "paths": [
      ["boss@corp.example.com", "assistant@corp.example.com", "team@corp.example.com", "bob@mail.example.com"]
    ]},
    {"mailbox": "boss-backup@mail.example.com", "paths": [
      ["boss@corp.example.com", "boss-backup@mail.example.com"]
    ]}
  ]
}
```

域兜底（无精确规则时）：`nobody@corp.example.com` → `catchall@mail.example.com`。

任一分支失败则整次失败（`422`，无部分结果）：

```bash
curl -X POST http://localhost:8000/resolve -H 'Content-Type: application/json' \
  -d '{"address": "blocked@corp.example.com", "version": 1}'
# => {"detail": {"error": "rejected", "address": "blocked@corp.example.com", "chain": [...]}}
```

`error` 取值：`rejected`（命中拒收规则）、`no_route`（别名无路由）、`cycle`（成环）。

### 4. 历史版本查询与复现

```bash
curl http://localhost:8000/versions        # 全部版本
curl http://localhost:8000/versions/1      # 某版本的完整规则快照
```

草稿后续修改、发布新版本后，仍可用 `"version": 1` 调 `/resolve` 复现当时的判定结果。

### 5. 发布前预览：草稿 vs 基线版本

管理员在发布前用 `POST /preview` 对比**当前草稿**与某个**已发布版本**的投递差异。
请求携带基线版本号与所见草稿修订号：

```bash
curl -X POST http://localhost:8000/preview -H 'Content-Type: application/json' \
  -d '{"base_version": 1, "revision": 2}'
```

服务在同一只读事务快照内取出两份规则集（不改动草稿或历史版本），沿用与 `/resolve`
相同的精确优先、逐跳展开、整次失败语义做全量对比，只列出**结果有变化**的项：

- `changed_addresses`：两侧全部**精确规则地址与别名目标**（并集）中结果有变化的地址。
  别名下游的变化会追溯到受影响的入口——例如 `team@` 改投后，`boss@`、`assistant@`
  这些经由它的入口也会列出。每项附 `base` / `draft` 两侧的完整结果：
  成功为 `{"status": "ok", "endpoints": [...]}`（形状同 `/resolve` 响应），
  失败为 `{"status": "failed", "error": {...}}`（形状同 `/resolve` 的 422 详情）。
- `changed_domain_fallbacks`：两侧任一处出现过域规则的每个域，排除上述具体地址后的
  **兜底地址类别**。每类用一个避开两侧精确地址与别名目标的合成示例地址
  （如 `_preview-sample@corp.example.com`）代表，同样附两侧完整结果。

比较时**路径排列顺序不计差异**（按路径集合比较），仅终点集合、到达路径或
拒收 / 无路由 / 成环结果不同才列出；无变化时两个列表均为空。
草稿尚未通过发布校验（如有环、目标无路由）时预览仍正常返回，对应入口展示失败结果。

```json
{
  "base_version": 1,
  "revision": 2,
  "changed_addresses": [
    {
      "address": "team@corp.example.com",
      "base":  {"status": "ok", "endpoints": [{"mailbox": "alice@mail.example.com", "paths": [["team@corp.example.com", "alice@mail.example.com"]]}, {"mailbox": "bob@mail.example.com", "paths": [["team@corp.example.com", "bob@mail.example.com"]]}]},
      "draft": {"status": "ok", "endpoints": [{"mailbox": "carol@mail.example.com", "paths": [["team@corp.example.com", "carol@mail.example.com"]]}]}
    }
  ],
  "changed_domain_fallbacks": [
    {
      "domain": "corp.example.com",
      "sample": "_preview-sample@corp.example.com",
      "base":  {"status": "ok", "endpoints": [{"mailbox": "catchall@mail.example.com", "paths": [["_preview-sample@corp.example.com", "catchall@mail.example.com"]]}]},
      "draft": {"status": "ok", "endpoints": [{"mailbox": "new-catchall@mail.example.com", "paths": [["_preview-sample@corp.example.com", "new-catchall@mail.example.com"]]}]}
    }
  ]
}
```

错误契约与既有接口一致：修订号过期返回 `409`（`revision_conflict`，附当前修订号），
基线版本不存在返回 `404`（`version_not_found`）。

> 上例为节选：该场景下 `boss@`、`assistant@` 等经由 `team@` 的入口也会一并出现在
> `changed_addresses` 中。

## 默认投递策略：已发布版本间的灰度

管理员可以让某个**已发布候选版本**按流量比例可控地成为默认投递结果，基线版本承接其余流量。

### 6. 配置策略（基线 / 候选 / 比例 / 策略修订号）

```bash
curl -X PUT http://localhost:8000/policy -H 'Content-Type: application/json' -d '{
  "baseline_version": 1,
  "candidate_version": 2,
  "candidate_percent": 20,
  "revision": 0
}'
```

- 只接受**已发布版本**：`baseline_version` / `candidate_version` 任一不存在（或指向草稿）→
  `422 {"error": "version_not_published", ...}`，事务回滚，原策略不变。
- `revision` 为客户端**所见策略修订号**；从未配置时修订号为 `0`，每次成功更新 `+1`。
  修订号过期返回 `409`（`revision_conflict`，附 `current_revision`），冲突不覆盖原策略。
- `candidate_percent` 为整数 `0～100`（请求体层强制），越界返回 `422`。

成功响应即当前策略：

```json
{"revision": 1, "baseline_version": 1, "candidate_version": 2, "candidate_percent": 20, "updated_at": "2026-10-04T01:16:03+00:00"}
```

查看当前策略：`GET /policy`（未配置时 `404 {"error": "policy_not_configured", "revision": 0}`）。

策略修订号与草稿修订号相互独立；改策略不会修改草稿、历史版本，也不会让任何已发布版本被改写。

### 7. 默认投递：只给收件地址

```bash
curl -X POST http://localhost:8000/deliver -H 'Content-Type: application/json' \
  -d '{"address": "boss@corp.example.com"}'
```

服务先把地址规范化（去空白、小写），再对地址本身做 SHA-256 稳定分桶（桶号 0–99）：
桶号 `< candidate_percent` 选候选，否则选基线。**同一地址在同一策略下永远选同一版本**；
调整比例只会移动分桶边界（地址随比例单调地从基线侧进入候选侧），不会来回抖动。
`0%` 时所有地址选基线，`100%` 时所有地址选候选。

```json
{
  "address": "boss@corp.example.com",
  "policy_revision": 1,
  "baseline_version": 1,
  "candidate_version": 2,
  "candidate_percent": 20,
  "selection": "candidate",
  "version": 2,
  "endpoints": [
    {"mailbox": "alice@mail.example.com", "paths": [["boss@corp.example.com", "alice@mail.example.com"]]}
  ]
}
```

`endpoints` 的形状与 `/resolve` 完全一致（原有终点与到达路径）；`selection` 为
`"baseline"` / `"candidate"`，`version` 为实际选中的版本号。

失败约定：

- **未配置策略** → `409 {"error": "policy_not_configured", ...}`；不会使用草稿或最新发布版本兜底。
- **选中版本**解析出**拒收 / 无路由 / 成环** → 与 `/resolve` 相同的 `422`
  （`error` 仍为 `rejected` / `no_route` / `cycle`，附原有 `address`、`chain`），
  **整次失败，不自动改用另一版本**；错误详情额外附 `version`、`selection`、`policy_revision`。
- 无效地址 → `422 {"error": "invalid_address", ...}`。

```bash
# 100% 候选后，命中候选拒收规则：失败，不会退回基线
curl -X POST http://localhost:8000/deliver -H 'Content-Type: application/json' \
  -d '{"address": "blocked@corp.example.com"}'
# => 422 {"detail": {"error": "rejected", ..., "policy_revision": 2, "version": 2, "selection": "candidate"}}
```

### 8. 显式版本解析保持兼容

`POST /resolve` 的请求（必须带 `version`）、成功响应与错误约定（`404 version_not_found`、
`422` 的 `rejected` / `no_route` / `cycle` / `invalid_address`）完全不变，不受任何策略影响；
草稿与历史版本同样不随策略变更而改写。需要强制指定版本复现结果时继续使用 `/resolve`。

## 幂等投递判定：超时重试安全的提交

投递系统可能因超时重试同一封邮件的路由判定。`POST /deliveries` 在 `/deliver` 的策略分桶之上
增加**调用方唯一投递编号**：首次受理时把完整判定持久化，之后相同编号的重试 / 并发请求
原样返回首次判定，绝不因策略变更或重试时机不同而给出不同结果。

### 9. 提交投递判定（投递编号 + 收件地址）

```bash
curl -X POST http://localhost:8000/deliveries -H 'Content-Type: application/json' \
  -d '{"delivery_id": "msg-2026-10-04-0001", "address": "boss@corp.example.com"}'
```

```json
{
  "delivery_id": "msg-2026-10-04-0001",
  "address": "boss@corp.example.com",
  "policy_revision": 1,
  "baseline_version": 1,
  "candidate_version": 2,
  "candidate_percent": 20,
  "selection": "candidate",
  "version": 2,
  "endpoints": [
    {"mailbox": "alice@mail.example.com", "paths": [["boss@corp.example.com", "alice@mail.example.com"]]}
  ]
}
```

响应在 `/deliver` 的字段（策略快照、`selection`、`version`、`endpoints`）之外增加 `delivery_id`；
地址同样先规范化（去空白、小写）再分桶与落库。

**重试 / 并发重放**：相同 `delivery_id` 且规范化地址相同的请求（无论并发还是稍后重试，
也无论策略随后如何调整）都返回与首次**完全相同**的响应——同样的状态码与响应体：

```bash
# 即使 PUT /policy 已把比例改为 0%，重放仍返回首次选中的版本与结果
curl -X POST http://localhost:8000/deliveries -H 'Content-Type: application/json' \
  -d '{"delivery_id": "msg-2026-10-04-0001", "address": "  BOSS@CORP.EXAMPLE.COM "}'
# => 与首次响应逐字段相同（version 仍为 2，policy_revision 仍为 1）
```

**失败判定同样持久化**：选中版本拒收 / 无路由 / 成环时，首次与重放都返回相同的 `422`
（`error` 为 `rejected` / `no_route` / `cycle`，附 `delivery_id`、`policy_revision`、
`version`、`selection`），**整次失败，不切换版本**：

```json
{"detail": {"error": "rejected", "address": "blocked@corp.example.com", "chain": ["blocked@corp.example.com"],
            "delivery_id": "msg-2026-10-04-0002", "policy_revision": 1, "version": 2, "selection": "candidate",
            "message": "blocked@corp.example.com rejected by rule"}}
```

**编号冲突**：`delivery_id` 相同而规范化地址不同 → `409`
`{"error": "delivery_id_conflict", "delivery_id": "..."}`，原判定不被改写。

**编号不被无效请求占用**：无效地址（`422 invalid_address`）与未配置策略
（`409 policy_not_configured`）都不会写入判定，修正后可用同一编号重新提交。

### 10. 查询已持久化的判定

```bash
curl http://localhost:8000/deliveries/msg-2026-10-04-0001
```

```json
{
  "delivery_id": "msg-2026-10-04-0001",
  "address": "boss@corp.example.com",
  "status": "ok",
  "policy_revision": 1,
  "baseline_version": 1,
  "candidate_version": 2,
  "candidate_percent": 20,
  "selection": "candidate",
  "version": 2,
  "endpoints": [
    {"mailbox": "alice@mail.example.com", "paths": [["boss@corp.example.com", "alice@mail.example.com"]]}
  ],
  "error": null,
  "created_at": "2026-10-04T01:16:03+00:00"
}
```

`status` 为 `ok` / `failed`；失败判定的 `error` 为首次 `422` 的完整详情，`endpoints` 为 `null`。
未知编号返回 `404 {"error": "delivery_not_found", ...}`。

> 既有 `/deliver`、`/resolve` 的请求、响应与错误约定完全不变：`/deliver` 仍只做即时分桶解析，
> 不产生持久化判定；需要强制指定版本复现时继续使用 `/resolve`。

## 历史投递判定核对报表：时间窗 + 分组计数 + 游标分页

运营核对历史投递判定用 `GET /deliveries/report`：按**带时区的起止时刻**（左闭右开
`[start, end)`，作用于判定的受理时刻 `created_at`）与**可选策略修订号**筛选已持久化的判定，
返回分组数量与分页明细。已持久化的失败判定同样计入；`delivery_id` 是主键，同一编号只计一次。

### 11. 首次查询：固定筛选与快照边界

```bash
curl 'http://localhost:8000/deliveries/report?start=2026-10-01T00:00:00%2B08:00&end=2026-11-01T00:00:00%2B08:00&policy_revision=1&page_size=2'
```

- `start` / `end` 必填，必须是**带时区**的 ISO 8601 时刻（`Z` 或 `+08:00` 等偏移均可，
  服务规范化为 UTC 后比较）；窗口**左闭右开**：恰好等于 `start` 的判定计入，恰好等于 `end` 的不计入。
- `policy_revision` 可选：只核对某个策略修订号下受理的判定；缺省不过滤。
- `page_size` 可选，默认 50，合法范围 1～500。

```json
{
  "window": {"start": "2026-09-30T16:00:00+00:00", "end": "2026-10-31T16:00:00+00:00", "policy_revision": 1},
  "page_size": 2,
  "total": 4,
  "groups": [
    {"version": 1, "selection": "baseline",  "outcome": "ok",       "count": 1},
    {"version": 2, "selection": "candidate", "outcome": "no_route", "count": 1},
    {"version": 2, "selection": "candidate", "outcome": "ok",       "count": 1},
    {"version": 2, "selection": "candidate", "outcome": "rejected", "count": 1}
  ],
  "items": [
    {"delivery_id": "msg-0001", "address": "shared@corp.example.com", "policy_revision": 1,
     "version": 2, "selection": "candidate", "status": "ok", "error_type": null,
     "created_at": "2026-10-04T01:16:03+00:00"},
    {"delivery_id": "msg-0002", "address": "blocked@corp.example.com", "policy_revision": 1,
     "version": 2, "selection": "candidate", "status": "failed", "error_type": "rejected",
     "created_at": "2026-10-04T01:16:03+00:00"}
  ],
  "next_cursor": "eyJ2IjoxLCJzdGFydCI6..."
}
```

- `groups` 按**选中版本 × 分流侧（`baseline` / `candidate`）× 结果**分组计数：
  成功为 `"outcome": "ok"`，失败为失败原因 `"rejected"` / `"no_route"` / `"cycle"`。
- `items` 明细含投递编号、规范化地址、策略修订号、选中版本、分流侧与状态或错误类型
  （`status` 为 `ok` / `failed`，`error_type` 仅失败时非空），按受理顺序排列。
- `total` 为筛选下的判定总数，恒等于各组计数之和；`groups`、`total` 与全部页的明细
  在**同一快照边界**上计算，三者必然一致。
- 首次查询在单只读事务内固定“**截至当时已提交判定的边界**”；空结果返回
  `"total": 0`、`"groups": []`、`"items": []`、`"next_cursor": null`。

### 12. 后续翻页：只凭游标，沿用同一筛选与边界

```bash
curl 'http://localhost:8000/deliveries/report?cursor=eyJ2IjoxLCJzdGFydCI6...'
# 翻页时允许调整页大小：...&page_size=100
```

- `next_cursor` 为 `null` 表示已到最后一页；否则凭它取下一页。
- 游标已携带首次查询的筛选（起止时刻、策略修订号）与快照边界，**翻页时不要再传
  `start` / `end` / `policy_revision`**（同传会被拒绝）；响应中的 `window` 回显实际沿用的筛选。
- **并发新投递不进入旧报表**：首次查询之后受理的判定不会出现在该游标的任何后续页，
  各页的 `groups` / `total` 也保持不变；要纳入新判定请重新发起首次查询。
- 页间按唯一单调的内部序号键集分页：不漏记、不重复；各页明细拼接即为完整筛选结果。

### 13. 明确拒绝（422）

| 场景 | 响应 |
| ---- | ---- |
| 缺 `start` / `end`、不带时区、无法解析、`start >= end` | `{"error": "invalid_time_window", ...}` |
| `page_size` 超出 1～500 | `{"error": "invalid_page_size", ...}` |
| 游标无法解码、形状 / 版本不符、与筛选参数同传 | `{"error": "invalid_cursor", ...}` |

> 该报表只读取已持久化的判定，不改动任何数据；`POST /deliveries` 的幂等判定、
> `GET /deliveries/{id}` 的按编号查询与原样重放语义完全不变。

## 项目结构

```
app/
  config.py    # 环境变量配置
  db.py        # SQLite 连接、建表、BEGIN IMMEDIATE 事务、只读快照事务
  routing.py   # 地址校验、规则集、逐跳解析、发布前全量校验
  traffic.py   # 默认投递的稳定分桶：规范化地址 → 0–99 桶 → 基线/候选
  preview.py   # 发布前预览：草稿 vs 基线版本的投递差异对比（纯内存，不落库）
  reporting.py # 核对报表：带时区时间窗（左闭右开）校验、页大小校验、翻页游标编解码
  models.py    # 请求模型与编辑期校验（无效地址/重复规则/空目标/按键合并操作组）
  store.py     # 草稿、修订快照、发布版本、默认投递策略、幂等投递判定与核对报表的持久化与查询
  main.py      # FastAPI 路由（/draft、/draft/changes、/resolve、/preview、/policy、/deliver、/deliveries、/deliveries/report 等）
tests/         # pytest：API 全流程 + 解析单元测试 + 发布前预览 + 灰度策略 + 幂等投递判定 + 按键合并与修订快照 + 核对报表
Dockerfile / docker-compose.yml
```
