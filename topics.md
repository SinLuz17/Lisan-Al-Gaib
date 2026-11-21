# 题目记录

> 做过的东西记在这里，动手前先翻一遍，避免重复。

| 日期 | 主题 | 说明 |
|------|------|------|
| 2026-11-17 | toolcall-guard：轮次截断下的工具调用守卫 | 把四家 provider 的 stop 信号归一化，在本轮未正常结束时整体拒绝执行 tool call，并回扫历史 trace 统计已发生的越权执行 |
| 2026-11-21 | wirecheck：工具 schema 的多 provider 可移植性检查 | 把一份 JSON Schema 编到四家 provider，分别列出会被拒、能过但不生效、文档没定的构造，并把被移走的约束写回 description |
