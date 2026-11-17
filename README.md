# 叙事母题证据台账

这是一个供文学课程与民间叙事研究小组使用的本地分析工具。研究者导入有来源说明的故事结构记录，逐条标注判断及其事件依据，再查看标注共识、未决覆盖率和可回溯报告。工具不自动解释文学意义，也不把新闻评论中的解释当作已验证结论。

## 运行

需要 Python 3.11 或更新版本，无第三方依赖。

从项目根目录运行：

```sh
PYTHONPATH=src python -m story_patterns.cli demo --database /tmp/story-patterns.sqlite3 --fixture fixtures/demo.json
```

也可分别使用 `import`、`decide` 与 `report` 子命令处理 JSON 文件。每个故事修订版包含来源、角色顺序及连续事件；每项判断必须引用该修订版中的事件。批次标识支持安全重试，标注标识与标注员序号不可覆盖既有内容。

支持包含故事对象的 JSON 数组或逐行 NDJSON 导入；这两种格式需使用 `--batch-id`。`queue` 展示未评、待补共识、分歧及已达共识的样本，并列出尚未被判断引用的事件。`report` 同时给出标注员两两一致率及事件证据覆盖率；这些是描述性指标，不替代人工解释。报告以内容指纹保存，可用 `export --report-id ... --format json|csv --output ...` 输出；默认拒绝覆盖现有文件，显式 `--force` 才替换。`audit` 检查 SQLite 内部与外键完整性。

导入前可以用 `preview` 汇总每条记录的校验错误，不写数据库。默认 `import` 仍是整批事务；加 `--partial` 时只写入可通过校验的记录，坏行、批内重复项、与既有修订冲突的行会留在批次台账中而不进入 `stories`，因此也不会进入任何报告分母。`lineage` 展示已录入的修订版本与缺失序号。`compare` 按传统标签分层输出描述性比例、共识数与覆盖率；不同分组的分母可能不同，结果不作因果或总体推断。

### 批次状态、写入边界与重试

每次 `import --partial` 都会在一个事务内写入两处内容：批次标记（`import_batches`）与逐行台账（`import_batch_rows`）。台账保存每一行的行号、规范化前的原始载荷、行指纹、判定状态与原因；只有状态为 `accepted` 或 `already_present` 的行才会写入 `stories` 与 `batch_stories`。批次状态由行状态推导：

| 状态 | 含义 | 是否写入故事表 |
| --- | --- | --- |
| `accepted` | 所有行都可入库（含内容完全相同的既有修订） | 是 |
| `partial` | 至少一行入库、至少一行被隔离 | 仅可接受行 |
| `rejected` | 没有任何行可入库；批次仍记台账供复核 | 否 |

行状态：`accepted`、`already_present`（同一修订版内容完全一致）、`invalid`（JSON 或字段校验失败）、`duplicate_in_batch`（同一批次重复出现同一 `story_id@revision`，两份都不入库）、`conflict`（该修订版已存在且内容不同，绝不覆盖）。

两个摘要各司其职：

- `source_digest` 覆盖按文件顺序排列的全部来源行（含被隔离的行与无法解析的原始文本），只由批次标识和行内容决定。它是重试时比较的对象。
- `accepted_digest` 只覆盖本批实际入库的修订版，与旧版整批摘要同形，用于复核 `batch_stories` 与 `stories` 是否一致。

重试规则：

- 同一 `--batch-id` 提交完全相同的来源内容 → `already_imported`，不写任何新行、不改动台账或摘要，属于幂等重试。
- 同一 `--batch-id` 提交不同内容 → 拒绝（退出码 2），错误 JSON 同时给出 `recorded_source_digest`、`submitted_source_digest` 与 `"idempotent": false`。这不会被误报为幂等成功。
- 修复坏行后重投请使用**新**的批次标识并加 `--retry-of <原批次>`；两个批次各自独立、来源摘要不同，`retry_of` 保留修复链路。已入库的可接受行在新批次中显示为 `already_present`，不会重复计数。
- 进程中断时事务回滚，批次标记与台账同时不存在，`batch-status` 返回 `"recorded": false`；同一内容可直接重投。只有提交成功才会留下批次记录。
- 未加 `--partial` 的历史式导入同样写入台账，语义仍为全有或全无；旧数据库在首次打开时自动迁移，旧批次标记为 `legacy`，仍按已入库内容复核摘要。

检查批次审计：

```sh
# 提交前：只读预检，含行状态与重试判定（相同/不同/新批次）
PYTHONPATH=src python -m story_patterns.cli preview --database "$DB" \
  --file fixtures/intake/import-2026-11-17.ndjson --format ndjson --batch-id intake-2026-11-17

# 逐行台账：每行的状态、原因与修订版指纹
PYTHONPATH=src python -m story_patterns.cli batch-status --database "$DB" --batch-id intake-2026-11-17

# 全部批次的状态、两个摘要、修复链路与隔离行数
PYTHONPATH=src python -m story_patterns.cli manifest --database "$DB"

# 台账交叉复核：行指纹、来源摘要、批次状态推导、被隔离行不得入库、retry_of 必须存在
PYTHONPATH=src python -m story_patterns.cli audit --database "$DB"
```

`preview` 的 `retry_assessment.kind` 会是 `new_batch`、`identical_retry`、`different_content` 或 `legacy_batch_unknown`；只有 `identical_retry` 的 `idempotent` 为 `true`。

`manifest` 汇总导入批次、故事修订和来源引用。保存报告还包含读者剔除敏感性检查，提示结论是否依赖某位读者；该诊断不等同于置信区间。报告可导出为 Markdown (`--format md`) 供课程讨论。

`audit` 除 SQLite 外键和数据库完整性检查外，也会复核故事文档与索引列、导入批次摘要、标注证据、仲裁摘要及已保存报告的内容指纹是否一致。修订对比会标出角色、顺序、来源和事件内容变化；每个版本独立标注，系统不把旧版判断自动复制到新版。

报告列出最新有效读者的逐标签投票数和每位读者覆盖的修订数；被后续序号取代的旧判断仍保留在 `history`，但不会重复进入统计。

若独立标注存在分歧，编辑可用 `adjudicate` 提交单独的处理记录。它要求引用当前修订事件与简短理由，不覆盖任何读者投票；报告并列保留独立共识、最终采用标签及仲裁依据。`history` 可查看一位读者此前的判断和所有仲裁版本。

报告分别统计“平行型/加害型”判断和最小者结局判断。`unclear` 与 `unknown` 不进入对应投票，但覆盖表会保留同意、分歧和样本不足数量。报告列出参与故事修订及判断编号，便于复核。数据库仅保存在用户指定的本地路径。

演示文件完全由本地合成材料构成，并非新闻报道或已出版故事的原文。

## 检查

```sh
PYTHONPATH=src python -m unittest discover -s tests -v
PYTHONPATH=src python -m compileall -q src
PYTHONPATH=src python -m story_patterns.cli demo --database /tmp/story-patterns-check.sqlite3 --fixture fixtures/demo.json
```

样例输入在 `fixtures/intake/`：`import-2026-11-17.ndjson` 含一条格式错误行、一处未知角色、一组批内重复项与两条可接受记录；`retry-2026-11-18.json` 是修复后的 JSON 数组重投。
