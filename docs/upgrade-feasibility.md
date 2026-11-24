# 升级可行性 / Upgrade feasibility

两个问题：2 GB 机器上能不能把并发开到 2；任务和用量能不能改存 SQLite。结论在每节开头。

依据：2 vCPU / 1962 MB 内存 / 2 GB swap 的 VPS（同机跑 VPN），muse2api v1.6.12，并发 1，一个 Free 账号；2026-11-24 实测；代码引用都指 `f7f5be5`。

---

## (a) 2 GB 机器上的并发

**结论：单账号保持 `MUSE2API_CONCURRENCY=1`。并发 2 估计要多约 270 MB 峰值内存，单账号下换不来任何吞吐；真正的上限是周额度，不是并发。只有在有 2 个以上账号、又在意「对话排在视频后面等两分钟」时才值得开 2，开之前先在自己机器上量一次。**

### 实测数字

| 项 | 数值 | 来源 |
|---|---|---|
| muse2api cgroup 空闲（Chrome 已启动） | ≈ 597 MB | `systemctl show -p MemoryCurrent` |
| 一轮对话 + 生图 + 文生视频 + 图生视频的峰值 | ≈ 868 MB | `MemoryPeak` |
| 其余常驻（VPN、Caddy、fail2ban、系统） | ≈ 0.35–0.45 GB | `free -m` 的 used 减去 muse2api cgroup |
| 整机 | 1962 MB + 2 GB swap | |
| 耗时（并发 1） | 对话 16.5 s、生图 38 s、文生视频 112 s、图生视频 60 s | 冒烟脚本 |
| 额度 | 两次对话 + 生图 + 文生视频让周额度 0% → 2%（图生视频之后单独跑） | 管理接口读额度 |

### 内存账

- 一个正在生成的任务比空闲多 ≈ 868 − 597 ≈ **270 MB**（一个独立的浏览器上下文 + 标签页渲染进程，外加视频下载、缩略图）。
- 并发 2 = 同时两个任务 → 峰值估计 ≈ 868 + 270 ≈ **1.14 GB**。这是推算，没有实测：两个渲染进程会共享一部分内存，也可能因为两段视频同时下载更高。
- 给 muse2api 的空间 ≈ 1962 − 400 ≈ 1.5 GB，`MemoryHigh` 设在 1.4 GB。1.14 GB 放得下，但只剩约 0.3 GB 给页面缓存和瞬时尖峰；一个 base64 参考素材的请求体最大能到几十 MB，解码时会再翻倍。结果大概率是频繁碰 `MemoryHigh` 被限速、进 swap，延迟变差。

### 为什么单账号开并发没用

上游每次对话或生成都要先租一个账号（`AccountPoolManager.acquire` 会跳过正在忙的账号，对话路径 `safe_chat_stream` 也是 `ACCOUNT_POOL.acquire(account_id=…)`），同一个账号不能同时跑两件事。所以只有一个账号时，`GEN_LOCK` 放进来两个任务，第二个也只会在账号租约上等：内存多占了，吞吐没变。

### 为什么并发不是瓶颈

串行跑一整轮（对话 + 图 + 两段视频）≈ 226 秒，一天能跑约 380 轮；而 Free 账号不含图生视频的一轮就用掉约 2% 周额度，一周约 50 轮、一天约 7 轮。额度先用完，并发再高也没东西可跑。要更多产出，加的是账号（每个账号一份周额度），不是并发。

### 什么时候开 2

- 有 2 个以上账号，并且有对话这类对延迟敏感的请求：并发 1 时，对话会排在一段 2 分钟的视频后面。
- 先量再开：设 `MUSE2API_CONCURRENCY=2` 重启，同时提交一段视频和一个对话，看 `systemctl show muse2api -p MemoryPeak` 和 `free -m` 里的 swap；峰值超过 `MemoryHigh` 或 swap 持续增长就退回 1，或换 4 GB 机器。

---

## (b) 任务和用量改存 SQLite

**结论：上游现在用 `data/tasks.json` 存任务，量小时没问题，按我们的用量一年内都不会成为瓶颈。改 SQLite 要动 `store.py` 和 `app.py` 的几处调用，约 150 行加测试，适合先在上游开 issue 讨论。现在不改上游也能拿到大部分好处：本工具包的 meter 已经把任务、请求数、额度记进 SQLite。**

### 现状（`store.py`）

- 任务在内存里是一个 dict，每次 `create_task` / `update_task` 都把**整个** dict 序列化写回 `tasks.json`（临时文件 + `os.replace`，原子但全量）。
- 一个任务从受理到完成会更新多次（排队、开始、进度、完成），每次都全量重写。
- 不会自动清理，只能在管理页手动清（`POST /admin/tasks/clear`）。
- 没有用量记录：`usage` 恒为 0，任务里只有耗时、账号、结果文件。

按一个任务约 1 KB 算，1 万个任务时每次进度更新要重写约 10 MB。Free 账号的额度把我们限制在每周几百个任务，大约一年才到这个量级。

### 改成 SQLite 要做什么

1. `store.py` 加一个任务存储：表 `tasks(id PRIMARY KEY, kind, status, created_at, updated_at, data JSON)`，WAL 模式，用标准库 `sqlite3`，沿用现有的 `_LOCK`。
2. 保持接口不变：`create_task`、`update_task`、`get_task`、`list_tasks`、`delete_task`、`clear_tasks`。
3. `store.tasks` 被直接读的地方有 3 处（`app.py` 里图片队列准入、升级前检查、启动时把未完成任务标失败），测试里还有 3 个文件直接 `store.tasks = {}`。要么给一个兼容的映射视图，要么把这几处改成 `store.active_tasks()` 之类的查询。
4. 首次启动时把旧 `tasks.json` 导入一次，保留原文件作回退。
5. 用量：加一张 `usage_events` 表，在对话完成、生图 / 视频任务完成处各写一行（路由、账号、耗时、估算 token 或件数）。这需要在 `app.py` 的 3–4 个完成点加钩子。
6. 测试：沿用上游「脚本即测试、不碰真号」的风格，覆盖迁移、并发更新、重启后未完成任务标失败。

风险：上游迭代很快（v1.6.x 几天一版），存储层改动面大、容易冲突；维护者也可能更想保留 JSON 的简单。所以建议先开 issue 说明动机和方案，得到认可再提 PR。

### 不改上游，现在就能做的

- **meter**（本仓库 `meter/`）：每 15 分钟把 `GET /admin/tasks` 的任务镜像进 SQLite（不存提示词原文），从 journald 的访问日志按小时、路由、状态码计数，记录额度百分比；`report` 给出每 1% 周额度大约做了多少次请求。用 `install.sh --meter` 打开。
- **控制 `tasks.json` 的大小**：meter 已经存了历史后，可以定期只保留最近的任务，例如 `POST /admin/tasks/clear`，body `{"keep": 200}`。本工具包没有自动做这一步，等真的变大再加。
- **用量估算**：[`upstream-prs/02-estimated-usage`](../upstream-prs/02-estimated-usage.md) 是一个可选开关（默认关）的上游补丁草稿，对话接口按字符粗估 token，已在上游测试套件里跑过。

---

## English summary

- **Concurrency on 2 GB**: keep 1 with a single account. One extra active slot costs about 270 MB (measured idle 597 MB vs peak 868 MB at concurrency 1, so concurrency 2 is estimated at about 1.14 GB peak under a 1.4 GB `MemoryHigh`), and upstream leases one account per job, so a single account gets zero extra throughput. The binding limit is the weekly quota (about 2% per two chats + one image + one 10 s video on a Free account, so about 50 rounds a week), not concurrency. Consider 2 only with 2+ accounts and latency-sensitive chat, and measure first.
- **SQLite**: upstream rewrites the whole `tasks.json` on every task update and keeps no usage records. A SQLite task store is about 150 lines plus tests touching `store.py` and 3 direct `store.tasks` reads in `app.py`; propose it upstream as an issue first. Today, without patching upstream, the kit's meter mirrors tasks, request counts and quota into SQLite.
