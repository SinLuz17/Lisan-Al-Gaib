# mcpaudit: server 接入面盘点

**MCP server 的"能不能信任"不写在配置里，散在 advisory、transport 形状和工具标注三个地方。这个工具把三处并成一份清单。**

零依赖 Python（库 + CLI）。输入是 MCP 客户端配置文件（Claude Desktop、`.mcp.json`、VS Code、Cursor 的形状都吃），输出每个 server 的判定：

1. **版本边界**：从 `command`/`args` 里抠出包名和钉住的版本，对照一份带来源链接的 advisory 快照，落在受影响区间内的直接点名；
2. **transport 暴露**：stdio 还是远程 HTTP；远程且没带认证头的，单独列出——那是浏览器可触达的一类（DNS rebinding / CSRF，有真实案例）；
3. **标注矛盾**：`readOnlyHint: true` 的工具，名字或参数里带 `exec` / `write` / `delete` 这类信号的，标成 trust-label mismatch；
4. **env 盘点**：只列传给 server 进程的敏感变量名，不打印值。

```
python -m mcpaudit scan examples/desktop.json
python -m mcpaudit scan .mcp.json --json
python -m mcpaudit advisories
```

退出码：`0` 干净；`1` 有发现（可直接当检查门禁）；`2` 用法错误（读不到文件、不是认识的 JSON 形状）。

---

## 1. 为什么动手

2026 年 11 月 18 日前后，MCP 生态一口气披露了十几个 advisory。把它们并排放，会发现被钻空子的不是同一处，而是三个不同的"边界画错了"：

- **版本边界**：mcp-server-git 的 `git_status` 这类只读工具会执行仓库自己 `.git/config` 里配的命令，11-11 修了一轮（regex 过滤 filter driver），11-18 又修了四个绕过——空名字的 `[filter ""]` 就能溜过那个 regex。TypeScript SDK 的三个 CVE（task store 不分 session、HTTP 校验耗尽内存、跨域重定向带走 `mcp-session-id` 和 OAuth refresh token）全在 1.32.0 才修。
- **transport 边界**：Jupyter MCP Server 的 `/api/connect`、`/api/stop` 在 HTTP 模式下没挂认证，恶意网页通过 DNS rebinding 就能碰到；todoist-mcp 的 HTTP 变体是同一个病，GHSA 里写着 PoC 存在。stdio 跑在本机的 server 没这个暴露面——**transport 形状本身就是一条安全属性**，但没人在接之前看一眼。
- **标注边界**：DOMShell 给 `domshell_eval` 标了 `readOnlyHint: true`，实际经 CDP 的 `Runtime.evaluate` 跑任意 JS。Claude Desktop 和 Cursor 按这个标注渲染"只读"的批准 UI——**客户端信的是 server 自己的声明**。

这三类里的前两类，对接的人手里都有现成信息：配置文件就躺在那儿，包名和版本写在 `args` 里。没人把它们并成一张表，是因为每条单独看都不值得写代码——直到出事的那天。

有个坑值得单独记一笔：`@modelcontextprotocol/server-filesystem` 的修复版本是 `2.3.0`（后来又进到 `2.4.0`），而它的旧版本是日期格式的（`2025.x.x`）。npm 的版本排序里 `2.3.0` 排在所有日期版本**前面**，把 `"@modelcontextprotocol/server-filesystem": "latest"` 换成写死版本号的人很容易钉回一个仍然带洞的日期版本。版本比较器里专门处理了这种混排。

## 2. 现有做法与不足

| 做法 | 能做什么 | 缺什么 |
|---|---|---|
| 客户端的批准 UI（Claude Desktop / Cursor） | 首次接入时让人确认 | 信息只有 server 自己声明的标注；DOMShell 那例里 UI 忠实地展示了错误的 `readOnlyHint` |
| 安全披露文章 | 逐条讲清楚每个 advisory | 不对着你的配置讲；十几个 advisory 分散在不同 GHSA，没人把它们落到"我这台机器上哪个 server 中枪" |
| `pip audit` / `npm audit` 一类 | 依赖树里的已知漏洞 | 不知道 MCP 配置里 `npx -y` 拉的是什么；`uvx` 的 `--from` 形状解析不了；也不看 transport 和标注 |
| 手工过一遍配置 | 什么都做得到 | 十个 server × 四类检查，靠肉眼必漏 |

缺的那块：**以配置文件为输入，把"版本是否踩雷、transport 是什么形状、标注可不可信、env 传了什么"四件事一次说清**。

## 3. 三个判断

### 3.1 包名从启动命令里抠，不从包管理器里猜

MCP 配置没有统一的"包名 + 版本"字段，只有 `command` 和 `args`。所以解析规则按真实世界出现过的形状来：

- `npx -y pkg` / `npx -y pkg@1.2.3`（npm 包，版本可能钉可能没钉）；
- `uvx pkg` / `uvx pkg==1.2.3` / `uvx --from pkg tool`（PyPI 包）；
- `docker run image:tag`（镜像名，tag 当版本）；
- 直接 `node server.js` / 本地脚本：没有包管理器参与，版本无从谈起，如实标 `local`，不做 advisory 匹配。

`npx -y` 且没钉版本的，单独给一条发现：**没钉版本意味着每次启动都在拉 latest，今天干净不代表下个月干净**。这一条只是提醒，不进退出码——不然所有没钉版本的配置都过不了 CI，等于逼人删工具。

### 3.2 版本比较要同时吃得下 `2026.11.11` 和 `2.3.0`

快照里的修复版本有两种历法混着：日期式的（mcp-server-git 的 `2026.11.11` → `2026.11.18`）和语义式的（filesystem 的 `2.3.0`）。比较器按纯数字逐段比，两种格式天然可比；对 `alpha` / `beta` / `rc` 后缀单独处理（排在正式版之前）。npm 那个排序陷阱不需要特判——逐段数字比较下 `2.3.0 < 2025.1.0` 是错的，但**受影响区间是按"哪些版本有洞"逐条声明的**，不是按"小于某个版本"一刀切，所以日期版本踩洞、`2.3.0` 干净这类事实能直接表达。

### 3.3 标注矛盾是"签名 + 信号"，不是"名字黑名单"

`readOnlyHint: true` 的工具里出现 `exec` / `run` / `write` 就一定有问题吗？不。有些工具真的只读（`run_query` 对只读副本）。所以工具不直接报"危险"，而是把三件事放在一起让人自己判：

- 名字与参数名里的执行类信号词（`eval`、`exec`、`evaluate`、`runtime`、`write`、`create`、`delete`、`drop`、`install`、`send`…）；
- 参数 schema 里有没有"代码体"形状的参数（名字含 `code`、`script`、`source`、`expression`，类型是字符串）；
- 有没有别的工具标了 `destructiveHint: true` 但这个标了只读（同一个 server 内自相矛盾，比单条更硬）。

第三条最值得看：DOMShell 同一个 server 里如果既有 `readOnlyHint` 的 eval 又有 `destructiveHint` 的别的工具，矛盾一眼可见。检查是提示性的，单独一条不改变退出码——它没有 advisory 那种确定性，把它算成硬失败会逼人给 `run_query` 改名。

## 4. 用法

无第三方依赖，Python 3.9+。在 `tools/2026-11-21-mcp-server-audit/` 下运行。

### 4.1 命令行

```bash
# 扫一份配置
python -m mcpaudit scan examples/desktop.json

# 机器可读（给脚本吃）
python -m mcpaudit scan examples/desktop.json --json

# 一次扫多份
python -m mcpaudit scan .mcp.json ~/.claude/claude_desktop_config.json

# 只列快照里的 advisory 及来源
python -m mcpaudit advisories

# 测试
python -m unittest discover -s tests -v
```

认识的配置形状：`{"mcpServers": {...}}`、`{"servers": {...}}`、`{"mcp": {"servers": {...}}}`，顶层直接是 server 映射也行。远程 server 用 `url`（或 `type: "http"/"sse"` 加 `url`），本地用 `command`。

### 4.2 当库用

```python
from mcpaudit import load_config, audit_server, all_advisories

servers = load_config(open("examples/desktop.json"))
for name, server in servers.items():
    for finding in audit_server(name, server):
        print(finding.severity, name, finding.kind, finding.detail)
```

`Finding(kind, severity, detail, refs)` 里 `refs` 是来源链接，只在 advisory 类发现上非空。

---

## 5. 运行示例

`examples/desktop.json` 是按真实配置形状构造的一份，故意埋了四个问题。`python -m mcpaudit scan examples/desktop.json`（有删节）：

```
config: examples/desktop.json

[filesystem]
  HIGH   advisory      @modelcontextprotocol/server-filesystem (npm) 2025.7.15 hits >=2025.1.1
         — two symlink swaps after path validation reach files outside allowed dirs;
         fixed in 2.3.0 (also pulled into 2.4.0); npm sorts 2.3.0 before every 2025.x
         date version; pin explicitly

[git]
  HIGH   advisory      mcp-server-git (pypi) 2026.5.0 hits <2026.11.18
         — repo-controlled git config runs commands through read-only tools
         (filter drivers, promisor remote lazy fetch, gc.recentObjectsHook, config include)

[jupyter]
  MEDIUM remote-unauthenticated   remote http/sse server without auth headers;
         browser-reachable (DNS rebinding / CSRF class)

[search]
  INFO   env-sensitive  passes env var(s) SEARCH_API_TOKEN to the server process
         (names only, values not read)

[shell]
  HIGH   advisory       domshell (npm) 2.0.5 hits <2.0.8
         — domshell_eval runs arbitrary JS via CDP while declaring readOnlyHint: true
  MEDIUM label-mismatch tool domshell_eval declares readOnlyHint but name carries
         action word(s): eval; code-shaped parameter(s): expression; same server also
         marks destructiveHint: domshell_reset_profile

5 finding(s) worth acting on (exit 1)
```

几个值得看的点：

- `filesystem` 命中的是 `>=2025.1.1` 而不是 `<2.3.0`——它的版本是日期式的，而修复版本 `2.3.0` 在 npm 的排序里排在所有日期版本前面。两种历法混排是快照里最阴的一条，比较器不跨历法硬比，范围两头都写。
- `shell` 同时命中 advisory 和 label-mismatch：版本是带洞的 `2.0.5`，工具标注本身也和形状矛盾。两层各自独立，不合并——升级到 `2.0.8` 解决版本，解决不了"标注说得对不对"。
- `search` 是本地脚本，没有包管理器参与，advisory 匹配如实跳过（标 `local`），只报 env 变量名。

`--json` 输出同结构的机器可读版，`finding.kind` 取 `advisory` / `unpinned` / `remote-unauthenticated` / `label-mismatch` / `env-sensitive` / `local`。

## 6. 目录结构

```
mcpaudit/
  extract.py     # command/args → (ecosystem, package, version)；transport 判定
  versions.py    # 混合历法版本比较（日期式 + 语义式 + 预发布后缀）
  advisories.py  # 快照：受影响区间、修复版本、来源链接；匹配逻辑
  labels.py      # readOnlyHint 矛盾检查（信号词 + 代码体参数 + server 内互斥）
  cli.py         # scan / advisories；报告与退出码
examples/
  desktop.json   # 构造的配置，含四类发现
tests/
  test_mcpaudit.py
```

## 7. 局限与取舍

- **快照会过时，这是这个工具的宿命。** advisory 列表是 2026-11-21 的手工快照，每条带来源；新的 advisory 要人往里加。`unverified` 这个思路在 schema 检查那套里用过一次，这里反过来：不做"未定档"，只收有来源的条目——安全披露是离散事件，没有"文档没说"这一档。
- **配置里通常没有工具清单。** 大多数客户端配置只到 server 为止，工具定义是运行时拉取的。所以标注检查只有在配置或 `--tools` 文件里真给了工具时才生效，没有就跳过并写明——不猜。
- **`npx -y` 不钉版本报的是"不可审计"，不是"有洞"。** 这两个是不同的事；混在一起会把提醒变成噪音。
- **env 检查只看变量名。** 名字里有 `TOKEN` / `KEY` / `SECRET` / `PASSWORD` / `CREDENTIALS` 的列出来，值一律不碰、不进 `--json`。想确认值有没有泄漏不是静态检查的活儿。
- **只认本地路径的 `$ref` 这类 schema 复杂度不在这的射程内**——工具定义级别的 schema 检查是 wirecheck 的事，这个工具对 tools 只看标注和参数名形状，不重复造一遍 schema 解释器。

## 8. 来源

advisory 快照与论述基于以下公开材料；代码为独立实现：

- 11 月 MCP 只读标注/版本修复批次总览（含 GHSA/CVE 编号与修复版本表）：<https://cyberstrategyinstitute.com/read-only-mcp-not-a-security-boundary>
- mcp-server-git 2026.11.18 修复批次（含空名 filter driver 绕过 GHSA-pm7p-vphh-2cg7）：<https://mindpattern.ai/s/2026-11-20-mcp-tools-guard-the-path-their-authors-pictured-and-leave-the-next-one-open>
- servers 仓库 v2.4.0 修复清单（git / filesystem / fetch，含 filesystem npm 版本序陷阱）：<https://artofcode.in/blog/ai-citations-not-proof-edtech>
- TypeScript SDK 1.32.0 的三个 CVE（task store 越权、校验 DoS、重定向头泄漏）：<https://psirt.com/de/advisory/CLSA-2026-1794695478>
- 同批次的修复版本汇总报道：<https://undercodenews.com/mcp-servers-release-eight-security-fixes-after-git-configuration-flaw-exposes-ai-agents-to-code-execution-video>
