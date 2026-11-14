# schemaevo · Parquet 字段 ID 与类型演进映射

湖仓表治理小工具：上游提交 Parquet 表 schema 变更时，先算清「字段怎么变了、兼容级别是什么、迁移 SQL 按什么顺序执行、谁会读坏」，再按**受控发布**流程把结论切换为生效版。

## 受控发布（核心流程）

一次对比 = 一个发布版本（release），不再是「一改输入就生成新结论」：

1. **冻结**：生成预演时，把本次规则快照（矩阵版本 + 完整矩阵内容摘要）、两版 schema、读取方清单一起冻结，算出指纹（sha256）。回填与影响范围来自同一时刻的同一份快照。
2. **预演**：`POST /api/releases/preview` 生成 `preview` 状态的版本；**相同冻结输入重试直接回到原版本，不新增版本**（指纹即幂等键，与粘贴文本的缩进/键序无关）。
3. **发布闸门**：阻断全部消除才允许发布。阻断 = 迁移阻断（不产出 ALTER 的项）+ 需人工确认项 + broken_consumers + 兼容级别 none。方向性阻断（backward/forward 级别的成因）不算阻断，它决定的是升级顺序。
4. **一次性切换**：发布 = 原子替换 `data/current.json` 里该表的指针（临时文件 + `os.replace`）。结果页、历史、下载脚本都读同一个指针，永远指向同一版。
5. **失败保留上一版**：切换过程任何 IO 失败，`current.json` 原样不动，上一版继续生效；该版本标为 `failed`，可用同一冻结输入重试（不新增版本）。
6. **失效**：规则（类型矩阵内容）一旦更新，旧版本派生为 `stale`；同表发布更新版本后，旧版派生为 `superseded`。失效版不再是可执行方案：结果页醒目提示、下载接口一律 409。
7. **版本状态**：`preview`（预演）/ `published`（当前生效版，唯一可下载脚本的状态）/ `failed`（发布失败，上一版仍生效）/ `stale`（规则已更新）/ `superseded`（已被新版取代）。状态由 current 指针与当前规则摘要**按读取时刻派生**，不落库、不漂移。

## 能力

- 字段匹配：`field_id` 优先，无 id 时按名称精确匹配；改名、字段 ID 变更、新增、删除逐条标注依据。
- 类型提升矩阵：代码内显式表（int8/int16/int32/int64、float/double、decimal 精度、string→int、date→timestamp 等），表外类型对一律 `unknown`（不猜）。
- 兼容级别：按「旧写新读 / 新写旧读」双向判定，输出 `full` / `backward` / `forward` / `none`。
- 迁移计划：有序 `ALTER TABLE` 序列（改名 → 加列/默认值 → 回填 → 类型变更 → 删列最后）、需回填对象、读写方升级顺序；分区字段被删或改类型直接阻断。
- 列引用检查：数据文件清单里引用被删/被阻断列的 → `broken_consumers`；引用改名列的 → 需同步改名。
- 4 个页面：首页（场景 + 三组样例 + 演示步骤）、对比工作台、结果页（预演/生效/失效状态 + 发布按钮）、复核与导出（发布历史 + 生效版 SQL 下载 + 矩阵说明）。
- 发布记录以 JSON 落在 `/app/data/releases/`（compose 命名卷），当前版指针在 `/app/data/current.json`；所有写入走「临时文件 + 原子替换」。

## 核心规则（纯函数模块）

| 模块 | 文件 | 规则 |
|---|---|---|
| 字段匹配 | `app/matching.py` | id 相同优先；id 同名称变 → renamed；名称同 id 变 → id_changed（人工确认）；无匹配 → added/removed |
| 类型矩阵 | `app/types.py` | 显式提升/阻断表；decimal 精度提升兼容、收窄阻断、标度变更 unknown |
| 兼容判定 | `app/compat.py` | 双向各自判定后合成级别；分区字段增删改类型双向阻断 |
| 迁移计划 | `app/migration.py` | 固定顺序出 ALTER，阻断项不产出 ALTER，删列最后 |
| 引用检查 | `app/consumers.py` | 逐文件逐列比对匹配结果，输出 broken / needs_update / unknown |
| 报表编排 | `app/report.py` | 汇总成可重放的对比结果 + 下载脚本 |
| 受控发布 | `app/release.py` | 冻结输入 / 指纹 / 派生状态 / 发布闸门（纯函数） |
| 发布编排 | `app/control.py` | 预演幂等、发布原子切换、失败保留上一版 |
| 持久化 | `app/store.py` | releases/*.json 原子写 + current.json 指针 + 裁剪（生效版永保留） |

## 启动

```bash
docker compose up -d --build
curl -s http://127.0.0.1:18216/health
```

页面：<http://127.0.0.1:18216/>

## 演示步骤

1. 首页点「用样例会 B 执行对比」，工作台预填 `dwd_payment` 旧/新 schema 与数据文件清单。
2. 点「生成预演」→ 结果页：预演状态（未生效），改名 1、新增 2、回填 1、`decimal(10,2)→(18,2)`，兼容级别 `backward`，ALTER 4 条。
3. 阻断已全部消除 → 点「发布为当前生效版」→ 状态变为 `published`，此时才可下载迁移脚本；重复发布是幂等空操作。
4. 换成样例 C（`dwd_shipment`）→ 级别 `none`、阻断未消除，发布被 409 拒绝并返回阻断清单。
5. 到「复核与导出」看发布历史与各表当前生效版；用新输入/新清单再预演发布后，旧版自动失效（`superseded`），其脚本下载返回 409。

样例：

| 样例 | 表 | 预期 |
|---|---|---|
| A | `dwd_orders` | `full`，0 条迁移 SQL，可直接发布 |
| B | `dwd_payment` | `backward`，rename 1 / added 2 / 回填 1 / ALTER 4，可发布 |
| C | `dwd_shipment` | `none`，阻断 + broken_consumers，发布被拒 |

## API / 页面

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/` | 首页（场景、样例、演示步骤、最近发布） |
| GET | `/workbench?sample=A|B|C` | 对比工作台（预填样例 + 即时预演，不落库） |
| POST | `/api/releases/preview` | `{sample}` 或 `{old,new,consumers}`；冻结输入生成预演，同指纹幂等返回；非法输入 400 |
| POST | `/api/releases/{release_id}/publish` | 阻断清零后原子切换为当前生效版；有阻断 409（附清单）、已失效 409、切换失败 500（上一版保留） |
| GET | `/result/{release_id}` | 结果页（状态横幅 + 逐条依据 + 发布/重试按钮） |
| GET | `/review` | 发布历史 + 各表当前生效版 + 生效版 SQL 下载 + 矩阵说明 |
| GET | `/api/releases`、`/api/releases/{release_id}` | 发布记录（含派生状态） |
| GET | `/api/releases/{release_id}/migration.sql` | 下载迁移脚本：仅当前生效版 200，其余状态 409 |
| GET | `/api/matrix` | 完整类型矩阵 |
| GET | `/api/samples/{key}` | 样例原文 |
| GET | `/health` | 健康检查 |

## 目录

```text
app/            main.py + release/control/store + 6 个纯函数模块 + templates/ + static/
sample_data/    a/b/c 三组 old/new/consumers.json + index.json
tests/          test_matching / test_types / test_compat / test_migration / test_release / test_api
data/           发布记录与当前版指针（本地直跑时生成，已 gitignore）
```

## 已知限制

- 只做 schema 级别的静态判定，不读取 Parquet 文件字节，也不校验数据文件真实 id 编码；`consumers.json` 里的列引用来自人工/采集清单。
- 类型矩阵是显式小表，未覆盖的合法类型对（如 `string→double`、`timestamp→date`）返回 `unknown`，需人工确认，不自动推断。
- `ALTER TABLE` 为通用 SQL 片段（Spark/Trino 语义），不连接具体引擎执行；回填动作只给建议 SQL 与对象清单。
- 「清单变化」以新一次预演/发布为准被感知：系统不主动轮询读取方清单，清单更新后需重新预演，发布后旧版即失效。
- 发布记录保存最近 200 条，超出按修改时间裁剪（当前生效版永远保留）；`data/` 被 gitignore，容器重建需通过命名卷保留。
- 单机文件存储，无多副本一致性协议；并发发布同一表以 `current.json` 原子替换为准，后写覆盖先写。
- 无登录/权限，单机演示定位。
