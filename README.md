# 本地数据导出风险审查 API

用 Python / FastAPI 实现的本地 HTTP 服务。输入一张**可乱序、至多 60 个节点**的数据加工图和一个
导出节点，服务沿依赖关系逐列追踪**敏感来源**，对导出节点的每一列给出审查结论：

* **内容影响（content）**：敏感源列的值流入了该列；
* **行影响（row）**：敏感源列影响了结果里“出现哪些行”，该影响会落到结果的**每一列、每一行**。

两类原因只要存在其一，该导出列即判 `blocked: true`，并分别列出源头节点 ID 与列名。

服务还提供 `POST /api/revise`：在审查结论基础上把图修订成一张**可再次提交、复审判安全**的
导出图（只允许在导出末尾追加一个投影节点），详见下文“安全修订”。

此外提供 `POST /api/prune`：按选定的导出列把图**精简**为只保留这些列及其全部依赖祖先的
等价图——删除无关分支，并从选定列反向传播需求、裁剪导出及各投影/连接的无用输出（共享分支
需求取并集）；最终导出只含选定列（沿用原列序），所选列的内容与行影响原因与原图完全一致，
详见下文“按导出列精简”。

还提供 `POST /api/rename`：把一个**源节点的若干列批量改名**，同时让图内所有引用仍指向
原来的列（筛选引用随输入改名并继续透传；投影 `copy`、连接键与输出引用随各自输入改名，投影
输出名与连接别名不变）。不增删任何节点或列，复审阻断结论不变，详见下文“源列批量改名”。

## 风险传播规则

| 节点 | 行为 | 内容影响 | 行影响 |
|---|---|---|---|
| `source` | 声明互异列名与 `sensitive` 标记 | 敏感列自身的来源 `(节点, 列)` | 初始为空 |
| `projection` | 只保留明确列出的输出列；每列 `copy` 上游列或以 `constant` 常量替换 | `copy` 继承上游列；`constant` **清除**该列内容来源 | 原样保留（投影/替换不能消除行影响） |
| `filter` | 声明条件 `references` 引用的列，保留全部输入列 | 输入列原样保留 | 并入所有被引用列的内容来源；并继承上游行影响 |
| `join` | 等值连接：左右输入、`left_key`/`right_key`、输出列与互异别名 | 每个输出列继承对应侧列的内容来源 | 合并两侧已有行影响，**并并入左右连接键的敏感内容来源** |

要点：筛选引用敏感列、或连接键带敏感来源时，敏感来源影响结果的**每一行**；之后的投影、
常量替换都无法消除；连接时两侧行影响取并集。

### 整次请求拒绝（HTTP 400）

出现以下情况拒绝整次请求，不给出部分审查结果：

* 节点 ID 重复（`DUPLICATE_NODE`）；
* 列名冲突：source 列名、projection 输出列名、filter 引用名、join 输出别名在各自节点内重复（`COLUMN_CONFLICT`）；
* 无效引用：输入节点/被复制列/筛选列/连接键/连接输出列/导出节点不存在（`INVALID_REFERENCE`）；
* 依赖图含环，包括自环（`CYCLE`）。

请求体不符合 JSON Schema（未知字段、同时写 `copy` 与 `constant`、节点数超过 60 等）返回 HTTP 422。

`POST /api/revise` 复用上述全部校验（重复 ID、列名冲突、无效引用、环、超过 60 节点等同样拒绝），
额外把 `allowed_replacements` 的列名重复判为 422，把允许列不存在及各类不可修订情形判为 400。

`POST /api/rename` 同样先校验整张原图（无关分支的错误也拒绝），再执行改名；源节点不存在、
原列缺失、改名后源列名重复或复审不一致判为 400，改名列表为空、原列名重复/为空、新名为空等
schema 问题判为 422。

## 单条命令启动

> 端口：**8080**

```bash
docker compose up --build
```

后台运行：

```bash
docker compose up --build -d
```

健康检查：

```bash
curl http://localhost:8080/health
# {"status":"ok"}
```

不使用 Docker 时：

```bash
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8080
```

## HTTP 调用示例

### 请求

```
POST /api/review
Content-Type: application/json
```

```bash
curl -s -X POST http://localhost:8080/api/review \
  -H 'Content-Type: application/json' \
  -d '{
  "nodes": [
    {
      "id": "users",
      "type": "source",
      "columns": [
        {"name": "uid", "sensitive": false},
        {"name": "email", "sensitive": true},
        {"name": "country", "sensitive": false}
      ]
    },
    {
      "id": "only_cn",
      "type": "filter",
      "input": "users",
      "references": ["country"]
    },
    {
      "id": "export_users",
      "type": "projection",
      "input": "only_cn",
      "columns": [
        {"name": "user_id", "copy": "uid"},
        {"name": "mail", "copy": "email"},
        {"name": "bucket", "constant": "ALL"}
      ]
    }
  ],
  "export": "export_users"
}'
```

也可以直接用仓库里的示例：

```bash
curl -s -X POST http://localhost:8080/api/review \
  -H 'Content-Type: application/json' \
  --data @examples/request.json
```

### 响应

```json
{
  "export": "export_users",
  "blocked": true,
  "columns": [
    {"name": "user_id", "blocked": false, "content_reasons": [], "row_reasons": []},
    {
      "name": "mail",
      "blocked": true,
      "content_reasons": [{"node_id": "users", "column": "email"}],
      "row_reasons": []
    },
    {"name": "bucket", "blocked": false, "content_reasons": [], "row_reasons": []}
  ]
}
```

### 行影响示例：筛选敏感列后，连常量列也被阻断

```bash
curl -s -X POST http://localhost:8080/api/review \
  -H 'Content-Type: application/json' \
  -d '{
  "nodes": [
    {"id": "s", "type": "source",
     "columns": [{"name": "id_card", "sensitive": true}, {"name": "score"}]},
    {"id": "f", "type": "filter", "input": "s", "references": ["id_card"]},
    {"id": "e", "type": "projection", "input": "f",
     "columns": [{"name": "score", "copy": "score"}, {"name": "lit", "constant": 1}]}
  ],
  "export": "e"
}'
```

`score`（非敏感复制列）与 `lit`（常量列）的 `row_reasons` 都包含
`{"node_id":"s","column":"id_card"}`，均被阻断。

### 连接示例：敏感连接键影响连接结果每一行

```bash
curl -s -X POST http://localhost:8080/api/review \
  -H 'Content-Type: application/json' \
  -d '{
  "nodes": [
    {"id": "orders", "type": "source",
     "columns": [{"name": "uid_enc", "sensitive": true}, {"name": "amount"}]},
    {"id": "users", "type": "source",
     "columns": [{"name": "uid"}, {"name": "name"}]},
    {"id": "j", "type": "join",
     "left_input": "orders", "right_input": "users",
     "left_key": "uid_enc", "right_key": "uid",
     "outputs": [
       {"alias": "amt", "side": "left", "column": "amount"},
       {"alias": "uname", "side": "right", "column": "name"}
     ]}
  ],
  "export": "j"
}'
```

两个输出列本身均不含敏感内容，但都因敏感连接键 `orders.uid_enc` 的行影响而阻断。

## 安全修订：POST /api/revise

把审查结论转成一张**可再次提交的安全导出图**。请求体在审查请求基础上增加
`allowed_replacements`：允许被替换为常量 `null` 的导出列名列表（**必须互异**，可传空数组；
列名非空）。整张图仍按 `/api/review` 的原规则校验，原有错误行为不变。

修订规则：

1. **原图已安全**：原样返回 `nodes`、原 `export`，**不新增节点**，`replaced_columns` 为空。
2. **原图被阻断且可修订**：只允许在导出节点**末尾追加恰好一个 `projection` 节点**，
   * 列名、列顺序、行集合完全保持（输入即原导出节点，不做筛选/连接）；
   * 仅把**带有敏感内容来源且在允许列表内**的列置为常量 `null`；
   * 其余列（含获准但本身干净的列、原常量列）一律按同名 `copy` 复制；
   * **不修改任何原节点**，追加的投影 ID 不会与原节点冲突（冲突时自动换名）。
3. **不可修订**（返回 HTTP 400，**不返回部分改好的图**）：
   * 导出存在**行影响**（投影无法消除行影响）：`UNREVISABLE_ROW_INFLUENCE`；
   * 任一阻断列（带敏感内容来源）**不在允许列表**：`UNREVISABLE_COLUMN_NOT_ALLOWED`；
   * 追加投影后节点数**超过 60**：`UNREVISABLE_NODE_LIMIT`；
   * 允许列表中的列名在导出节点上**不存在**（原图安全时同样检查）：
     `REPLACEMENT_COLUMN_NOT_FOUND`。

允许列表列名重复、为空字符串、字段类型不符等 schema 问题返回 HTTP 422。

成功响应包含完整 `nodes`、更新后的 `export`、实际替换列 `replaced_columns`
（只含真正被置空的列，按列顺序）、以及用原规则对新导出节点**重审**的结果 `review`；
该图可直接再 POST 到 `/api/review` 并判安全。

### 修订并复审（可直接执行）

```bash
# 1) 发起修订（示例文件：敏感列 mail 获准替换；filter 只引用非敏感列 country，无行影响）
curl -s -X POST http://localhost:8080/api/revise \
  -H 'Content-Type: application/json' \
  --data @examples/revise_request.json
```

返回（节选）：末尾追加了 `export_users__safe_export`，`mail` 被置为 `null`，其余列同名复制：

```json
{
  "nodes": [
    {"id": "users", "type": "source", "columns": [
      {"name": "uid", "sensitive": false},
      {"name": "email", "sensitive": true},
      {"name": "country", "sensitive": false}]},
    {"id": "only_cn", "type": "filter", "input": "users", "references": ["country"]},
    {"id": "export_users", "type": "projection", "input": "only_cn", "columns": [
      {"name": "user_id", "copy": "uid"},
      {"name": "mail", "copy": "email"},
      {"name": "bucket", "constant": "ALL"}]},
    {"id": "export_users__safe_export", "type": "projection",
     "input": "export_users", "columns": [
       {"name": "user_id", "copy": "user_id"},
       {"name": "mail", "constant": null},
       {"name": "bucket", "copy": "bucket"}]}
  ],
  "export": "export_users__safe_export",
  "revised": true,
  "replaced_columns": ["mail"],
  "review": {"export": "export_users__safe_export", "blocked": false, "columns": [
    {"name": "user_id", "blocked": false, "content_reasons": [], "row_reasons": []},
    {"name": "mail", "blocked": false, "content_reasons": [], "row_reasons": []},
    {"name": "bucket", "blocked": false, "content_reasons": [], "row_reasons": []}]}
}
```

```bash
# 2) 用原审查接口复审返回的图（需要 jq；应得 "blocked": false）
curl -s -X POST http://localhost:8080/api/revise \
  -H 'Content-Type: application/json' \
  --data @examples/revise_request.json \
| jq '{nodes, export}' \
| curl -s -X POST http://localhost:8080/api/review \
  -H 'Content-Type: application/json' --data @-
```

无 `jq` 时可分两步保存中间结果：

```bash
curl -s -X POST http://localhost:8080/api/revise \
  -H 'Content-Type: application/json' --data @examples/revise_request.json > /tmp/revised.json
python3 -c "import json; d=json.load(open('/tmp/revised.json')); \
  json.dump({'nodes': d['nodes'], 'export': d['export']}, open('/tmp/safe.json','w'))"
curl -s -X POST http://localhost:8080/api/review \
  -H 'Content-Type: application/json' --data @/tmp/safe.json
```

行影响不可修订的例子（对敏感列筛选后，允许替换全部列也无法消除行影响）：

```bash
curl -s -i -X POST http://localhost:8080/api/revise \
  -H 'Content-Type: application/json' \
  -d '{
  "nodes": [
    {"id": "s", "type": "source",
     "columns": [{"name": "id_card", "sensitive": true}, {"name": "score"}]},
    {"id": "f", "type": "filter", "input": "s", "references": ["id_card"]},
    {"id": "e", "type": "projection", "input": "f",
     "columns": [{"name": "score", "copy": "score"}, {"name": "lit", "constant": 1}]}
  ],
  "export": "e",
  "allowed_replacements": ["score", "lit"]
}'
# HTTP/1.1 400 ...  {"error":{"code":"UNREVISABLE_ROW_INFLUENCE", ...
#   "row_reasons":[{"node_id":"s","column":"id_card"}]}}
```

## 按导出列精简：POST /api/prune

按**选定的导出列**把图精简为语义等价的最小图。请求体在审查请求基础上增加 `columns`：
**非空、列名非空且互异**的导出列名列表（顺序不影响结果，最终列序一律沿用导出节点的原列序）。
整张图先按 `/api/review` 的原规则校验，原有错误行为不变；随后每个选定列名都必须存在于导出节点。

### 精简规则

1. **节点保留**：保留导出节点及其**全部依赖祖先**，删除从导出不可达的其余节点。
   共享节点满足所有引用它的分支需求（一个节点同时喂给筛选与连接时取需求并集，不复制节点）。
   常量列不要求上游**列**，但其输入**关系**（节点）不能删除。
2. **反向需求传播**（自导出选定列向上游）：
   * **source 不裁剪**，源列与 `sensitive` 标记原样保留；
   * **filter** 保留条件 `references` 引用列，以及其下游仍需读取的列；条件不被修改；
   * **projection / join（含导出节点本身）只保留被需要的输出/别名**：导出投影或连接与普通
     节点一样**就地裁剪**，未选定的输出连同其复制依赖一并释放；
   * 两侧**连接键始终保留**（它们决定行集合），筛选引用列同样始终保留；
   * 某投影/连接虽被保留但已无任何被需要的输出时，保留其**原第一个输出**及其依赖，
     绝不产生空输出的非法节点（导出节点恒有选定列，不会落入此情形）。
3. **尾部投影**：**仅当**裁剪后导出节点**实际仍含未选定列**时，才在其后追加一个 ID 不与已有节点
   冲突的 `projection`（输入即原导出节点），只按**原列序** `copy` 选定列。导出投影/连接已就地
   只剩选定列时**不追加**；筛选只是**透传**其输入列——上游投影/连接已缩掉的未选列不会在筛选
   导出处重新出现，因此筛选导出同样可能无需追加。只有筛选条件 `references`（或源列）把未选列
   保留下来时（source 永不缩列，source 导出选定子集同理），导出才确实仍含未选列、需要追加。
4. **节点上限**：仅在确需尾部投影（总节点数变为 61）时才以 `PRUNE_NODE_LIMIT` 拒绝；
   上游缩列后筛选透传、无需追加的 60 节点图成功返回，拒绝时不返回部分图。
5. **保持不变量**：保留原节点 ID；保留节点在原请求中的相对顺序，以及节点内列/别名的相对顺序；
   不修改 `sensitive` 标记、常量值与筛选条件。
6. **语义等价但不“变安全”**：最终导出只含选定列；每个选定列的 `content_reasons` /
   `row_reasons` 与原图**完全相同**（敏感列精简后依然阻断，行影响不会因删列而消失）。

### 拒绝（HTTP 400，不返回部分图）

* 选定列名在导出节点上不存在（即使图本身安全也检查）：`PRUNE_COLUMN_NOT_FOUND`；
* 裁剪后导出仍实际含有未选定列、且追加尾部投影后节点数**超过 60**：`PRUNE_NODE_LIMIT`
  （可就地缩列、或经筛选透传后已无未选列的 60 节点图不追加、不拒绝）；
* 构造出的精简图对选定列的复审与原图不一致：`PRUNE_REVIEW_MISMATCH`（内部不变量，正常不会出现）；
* 以及 `/api/review` 的全部原图校验（`DUPLICATE_NODE` / `COLUMN_CONFLICT` /
  `INVALID_REFERENCE` / `CYCLE`）——整次请求先校验整张原图，无关分支的错误同样拒绝。

`columns` 为空或缺失、含空字符串或重复列名、未知字段、节点超过 60 等 schema 问题返回 HTTP 422。

成功响应包含：完整 `nodes`（精简后的图）、最终 `export`、按原列序的 `columns`、
`dropped_nodes`（被删除的不可达节点，按原顺序）、`added_projection`（追加节点 ID 或 `null`）、
`pruned`（是否发生任何删除/裁剪/追加），以及用原规则对最终导出**复审**的 `review`。

### 精简并用 POST /api/review 复审（可直接执行）

```bash
# 1) 发起精简（示例文件：只保留 user_id 与 bucket；列序沿用导出节点）
curl -s -X POST http://localhost:8080/api/prune \
  -H 'Content-Type: application/json' \
  --data @examples/prune_request.json
```

返回（节选）：导出投影 `export_users` **就地裁剪**，未选定的 `mail` 连同其复制依赖被删除，
**不追加新节点**；最终只含选定列、沿用原列序：

```json
{
  "nodes": [
    {"id": "users", "type": "source", "columns": [
      {"name": "uid", "sensitive": false},
      {"name": "email", "sensitive": true},
      {"name": "country", "sensitive": false}]},
    {"id": "only_cn", "type": "filter", "input": "users", "references": ["country"]},
    {"id": "export_users", "type": "projection", "input": "only_cn", "columns": [
      {"name": "user_id", "copy": "uid"},
      {"name": "bucket", "constant": "ALL"}]}
  ],
  "export": "export_users",
  "columns": ["user_id", "bucket"],
  "pruned": true,
  "dropped_nodes": [],
  "added_projection": null,
  "review": {"export": "export_users", "blocked": false, "columns": [
    {"name": "user_id", "blocked": false, "content_reasons": [], "row_reasons": []},
    {"name": "bucket", "blocked": false, "content_reasons": [], "row_reasons": []}]}
}
```

> 是否追加只取决于**裁剪后导出实际暴露的列**：source 永不缩列，source 导出选定子集时必追加；
> filter 只透传输入列——上游投影/连接已就地缩掉的未选列不会经筛选复活，此时筛选导出**不追加**，
> 而当筛选条件引用了未选列（或其上游是 source，未选列无法消除）时才追加；追加节点 ID 形如
> `<导出节点>__export`（冲突时自动换名），`added_projection` 返回其 ID 或 `null`。

筛选导出经上游缩列而无需追加的例子（投影 `p` 就地只剩 `a`，筛选只引用 `a` 并透传，选定 `a`
时 `added_projection` 为 `null`）：

```bash
curl -s -X POST http://localhost:8080/api/prune \
  -H 'Content-Type: application/json' \
  -d '{
  "nodes": [
    {"id": "s", "type": "source",
     "columns": [{"name": "a"}, {"name": "b"}]},
    {"id": "p", "type": "projection", "input": "s",
     "columns": [{"name": "a", "copy": "a"}, {"name": "b", "copy": "b"}]},
    {"id": "f", "type": "filter", "input": "p", "references": ["a"]}
  ],
  "export": "f",
  "columns": ["a"]
}'
# export 仍为 "f"，p 就地裁剪为只输出 a，added_projection 为 null
```

对照：筛选条件引用了**未选定**列 `b` 时，`b` 无法在上游消除，导出确实仍含 `b`，此时才追加
尾部投影（`f__export`）：

```bash
curl -s -X POST http://localhost:8080/api/prune \
  -H 'Content-Type: application/json' \
  -d '{
  "nodes": [
    {"id": "s", "type": "source",
     "columns": [{"name": "a"}, {"name": "b"}]},
    {"id": "f", "type": "filter", "input": "s", "references": ["b"]}
  ],
  "export": "f",
  "columns": ["a"]
}'
# export 为 "f__export"，f 的 references 仍为 ["b"]，added_projection 为 "f__export"
```

```bash
# 2) 用原审查接口 POST /api/review 复审返回的精简图（需要 jq）
curl -s -X POST http://localhost:8080/api/prune \
  -H 'Content-Type: application/json' \
  --data @examples/prune_request.json \
| jq '{nodes, export}' \
| curl -s -X POST http://localhost:8080/api/review \
  -H 'Content-Type: application/json' --data @-
```

复审也可以直接把精简结果（或手写同构的图）POST 到 `/api/review`，下例即上面“投影就地缩列、
筛选透传”的精简图，复审应得 `blocked: false`、只含选定列：

```bash
curl -s -X POST http://localhost:8080/api/review \
  -H 'Content-Type: application/json' \
  -d '{
  "nodes": [
    {"id": "s", "type": "source",
     "columns": [{"name": "a"}, {"name": "b"}]},
    {"id": "p", "type": "projection", "input": "s",
     "columns": [{"name": "a", "copy": "a"}]},
    {"id": "f", "type": "filter", "input": "p", "references": ["a"]}
  ],
  "export": "f"
}'
```

```json
{
  "export": "f",
  "blocked": false,
  "columns": [
    {"name": "a", "blocked": false, "content_reasons": [], "row_reasons": []}
  ]
}
```

选定列的原因与原图一致：若选定的是敏感列 `mail`，精简后复审仍判阻断（精简不“变安全”）：

```bash
curl -s -X POST http://localhost:8080/api/prune \
  -H 'Content-Type: application/json' \
  -d "$(jq '.columns = ["mail"]' examples/prune_request.json)"
# review.columns[0].content_reasons 仍为 [{"node_id":"users","column":"email"}]，blocked 为 true
```

选定列名不存在时返回 400 且不带任何部分图：

```bash
curl -s -i -X POST http://localhost:8080/api/prune \
  -H 'Content-Type: application/json' \
  -d "$(jq '.columns = ["ghost"]' examples/prune_request.json)"
# HTTP/1.1 400 ... {"error":{"code":"PRUNE_COLUMN_NOT_FOUND", ...
```

## 源列批量改名：POST /api/rename

把一个源节点的若干源列**批量改名**，并让图内引用仍指向**原来的列**。请求体在审查请求基础上
增加 `source`（源节点 ID）与 `renames`（**非空**改名列表，每项 `{"from": "原名", "to": "新名"}`，
原名、新名均非空）。整张图先按 `/api/review` 的原规则校验，原有错误行为不变。

### 输入约束（同时生效，允许互换）

* `source` 必须存在且为 `source` 节点：否则 `RENAME_SOURCE_NOT_FOUND`（400）；
* 每个 `from` 都按**原图**解析、必须存在于该源节点：否则 `RENAME_COLUMN_NOT_FOUND`（400，
  返回缺失原名列表）；
* `from` 在列表中**互异**（重复为 422）；`to` 非空（空字符串为 422）；
* 所有改名**同时生效**，因此**允许互换**（`a→b`、`b→a`），但最终源列名不得重复——两个原名
  指向同一新名、或新名撞上未改名的同名列，均判 `RENAME_CONFLICT`（400）；
* `from == to` 是无害空操作（不报错，也不计入结果的 `renamed_columns`）。

### 改名传播规则（更新该源的所有下游分支，含非导出分支）

1. **source**：源列定义就地改名，`sensitive` 标记、列顺序不变；
2. **filter**：条件 `references` 随其输入改名，并继续把改名**透传**给自己的输出列；
3. **projection**：每列的 `copy` 随其输入改名；**投影输出名不变**，`constant` 常量值不动——
   名称变化在此**停止**，下游只看到投影输出名；
4. **join**：对应侧的 `left_key`/`right_key` 与输出 `column` 随各自输入改名；
   **连接输出别名 alias 不变**——名称变化同样在此停止；
5. **不动的内容**：`sensitive` 标记、**其他源节点**的同名列、所有常量值；
6. 节点和列**不增删**；节点 ID、输入关系、节点顺序、节点内列/别名顺序全部不变。

### 复审一致性

响应自带用原规则对改名后图、**原 export** 的复审结果 `review`。要求：

* 阻断结论（整体与逐列 `blocked`）与原图**完全一致**；
* 改名后各导出列的原因集合，按“**新源列名 → 原源列名**”映射回原图后，与原图逐列一致；
  仅属于被改名源节点的原因被映射，其他源的同名列原因保持不变；
* 透传导出（source/filter 导出，或改名尚未在任何投影/连接处停止）的导出列名随改名变化；
  投影输出名、连接别名边界之后的导出列名不变。

复审不一致时以 `RENAME_REVIEW_MISMATCH` 整次拒绝（正常构造不会触发，属内部不变量）。
无效源、缺失原列、改名冲突或复审不一致时均**不返回部分图**。

### 请求与响应（可直接执行）

```bash
# 1) 发起批量改名（示例文件：uid/email/country 同时改名）
curl -s -X POST http://localhost:8080/api/rename \
  -H 'Content-Type: application/json' \
  --data @examples/rename_request.json
```

返回（节选）：源列改名；filter 的 `references` 变为 `["region"]`；导出投影的 `copy` 变为
`user_id`/`mail_addr`，但输出名 `user_id`、`mail`、`bucket` 与常量 `"ALL"` 不变——名称变化
在投影处停止；`mail` 的内容原因随源列改名为 `users.mail_addr`，仍判阻断：

```json
{
  "nodes": [
    {"id": "users", "type": "source", "columns": [
      {"name": "user_id", "sensitive": false},
      {"name": "mail_addr", "sensitive": true},
      {"name": "region", "sensitive": false}]},
    {"id": "only_cn", "type": "filter", "input": "users",
     "references": ["region"]},
    {"id": "export_users", "type": "projection", "input": "only_cn", "columns": [
      {"name": "user_id", "copy": "user_id"},
      {"name": "mail", "copy": "mail_addr"},
      {"name": "bucket", "constant": "ALL"}]}
  ],
  "export": "export_users",
  "source": "users",
  "renamed_columns": [
    {"from": "uid", "to": "user_id"},
    {"from": "email", "to": "mail_addr"},
    {"from": "country", "to": "region"}],
  "review": {"export": "export_users", "blocked": true, "columns": [
    {"name": "user_id", "blocked": false, "content_reasons": [], "row_reasons": []},
    {"name": "mail", "blocked": true,
     "content_reasons": [{"node_id": "users", "column": "mail_addr"}],
     "row_reasons": []},
    {"name": "bucket", "blocked": false, "content_reasons": [], "row_reasons": []}]}
}
```

```bash
# 2) 用 POST /api/review 复审返回的完整图（需要 jq；应与响应内 review 一致）
curl -s -X POST http://localhost:8080/api/rename \
  -H 'Content-Type: application/json' \
  --data @examples/rename_request.json \
| jq '{nodes, export}' \
| curl -s -X POST http://localhost:8080/api/review \
  -H 'Content-Type: application/json' --data @-
```

透传的导出列名会随之变化——对 source 直接改名并以 source 为导出时，复审列名即新源列名：

```bash
curl -s -X POST http://localhost:8080/api/rename \
  -H 'Content-Type: application/json' \
  -d '{
  "nodes": [{"id": "s", "type": "source",
             "columns": [{"name": "a"}, {"name": "b", "sensitive": true}]}],
  "export": "s",
  "source": "s",
  "renames": [{"from": "a", "to": "aa"}, {"from": "b", "to": "bb"}]
}'
# review.columns 的名为 "aa"、"bb"；"bb" 的 content_reasons 为
# [{"node_id":"s","column":"bb"}]，blocked 仍为 true
```

互换两列（同时生效）合法，但两列改到同一新名会冲突：

```bash
curl -s -i -X POST http://localhost:8080/api/rename \
  -H 'Content-Type: application/json' \
  -d '{
  "nodes": [{"id": "s", "type": "source",
             "columns": [{"name": "a"}, {"name": "b"}]}],
  "export": "s",
  "source": "s",
  "renames": [{"from": "a", "to": "x"}, {"from": "b", "to": "x"}]
}'
# HTTP/1.1 400 ... {"error":{"code":"RENAME_CONFLICT", ... "columns":["x"]}}
```

`renames` 为空/缺失、`from` 重复或为空字符串、`to` 为空、未知字段、节点超过 60 等 schema
问题返回 HTTP 422。

## 节点 JSON 结构

```jsonc
// source
{"id": "s", "type": "source",
 "columns": [{"name": "col", "sensitive": true}]}

// projection：copy 与 constant 二选一（constant 允许为 null）
{"id": "p", "type": "projection", "input": "s",
 "columns": [{"name": "out", "copy": "col"}]}

// filter：保留全部输入列，references 为条件引用到的列
{"id": "f", "type": "filter", "input": "s", "references": ["col"]}

// join：等值连接；outputs 别名必须互异，side 为 "left" 或 "right"
{"id": "j", "type": "join",
 "left_input": "a", "right_input": "b",
 "left_key": "ka", "right_key": "kb",
 "outputs": [{"alias": "x", "side": "left", "column": "v"}]}
```

* 节点可以乱序给出，服务按依赖做拓扑排序；只审查导出节点可达的祖先链。
* 一个关系连到自身两侧的等值连接（左右输入相同）是合法的；自环依赖（节点输入是自己）按环拒绝。
* `content_reasons` / `row_reasons` 中的来源按 `(node_id, column)` 排序，保证输出确定。

## 开发与测试

```bash
pip install -r requirements.txt pytest httpx
python -m pytest tests/ -q
```

## 文件说明

```
app/models.py     请求模型与结构约束（Pydantic；含 ReviseRequest、PruneRequest 与 RenameRequest）
app/engine.py     图校验、拓扑排序、来源传播、逐列审查、安全修订、按列精简与源列批量改名
app/main.py       FastAPI 入口（/api/review、/api/revise、/api/prune、/api/rename、/health）
examples/         请求示例（request.json、revise_request.json、prune_request.json、rename_request.json）
tests/            传播规则、拒绝场景、修订/复审、精简与改名测试
Dockerfile        容器镜像（python:3.11-slim，暴露 8080）
docker-compose.yml 单命令编排
```
