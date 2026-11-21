# wirecheck

**同一份 tool schema 要发给多家 provider 时，先说清楚每家会拒掉什么、会悄悄忽略什么。**

零依赖 Python（库 + CLI）。输入是 JSON Schema 形式的工具定义，输出是每个目标 provider 的判定与一份损失清单：哪些构造会被 400 拒绝、哪些关键字能过但不生效、哪些属于"文档既没说支持也没说不支持"。

```
python -m wirecheck demo
python -m wirecheck lint examples/ops-tools.json --targets gemini,openai
python -m wirecheck compile examples/ops-tools.json --targets azure --out build/
```

---

## 1. 为什么动手

给同一个 agent 接三家 provider 时，最先坏的不是 prompt，是工具定义。

我这边的顺序是这样的：Claude 一次就过，GPT 对嵌套对象参数的写法有意见，Gemini 直接 400，报的是 `Unknown name "$ref"`——请求在工具被调用之前就被拒了，一个字段都没执行。Gemini 的函数声明只接受 OpenAPI 3.0 的一个子集，`$ref`、`$defs`、`additionalProperties` 都属于"未知字段"，有最小复现可以对照。而 OpenAI 的 strict 模式要的正好是反面：每个 object 都要写 `additionalProperties: false`，所有字段都要进 `required`。同一份 schema，两边的要求互相冲突。

真正难查的不是这个 400，而是不报错的那种。有整理把 Azure 的文档列表并出来看：`minimum`、`maximum`、`pattern`、`format` 这类关键字是不支持的——请求照样 200，约束不生效。网关的做法更隐蔽：把 `$ref` 从 `$defs` 里内联，把 `oneOf` 改成 `anyOf`，把目标不认识的关键字直接丢掉，日志打在 debug 级。于是同一个工具在 OpenAI 上是"金额 1 到 5000"，在 Gemini 上什么限制都没有，两边的调用记录都写着成功。

我要的不是又一个转译器。转译器已经够多了：网关在做、LiteLLM 在做、各家 SDK 的 helper 也在做。我要的是一个能回答"这份 schema 在目标上会以什么方式变弱、弱在哪里"的检查器，因为改完之后没人会去比对两份 JSON。

## 2. 现有方案与不足

| 方案 | 它能做什么 | 缺什么 |
|---|---|---|
| API 网关 / LiteLLM 一类的 schema 改写 | 让请求能过：内联 `$ref`、折叠 `allOf`、丢掉不认识的关键字 | 改写是单向的。丢掉的关键字打在 debug 日志里，写 schema 的人看不到；一个内部工具被多个团队调用时，没人知道自己的 `pattern` 在某家上没生效 |
| 各家 SDK 的 schema 生成（Pydantic / Zod helper） | 面向一家 provider 生成合法 schema | 换一家要重写；不告诉你两家生成的差异在哪，也不告诉你哪些字段在换的过程中失去了约束 |
| JSON Schema 校验器（jsonschema / ajv） | 判断"一个实例是否符合 schema" | 判断的是实例，不是"这个 schema 在目标 provider 上是否还成立"。被丢掉的关键字在这里完全隐形 |
| 各家文档 | 列出自己支持的关键字 | 每份一份、口径互相矛盾（Gemini 文档支持 `minimum`/`maximum`，Azure 文档列为不支持），没人把它们并排放，也没人对着你的 schema 跑一遍 |

缺的那一块：**以"我的 schema"为输入，逐个目标给出拒绝项与损失项，并且把损失写下来**。不是"能不能转"，是"转完还剩什么"。

## 3. 三个判断

### 3.1 三档，不是两档

只分"支持/不支持"会漏掉最贵的一类：

- `reject`：目标会拒绝请求（Gemini 收到 `$ref` 直接 400）。这种最吵，反而最容易发现。
- `silent`：请求成功，约束不生效（Azure 的 `minimum`）。没有错误、没有日志、没有指标，是事故的真正来源。
- `unverified`：文档既没说支持也没说拒绝。工具把关键字留在 schema 里，但单独列出来，并写明"需要发一次真实请求才能定"。

第三档是刻意的。把不确定当支持，等于把线上第一次调用当成测试；把不确定当拒绝，会逼人删掉本来有用的约束。分开列出来，成本最低。

每个 profile 的每条规则都带来源链接（`python -m wirecheck profiles` 会打出来）。规则写错是我的问题，但至少能追到是哪一页读错了。

### 3.2 编译不许静默

被移走的关键字不直接扔掉，而是转成一句人话留在 `description` 里：

```json
"amount": {
  "type": "integer",
  "description": "Values are constrained (minimum 1; maximum 5000); this endpoint does not enforce it, so validate in code."
}
```

模型仍然看得见"金额有上下限"，代码侧也知道要自己校验。这比两种常见做法都好：一种是留着关键字假装没事（请求过了，约束没了），一种是删干净（模型连"有约束"都不知道）。

每条损失都记成一行：JSON 指针、关键字、类别、原因。指针用属性名而不是下标（`/properties/amount`，不是 `/properties/3`），因为要拿去改代码。

### 3.3 能等价改写的才改写，做不到就判 blocker

改写只做语义等价的那几种：

- `$ref` 内联（目标没有 `$ref` 时）。递归 schema 内联不出来，直接判 `blocker`——目标表达不了，任何"补一个 depth 上限"的写法都是在改语义。
- `allOf` 的对象分支合并（properties / required 取并集）。分支冲突时保留后者，并把冲突记成 `coerced`。
- `const` → 单值 `enum`。
- 非字符串 `enum` → 字符串（Gemini 只收字符串），同时提示"模型会回字符串，记得 parse 回来"。
- strict 目标：每个 object 补 `additionalProperties: false`，所有字段进 `required`，可选字段变成 `T | null`。

最后一条有个副作用值得单独说：`close_tickets` 那种"所有过滤条件都可选"的工具，在 strict 模式下会变成"必须逐个显式给 null"。这看着啰嗦，但它把"模型漏写了一个过滤条件"从默认值行为变成了显式选择——而默认值行为正是"截断的调用把工单关到全表"那类事故的形状。

剩下一律不动，只报告。`oneOf` 不折叠成 `anyOf`，因为那会改掉互斥语义；`pattern` 不在 Gemini 上删掉，因为它的文档没说不行。

## 4. 用法

无第三方依赖，Python 3.9+。在 `tools/2026-11-21-tool-schema-portability/` 下运行。

### 4.1 命令行

```bash
# 四家一起看
python -m wirecheck lint examples/ops-tools.json

# 只看一家、一个工具，把来源也打出来
python -m wirecheck lint examples/ops-tools.json --targets azure --tool refund_order --explain

# 机器可读，适合挂 CI
python -m wirecheck lint examples/ops-tools.json --json > report.json

# 把某家的 wire schema 落盘
python -m wirecheck compile examples/ops-tools.json --targets openai,gemini --out build/

# 规则快照与来源
python -m wirecheck profiles

# 测试
python -m unittest discover -s tests -v
```

退出码：`0` 没有 blocker；`1` 有 blocker（默认），或 `--fail-on degraded` 时出现损失。`2` 是参数错误。

输入接受三种常见形状：OpenAI 的 `{"tools":[{"type":"function","function":{...}}]}`、`[{"name":...,"parameters":...}]`、以及 `{"工具名": {json schema}}`。

### 4.2 当库用

```python
from wirecheck import compile_tool, get, load_tools
import json

tools = load_tools(json.load(open("examples/ops-tools.json")))
name, schema = tools[0]

compiled = compile_tool(name, schema, get("gemini"))
print(compiled.verdict)                 # pass / edge / degraded / reject
for loss in compiled.losses:
    print(loss.pointer, loss.keyword, loss.severity, loss.detail)
wire = compiled.schema                  # 直接发出去的那份
```

`Compiled.losses` 每项带 `pointer / keyword / kind / severity / detail`；`verdict` 由最严重的一档决定。

---

## 5. 运行示例

`python -m wirecheck demo`，输入是 `examples/ops-tools.json` 里的四个工具（退款、开工单、批量关单、搜文档）：

```
input: examples/ops-tools.json (4 tools)

4 tool(s) x 4 target(s)

tool            openai     azure      anthropic  gemini
refund_order    degraded 1 degraded 5 edge       degraded 1
create_ticket   edge       degraded 3 edge       degraded 1
close_tickets   edge       degraded 2 edge       pass
search_docs     pass       pass       pass       pass

silent losses — accepted, not enforced, nothing reports them: 13
  refund_order @ openai  [degraded]
    /properties/meta                             additionalProperties     was open (true); strict mode closes it, so callers can no longer send extra keys
  refund_order @ azure  [degraded]
    /$defs/LineItem/properties/qty               minimum                  accepted by the API but not enforced; removed so the schema stops lying
    /properties/amount                           maximum                  accepted by the API but not enforced; removed so the schema stops lying
    /properties/amount                           minimum                  accepted by the API but not enforced; removed so the schema stops lying
    /properties/meta                             additionalProperties     was open (true); strict mode closes it, so callers can no longer send extra keys
  ... 8 more in this group

unverified — kept on the wire, needs one real request to settle: 21
  refund_order @ openai  [degraded]
    /$defs/LineItem/properties/qty               minimum                  not documented either way for this target; kept, but untested
    /properties/amount                           maximum                  not documented either way for this target; kept, but untested
    ...
```

几点值得看：

- `search_docs` 四家全 `pass`。它只有 `string` 和一个 `integer`，这说明"能过"和"约束还在"是两件可以同时成立的事——前提是 schema 一开始就别用各家的方言。
- `refund_order` 在 Gemini 上是 `degraded` 而不是 `reject`，因为 `$ref` 被内联掉了。这是工具的价值所在：一个本来 400 的工具，变成了"能跑、但少了一个约束"。
- `refund_order` 在 OpenAI 上唯一的损失是 `meta` 那个 `additionalProperties: true` 被 strict 模式关掉——作者本意是"允许任意扩展字段"，现在发不出去了。

`refund_order @ azure` 的完整损失（`--limit 20`）：

```
silent losses — accepted, not enforced, nothing reports them: 5
  refund_order @ azure  [degraded]
    /$defs/LineItem/properties/qty               minimum                  accepted by the API but not enforced; removed so the schema stops lying
    /properties/amount                           maximum                  accepted by the API but not enforced; removed so the schema stops lying
    /properties/amount                           minimum                  accepted by the API but not enforced; removed so the schema stops lying
    /properties/meta                             additionalProperties     was open (true); strict mode closes it, so callers can no longer send extra keys
    /properties/order_id                         pattern                  accepted by the API but not enforced; removed so the schema stops lying

shape changes — required for the target, no meaning lost: 1
  refund_order @ azure  [degraded]
    /                                            required                 3 optional properties became required-and-nullable for strict mode: currency, line_items, meta
```

`create_ticket @ gemini` 是另一种损失：枚举值被改成字符串，`oneOf` 留着但标成 untested。

```
silent losses — accepted, not enforced, nothing reports them: 1
  create_ticket @ gemini  [degraded]
    /properties/severity                         enum                     enum values sent as strings (1, 2, 3); parse them back, the model will echo text

unverified — kept on the wire, needs one real request to settle: 3
  create_ticket @ gemini  [degraded]
    /properties/attachment                       oneOf                    not documented either way for this target; kept, but untested
    /properties/attachment/oneOf/1/properties/expires_at format                   not documented either way for this target; kept, but untested
    /properties/attachment/oneOf/1/properties/url format                   not documented either way for this target; kept, but untested
```

编译结果里，`meta` 那个被丢掉的 `additionalProperties` 变成了描述里的一句话：

```json
"meta": {
  "type": "object",
  "description": "The original schema also declared additionalProperties true, which this endpoint has no equivalent for."
}
```

而 `line_items` 的 `$ref` 已经展开成对象本身（`/properties/line_items/items/properties/sku` 之类），`$defs` 不在输出里。

---

## 6. 目录结构

```
wirecheck/
  profiles.py   # 四家的规则快照：rejects / silent / unverified，每条带来源
  compile.py    # $ref 内联、allOf 合并、关键字迁移、strict 收口、上限检查、损失记录
  report.py     # 矩阵 + 按严重度分组 + JSON
  cli.py        # lint / compile / profiles / demo
examples/
  ops-tools.json
tests/
  test_wirecheck.py   # 40 个用例
```

## 7. 局限与取舍

- **规则是快照，不是真理。** 四家的能力都在变，`profiles.py` 里每条规则都挂了来源链接；读错了算我的。`unverified` 那一档就是"请自己发一次请求"的清单，工具替不了这一步。
- **不做真实请求。** 没有 provider key 也能跑，这是有意的：CI 里不该需要密钥才能检查 schema。
- **内联会放大体积。** `$ref` 展开后属性数和嵌套层数都会涨，Azure 的 100 属性 / 5 层限制可能因此被触发。工具会报 blocker，但不会替你拆工具——拆工具是设计决定，不是格式化。
- **只处理本地 `$ref`。** 外部引用和内联都表达不了的东西直接判 blocker，不猜。
- **输入是 JSON Schema。** 没有做 Pydantic / Zod 的适配层；那属于各家 SDK 的活儿，硬做出来只会跟着它们的版本碎掉。
- **它不判断 schema 对不对。** "这个工具该不该有 `amount` 上限"是业务问题；工具只回答"这个上限在目标上还在不在"。
- **`oneOf` 不折叠。** 有些网关会把它改成 `anyOf` 以便让请求通过，代价是互斥性没了。这里选择保留并标成 unverified，因为改语义比报错更难查。

## 8. 来源

规则与判断基于以下公开材料；代码为独立实现：

- Gemini 函数声明只接受 OpenAPI 子集，`$ref` / `$defs` / `additionalProperties` 触发 HTTP 400（含最小复现与错误原文）：<https://github.com/deepseek-ai/deepseek-harness/discussions/10904>
- Gemini 拒绝非字符串枚举与嵌套 `anyOf` 的现场记录：<https://community.n8n.io/t/gemini-cant-read-n8n-mcp-tools/326400/5>
- 三家 provider 的 JSON Schema 关键字对照，含 Azure 的不支持列表与 Claude 对范围/长度关键字的处理：<https://www.toolbit.ai/blog/llm-structured-output-json-schema>
- OpenAI strict 模式对 `additionalProperties` 与 `required` 的要求：<https://platform.openai.com/docs/guides/structured-outputs>
- 网关在转发前改写 schema 的实际做法（内联 `$ref`、`oneOf`→`anyOf`、丢弃无对应关键字、日志在 debug 级）：<https://ragmux.com/learn/gemini-gateway>
- 跨 provider 工具定义的现实差异（嵌套参数、字段被静默丢弃）：<https://wpnews.pro/news/mcp-s-simplicity-is-its-fatal-flaw>
- 同一份 schema 在不同供应商间行为不同、错误不出现的现象：<https://cloud.tencent.com.cn/developer/article/2760753>
