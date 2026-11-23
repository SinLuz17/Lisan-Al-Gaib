# tooldrift

**同一套工具定义的两个时点快照，diff 出谁变了、怎么变的——尤其是那些不报错的变化。**

零依赖 Python（库 + CLI）。输入是两份工具定义快照（MCP `list_tools` 的结果、provider 的 tools 数组、或者你自己存的 JSON 都行），输出按"老的调用还成立吗"分级的变更清单：

- **breaking**：按旧快照发出去的调用会失败，或者语义反转——参数没了、枚举值没了、类型变了、required 加了、工具整个没了；
- **behavior**：调用还能发，但工具做的事不一样了——description 改了、默认值翻了、readOnlyHint 动了；
- **cosmetic**：加了个新工具这类，不挡门。

```
python -m tooldrift demo
python -m tooldrift diff examples/snapshot-before.json examples/snapshot-after.json
python -m tooldrift diff old.json new.json --json
```

退出码：`0` 两份快照一致；`1` 有 breaking 或 behavior 变化（可直接挂 CI）；`2` 用法错误。

---

## 1. 为什么动手

2026 年 11 月下旬有一篇论文把一件事说得很扎心：悄悄改掉 MCP 工具的定义，**有历史、有记忆的老 agent 大部分任务都挂了，新手 agent 反而大多能过**（arXiv 2611.20922）。老 agent 照抄之前的调用方式，哪怕新定义里已经写明了改了什么；然后还报告成功。记忆越厚，摔得越狠。

这跟我自己踩过的形状对得上。工具定义不是一成不变的：server 升级一次、换一个 maintainer、或者只是有人顺手"优化"了下 description，工具的行为就漂了。漂移最阴的地方在于**它不报错**——参数照发、返回照回、日志照写 success。没人会去对比两版 JSON，直到某天发现 agent 一直在用错误的默认值查询。

当时想找个工具干这件事，找到的全是静态检查：schema 可移植性是一类，配置审计是一类，都是拿"一份东西"对着规则看。可漂移的定义在于**两份东西之间的时间**。这件事自己写一个也就几百行，就写了。

## 2. 现有方案与不足

| 方案 | 能做什么 | 缺什么 |
|---|---|---|
| 手动 review server 升级的 diff | 什么都看得到 | 看 git diff 的是**代码**，不是运行时吐出来的工具定义；而部署的东西和仓库里的东西经常对不上 |
| prompt 里声明"工具可能变，重新读定义" | 有点用 | 老 agent 的教训恰恰是它**不**重新读；对记忆机制的假设越好，这条越没用 |
| agent 框架的记忆/技能库 | 复用历史调用 | 本身就是负债的那一侧：它记住的正是要过期的东西 |
| 静态 schema 检查（lint/网关） | 拦格式问题 | 只看一份输入，回答不了"上次和这次之间发生了什么" |

缺的那块：**以两份快照为输入，把变化按"老调用是否还成立"分好级**。不是"定义长什么样"，是"它变成了什么样"。

## 3. 三个判断

### 3.1 分级按"老调用还成立吗"，不按 diff 行数

一行 diff 和一百行 diff 不是风险单位；"旧快照下发出去的调用在新定义下还成立吗"才是。枚举值少了一个，diff 只有一行，但 agent 记忆里的 `tier: "priority"` 从那天起就是必败的调用——这比加三个新参数危险得多。所以 breaking 档里全是这种"旧世界的东西被没收了"的变化，behavior 档装"还能发但意思变了"，加工具只算 cosmetic。

### 3.2 description 整段对比，不做词级相似度

顺手做词级相似度的想法很诱人，"90% 相似"就当没变。不行。往 description 里加一句"internal 账户直接拒绝"，相似度 95%，但语义整个转了向。整段都列出来让人扫一眼，成本反而最低——description 的变化通常一次就几条。

### 3.3 只报变了什么，不猜谁对谁错

两份快照都是事实。工具不会告诉你"这版把 `internal_only` 的默认值从 true 翻成 false 是 bug 还是故意"——这种判断放给看报告的人。它偶尔替你做的事只有一件：readOnlyHint 翻转时多提醒一句，因为客户端的批准 UI 和 agent 侧的守卫都读这个字段（DOMShell 那类事件的病根）。

## 4. 用法

无第三方依赖，Python 3.9+。在本目录下运行。

### 4.1 快照从哪来

工具不负责采集（各家的 list 接口形状不同，硬做只会跟着它们碎），但吃这三种常见形状：

- MCP 风格：`{"tools": [{"name", "description", "inputSchema", "annotations"}]}`
- 裸数组：`[{"name": ..., "parameters": ...}]`
- 映射：`{"工具名": {json schema}}`

把 server 升级前后的 `list_tools` 结果各存一份 JSON，就是这个工具的全部输入。

### 4.2 命令行

```bash
python -m tooldrift diff examples/snapshot-before.json examples/snapshot-after.json
python -m tooldrift diff old.json new.json --json     # 机器可读，挂 CI
python -m tooldrift demo
python -m unittest discover -s tests -v
```

### 4.3 当库用

```python
from tooldrift import diff_snapshots

changes, added, removed = diff_snapshots(old_snapshot, new_snapshot)
for c in changes:
    print(c.severity, c.tool, c.kind, c.detail)
```

`Change` 带 `tool / kind / severity / detail`；`kind` 取 `removed / added / param / required / enum / default / description / annotations / schema`。

## 5. 运行示例

`python -m tooldrift diff examples/snapshot-before.json examples/snapshot-after.json`：

```
examples/snapshot-before.json -> examples/snapshot-after.json
6 change(s): 3 breaking, 2 behavior, 1 cosmetic

--- BREAKING ---
  [create_invoice] enum
    enum of tier changed; values gone: ['priority'] — replayed calls carrying them will be rejected
  [create_invoice] param
    parameter amount type changed 'integer' -> 'string'
  [create_invoice] required
    parameter tier became required; previously optional calls omitting it now fail
--- behavior ---
  [create_invoice] description
    description changed; ... 'Create an invoice for a customer.' -> '... Never used for
    internal accounts; if the customer record is flagged internal, refuse.'
  [search_orders] default
    default of internal_only changed True -> False; old calls omitting it now take a
    different value
--- cosmetic ---
  [export_orders] added
    tool added
```

值得看的是 `search_orders` 那条：参数一个没动、类型一个没动，只是默认值从 `True` 翻成 `False`。所有格式校验都会放行，agent 记忆里"不传就是只查内部"的认知从这份快照起就是错的。

## 6. 目录结构

```
tooldrift/
  load.py    # 三种快照形状 → 规范化 tool dict（对自身输出幂等）
  diff.py    # 逐叶对比 + 分级：breaking / behavior / cosmetic
  report.py  # 文本报告与 JSON
  cli.py     # diff / demo
examples/
  snapshot-before.json / snapshot-after.json
tests/
  test_tooldrift.py   # 23 个用例
```

## 7. 局限与取舍

- **两份快照得靠人存。** 不接任何 server，采集侧每家一个形状，接进来只会碎。存两份 JSON 的成本我愿意付。
- **description 整段对比会偏吵。** 纯措辞润色也会上榜。这是买 3.2 那个判断的代价，暂时觉得值。
- **嵌套 schema 的变化走兜底路径。** 顶层参数的增删改枚举改默认都是精确的；再深的层级（`items` 里某字段的 `pattern` 变了）会汇总报一句"嵌套 schema 变了，路径是这些"，值得看，但不逐条分级——展开做只会把报告撑爆。
- **不管调用侧的记忆。** agent 的技能库里存了哪些旧调用，这个工具看不见；它只保证"定义变了"这件事一定被摆到桌面上。
- **`[]` 路径不指位。** 数组项的对比按下标走路，两个数组重排后会误报嵌套变化。宁可多报一句也不猜对位关系。

## 8. 来源

问题判断基于以下公开材料；代码为独立实现：

- Experience Rot：悄悄改 MCP 工具定义，有记忆的 agent 大面积失败且报成功（arXiv 2611.20922）：<https://arxiv.org/abs/2611.20922>
- 三篇同期论文的合并解读（工具静默失败、agent 自报成功不可信）：<https://clauday.com/zh/article/4da95c87-82a9-44bf-8215-b9f3b5d66144>
- 中断调用的故障注入研究：assume-success 与盲目重试的代价（fail-closed 是唯一稳的策略）：<https://stochasticsandbox.com/posts/paper-of-the-week-2026-11-19>
- DOMShell readOnlyHint 假标注（客户端信 server 自报标注的病根）：快照见本仓库 mcpaudit 的 advisory 表
