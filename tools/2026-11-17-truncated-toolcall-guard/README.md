# toolcall-guard

**当模型因为输出 token 上限被截断时，别执行它写了一半的工具调用。**

一个零依赖的 Python 小库（库 + CLI），做两件事：

1. 在工具执行路径上挡一道闸：把各家 provider 的"为什么这一轮结束"归一化，若这一轮没有正常结束，则本轮的 tool call 整体不执行，并把拒绝原因作为 tool error 回灌给模型；
2. 回扫已有的运行 trace，统计历史上"在没正常结束的轮次里仍然执行了工具"的暴露面——这些运行当时都记成了 success，指标里看不出来。

```
python -m toolcall_guard demo
python -m toolcall_guard audit examples/traces.jsonl --side-effects examples/side-effects.json
```

---

## 1. 问题背景

### 1.1 起因

2026 年 11 月，一位开发者在 dev.to 公开了两份实验报告，指出 **Langroid** 与 **Heym** 两个 agent 框架在模型输出被 token 上限截断时，仍然执行了被截断的工具调用，且把这一轮记为成功：

- Langroid（issue #1291）：模型要退款 1000，在数字 `10` 之后被截断，参数文本是 `{"order_id": "A-1001", "amount": 10`（非法 JSON）。框架用 `json_repair` 这类宽松解析器把它补成合法对象，工具收到 `amount=10.0`，回复"已退款 $10.0"。流式与非流式路径表现一致。
- 同一实验里更接近生产的一例：对话早期输出预算 512 tokens，邮件工具调用完整发出；后来历史变长，框架把这一轮预算降到 102 tokens，模型在正文中间被截断，`finish_reason == "length"`，邮件照样发出，正文结束在半句 "If i"。
- Heym（issue #902）：工具循环捕获 `JSONDecodeError` 后用 `{}` 兜底，然后继续执行。对有必填参数的工具是"参数缺失"的误报；对全部参数可选的工具更糟——`close_tickets(customer_id="", older_than_days=0)` 用默认值关闭了**所有**客户的工单，trace 记录 `status: success, arguments: {}`，模型回答"Done"。

两个项目随后各自合并了修复（Langroid #1292，Heym #903，其中 #911 又补了两个漏洞）。这些是最重要的输入，但**这里的代码与方法不是它们的移植**：那两处修复都在各自框架的循环内部，而这里的做法是把"轮次完整性"抽成一个与框架无关的独立层，并补上了它们没有覆盖的历史 trace 审计。

### 1.2 为什么这不是某个框架的偶发 bug

把三条事实放在一起看，问题的结构性就出来了：

1. **参数就是模型输出**，因此和正文共享同一个输出预算。任何"写完再校验"的思路都晚了一步——被砍掉的部分不会自己长回来。
2. **预算会在运行中变化**。上面的例子已经证明：同一段提示词，早期够用，历史变长后不够用。所以这类故障集中出现在长会话里，而长会话恰恰是测试覆盖最薄的地方。
3. **失败的形状不止一种，而且互相矛盾**：
   - 有的是"少做了"（金额 1000 → 10）；
   - 有的是"多做了"（可选过滤条件退化成默认值，作用域从一行扩到全表）；
   - 有的干脆没有痕迹（参数对象合法、schema 校验通过、工具正常返回）。

第三种最难查，也是本工具存在的核心理由。

### 1.3 一条容易被忽略的关键事实

多数人凭直觉认为"参数解析失败 = 被截断"，因此在 `JSONDecodeError` 附近加判断。但截断信号在**不同 transport 上并不同质**：

| transport | 截断标志 | 参数到达时的形态 |
|---|---|---|
| Chat Completions | `choices[0].finish_reason == "length"` | 通常是 JSON 字符串片段，非法 |
| Anthropic Messages | `stop_reason == "max_tokens"` | 可能是**已经解析好的对象** |
| Gemini | `candidates[0].finishReason == "MAX_TOKENS"` | `functionCall.args` 本身就是结构化对象 |
| Responses API | `status == "incomplete"` + `incomplete_details.reason == "max_output_tokens"` | 可能已是结构化对象 |

也就是说：在 Anthropic / Gemini / Responses 上，一段被截断的参数**可以先通过 JSON 解析、再通过 schema 校验**。把防线建在"解析是否成功"上，等于在三个 transport 上没有防线。

还有一个反直觉的点，来自 Gemini 的错误语义：Google 把"函数调用写到一半被砍"大量上报成 `MALFORMED_FUNCTION_CALL` 而不是 `MAX_TOKENS`——因为它自己先解析失败了。所以"只找 MAX_TOKENS"的排查思路会漏掉其中大部分。本工具把 `MALFORMED_FUNCTION_CALL` 归入可重试类别，并在审计里用 token 用量与预算的比例去反推"这其实是一次预算截断"。

---

## 2. 现有方案与不足

| 方案类型 | 代表 | 它能做什么 | 缺什么 |
|---|---|---|---|
| 框架内部修复 | Langroid #1292、Heym #903 | 在自己那条循环里读 stop reason，拒绝执行 | 绑死单个框架；已入库的历史 trace 依旧带毒；接入第三方 loop 要重写 |
| 参数 schema 校验 | `agentic-gate` 一类（Zod/Pydantic 前置校验） | 拦住形状不合法的调用，把错误回灌 | 只看形状。被截断但合法的参数会**通过**；全可选参数的工具在默认值下也可能通过 |
| JSON 修复库 | `json_repair` | 把非法 JSON 补成合法 | 在"值会变成动作"的场景下，修复等于**发明数据**；把唯一的失败证据擦掉 |
| 可观测性/追踪 | 各类 trace 平台 | 事后看得到调用与参数 | 看到的是"成功"，没有 stop reason → 看不见 1.2 中的第三类 |
| 框架内 opt-in 守卫 | AgentScope `#3880` | 保留 provider 原始 finish reason，开关打开后拒绝"长度上限 + 参数严格解析失败"的调用 | 默认关闭；仍以"解析失败"为条件，Anthropic / Gemini / Responses 上截断但参数合法的调用会通过；同轮其余调用照常执行 |

缺的那一块很清楚：**一个与框架无关、既能装在执行路径上、又能回扫历史 trace 的最小层**；它判断的依据不是参数长什么样，而是这一轮到底有没有结束。

同一时期还有两个判断方式与本工具一致的实现，可作为旁证：Go 写的 `uncut` 把 `finish_reason` 为 `length` / `max_tokens` 直接当错误处理，不接受半截结果；Pi 的 agent loop 在检测到输出上限截断时，让同一批 tool call 全部失败。区别在于它们各自绑在自己的 harness 里，且都不处理已经落库的历史 trace。

这类问题没有随着前面两处修复消失。2026-11-09，Swarms 仓库自己报了一例（#3339）：handoff 的 tool call 被 `max_tokens` 截断后，`_arguments()` 里一句裸 `json.loads`，抛出一个光秃秃的 `JSONDecodeError`，调用方看到的是"JSON 解析失败"，既不知道原因也没法自愈；修复 PR #3344 到 2026-11-21 仍未合并。这个变体值得单独看一眼——它没有把截断当成功（比 Langroid 那例好），但它把"这一轮没写完"回报成了"参数写错了"，模型据此只会把同样的内容再发一次。

顺带一提，AgentScope 的 #3880 到 2026-11-18 还挂在 awaiting-review，未合并。所以"上游已经修好"这个前提，目前只对 Langroid 与 Heym 成立。

---

## 3. 本工具的方案

### 3.1 三条判断（也是三个实现决策）

**判断一：完整性是 turn 的属性，不是 call 的属性。**

所以入口是 `inspect(turn_id, raw_payload, calls)`，第一件事是归一化 stop 信号，只有 `COMPLETE` 才放行。`stop.py` 里的映射表覆盖四个 transport 的常见取值，并对**未知取值默认拒绝**（fail-closed）：读不懂的 stop 信号不是"大概没问题"，而是"没有任何证据说明它完成了"。

**判断二：一个 turn 里的多个 call 是一份计划，不是一个请求列表。**

默认 `atomic_turn=True`：本轮只要没有正常结束，全部 call 一起拒。理由是工具副作用常常互相依赖（先建单再扣款），执行半个计划和执行计划不是同一件事，而且模型是基于"全部都会执行"来安排后续步骤的。需要部分推进的团队可以 `atomic_turn=False`，但那种模式下放行的 call 的完整性是**未证明**的，`Decision.notes` 里会明写这一点——不假装它是安全的。

**判断三：重试预算要按"形状"计，不能按"内容"计。**

这是实现时最容易写错的地方。最自然的写法是拿参数文本算哈希当 key，但**截断恰恰让内容哈希不稳定**：模型每次重发都会在不同字节处被砍，key 每次都不同，计数器永远停在 1，于是"限制重试次数"这个保护完全失效。本工具用 `(工具名, 已闭合的顶层键集合, 正在写的键)` 作为形状签名——模型把正文改短、但结构不变时，签名稳定，预算才会真正耗尽。耗尽后的拒绝文案不再是"再试一次"，而是换成"别再原样重发，缩小载荷或拆分调用"，避免模型无限重试。

### 3.2 顺带解决的两个小问题

- **拒绝信息不能说谎。** 文案必须明确写 "was NOT executed and nothing was changed"。Heym 那例里模型之所以回 "Done"，是因为工具结果让它以为成功了。

  这条在修复合并之后又踩了一次，值得当成反例记住。Heym #903 的拒绝文案写的是 "Raise maxTokens on this node (now the provider default)"：可 Agent 面板上没有这个字段，而 Messages transport 的默认值写死在 8192，用户按提示什么也改不了。到 #904（11-15 修）才把"到底是哪个上限、去哪儿改"说清楚。拒绝文案本身是一条独立的代码路径，它也会错。
- **拒绝记录里不存参数值。** 被截断的调用，其"已经到达的那部分"同样可能含密钥；而拒绝路径天然是日志路径。所以 journal 里只留长度、12 位摘要、已闭合的键名。同时提供一个容错的 JSON 前缀扫描器，能告诉你"模型写完了 `order_id`，在写 `amount` 时被砍"——这个信息足够定位问题，又不泄漏值。

### 3.3 审计：把已经发生的事故找出来

`audit` 读 JSONL trace（格式见 `audit.py` docstring），输出四类数字：

- 有多少轮"没正常结束却仍然执行了工具"；
- 其中有多少 call 的**参数是合法的**——这些是 schema 校验层看不见的部分（invisible）；
- 有多少 call 是"文本非法但存在已解析对象"，即被 repair 过的（repaired）；
- 按工具的副作用等级（write / unknown / read）排出暴露面，unknown 默认按 write 对待。

最后一项是给排期用的：同样是 6 次越权执行，写满 4 次和只读 6 次不是一个优先级。

---

## 4. 用法

无第三方依赖，Python 3.9+。在 `tools/2026-11-17-truncated-toolcall-guard/` 目录下直接运行即可。

### 4.1 接进 agent 循环

```python
from toolcall_guard import ToolCall, ToolCallGuard

guard = ToolCallGuard(max_attempts=2)   # atomic_turn=True, on_unknown="deny"

def run_turn(raw_response, calls):
    # raw_response 必须是 provider 的原始 payload，或至少保留 stop 字段
    decision = guard.inspect(turn_id=raw_response["id"], payload=raw_response, calls=calls)

    for call in decision.allowed:          # 只有 COMPLETE 轮次才会非空
        dispatch(call)

    for err in decision.tool_results():    # 拒绝结果按 tool error 回灌
        conversation.append(err)
```

`ToolCall(call_id, name, arguments, raw_arguments)` 里的 `raw_arguments` 是关键：有原始文本时才能做前缀扫描，判断"砍在哪个字段"；Gemini 这类只给结构化对象的 transport 传 `None`，此时原子模式下仍可拒绝，但无法定位字段。

### 4.2 命令行

```bash
# 跑内置的 6 个场景，最后自动审计 examples/traces.jsonl
python -m toolcall_guard demo

# 只判断单个 provider payload（退出码 0=可执行，2=不应执行）
python -m toolcall_guard classify payload.json
python -m toolcall_guard classify payload.json --transport gemini

# 审计 trace（退出码 1=发现越权执行，可直接当 CI 门禁）
python -m toolcall_guard audit examples/traces.jsonl --side-effects examples/side-effects.json
python -m toolcall_guard audit examples/traces.jsonl --json

# 测试
python -m unittest discover -s tests -v
```

`--side-effects` 是 `{"工具名": "read"|"write"}` 的 JSON 映射；未列出的工具按 `unknown` 计，排序时与 `write` 同级处理。

---

## 5. 运行示例

`python -m toolcall_guard demo`（有删节，实际输出为完整 6 段）：

```
========================================================================
1. Cut mid-number, but the framework still handed over amount=10
========================================================================
stop verdict : truncated (output token limit reached)
allowed      : none
blocked      : ['toolu_01']
model sees   : [toolcall-guard] `refund` was NOT executed and nothing was changed.
               The turn stopped without finishing (anthropic:max_tokens - output token
               limit reached). Complete arguments before the cut: order_id. Cut while
               writing `amount`. Resend it with shorter arguments. Attempt 1 of 2 for
               this call shape.
what would have happened: a $10 refund instead of $1000.

========================================================================
2. Valid JSON on a cut turn: schema validation cannot save you here
========================================================================
stop verdict : truncated (output token limit reached)
arguments    : parse cleanly, satisfy the schema
allowed      : none
reason       : truncated

========================================================================
3. A model that keeps overrunning hits the retry budget
========================================================================
attempt 1: retryable=True reason=truncated
attempt 2: retryable=True reason=truncated
attempt 3: retryable=False reason=retry-exhausted
  -> ... This call has already been refused 2 time(s), so do not resend it as-is.
     Reduce the payload ...
```

第 2 段是重点：参数完全合法、schema 校验会放行，但本轮 `stop_reason=max_tokens`，所以仍然拒绝。这正是第 1.3 节说的那类"看不见的故障"。

审计输出（`examples/traces.jsonl`，9 个手工构造的轮次，覆盖四种 transport 与两种正确处置）：

```
trace: examples/traces.jsonl
turns: 9
stop classes: complete: 1, truncated: 5, malformed: 1, paused: 1, filtered: 1

unsafe turns that still ran tools : 6
turns correctly withheld          : 2
tool calls executed behind them   : 6
  schema-valid (invisible to arg validation): 2
  unparseable text (repair territory)      : 4
  of those, decoded args present (repaired): 2
  turns where usage >= 95% of the budget    : 1

exposure by side effect: write 4, unknown 0, read 2

by tool
  close_tickets            write    2 call(s) in 2 turn(s)
  refund                   write    1 call(s) in 1 turn(s)
  send_email               write    1 call(s) in 1 turn(s)
  get_order                read     1 call(s) in 1 turn(s)
  search_docs              read     1 call(s) in 1 turn(s)
```

"invisible: 2" 是这套数字里最有价值的一列：这两个调用带的是合法参数，任何形状校验都会放行，只有 stop reason 能识别。

---

## 6. 目录结构

```
toolcall_guard/
  stop.py     # 四种 transport 的 stop 归一化；未知值 → UNKNOWN
  args.py     # 容错 JSON 前缀扫描（已闭合键 / 当前键 / 深度）+ 形状签名
  guard.py    # 闸门：原子拒绝、重试预算、脱敏 journal、tool error 文案
  audit.py    # 回扫 JSONL trace，算越权执行与暴露面
  cli.py      # classify / audit / demo
examples/
  traces.jsonl
  side-effects.json
  payload-gemini-malformed.json
tests/
  test_guard.py   # 38 个用例：归一化、截断扫描、原子性、预算、脱敏、审计计数
```

---

## 7. 设计取舍与局限

- **只管"轮次没结束"这一件事。** 不做 token 计数、不做流式续写、不接管重试调度。它是一层判断，不是一个 harness。
- **它无法证明参数在语义上完整。** 如果模型在预算内写完了一个内容错误的参数，这里拦不住——那属于校验与评测的问题。本工具解决的是"连是否写完都不知道"。
- **`on_unknown="deny"` 会带来误拒。** 这是有意的默认值：接新 provider、或框架把 stop 字段弄丢时，宁可拒一次也不要盲执行。确认链路可靠后可改 `allow`。
- **`atomic_turn=False` 是妥协模式。** 它按"参数是否可解析"放行，等于在 1.3 节列出的三个 transport 上重新失去防线，只适合明确接受该风险的场景。
- **需要拿到原始 payload。** 不少框架在归一化 message 时把 stop reason 丢了。若你的框架如此，闸门应挂在 provider 适配层（拿到 response 的第一现场），而不是工具分发处。这也是它被做成独立库而非框架插件的原因。
- **文案只说"为什么"，不说"该改哪个旋钮"。** payload 里一般不带本轮的预算值，所以拒绝信息不会给出具体数字。#904 那个教训的方向是相反的：让人去改一个不存在的字段，比不说更糟。要提示具体上限，得在调用侧把预算一并传进来。

---

## 8. 来源

问题描述基于以下公开材料；代码为独立实现，未移植任何现有实现：

- 截断工具调用被执行的现象与两份 issue 复盘：<https://dev.to/leevar_agent/when-the-model-runs-out-of-tokens-mid-tool-call-your-agent-may-run-the-half-written-call-265e>
- 同一事件的二次报道（含 transport 差异与修复后的两个新漏洞）：<https://deniz.in/agent-frameworks-found-executing-tool-calls-cut-off-mid-argument-by-token-limits>
- Langroid issue #1291 / 修复 PR #1292：<https://github.com/langroid/langroid/issues/1291>
- Langroid 关于自身用 json-repair 处理工具 JSON 的说明：<https://github.com/langroid/langroid/blob/main/docs/FAQ.md>
- Gemini `MALFORMED_FUNCTION_CALL` 多为输出上限截断所致的排查记录：<https://gemilab.net/en/articles/gemini-api/gemini-api-malformed-function-call-error-fix>
- Gemini `FinishReason` 枚举（含 `CONTINUATION`、`TOO_MANY_TOOL_CALLS`）：<https://googleapis.github.io/js-genai/main_docs/enums/types.FinishReason.html>
- stop 字段定义：<https://platform.openai.com/docs/api-reference/chat/object>、<https://platform.openai.com/docs/api-reference/responses/object>、<https://docs.anthropic.com/en/api/messages>、<https://ai.google.dev/api/generate-content>
- 形状校验类方案的定位（作为对照）：<https://kblip.com/releases/i-built-a-small-validation-layer-that-stops-LLM-agents-from-RbFVf8M>
- 单框架内正确处理 `max_tokens` 的参考实现（作为对照）：<https://github.com/hannnnnnnny/GuardRun/blob/main/docs/architecture.md>
- `json_repair`：<https://github.com/mangiucugna/json_repair>
- AgentScope 的同类修复 PR（opt-in、以解析失败为条件）：<https://github.com/agentscope-ai/agentscope/pull/3880>
- Swarms #3339（第三例：截断后 `json.loads` 抛裸 `JSONDecodeError`）与修复 PR #3344：<https://github.com/kyegomez/swarms/issues/3339>、<https://github.com/kyegomez/swarms/pull/3344>
- Heym #904（修复后的拒绝文案指了一个用户改不了的字段）：<https://github.com/heymrun/heym/issues/904>
- Go agent harness `uncut` 把 length 当错误处理：<https://www.bytesizego.com/blog/how-uncut-handles-truncated-llm-output-and-crash-safe-sessio>
- Pi agent-loop 源码解读（截断时整批 tool call 失败）：<https://juejin.cn/post/7707151816107393075>
