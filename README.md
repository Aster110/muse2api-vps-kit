# muse2api-vps-kit

把 [czg86389-hub/muse2api](https://github.com/czg86389-hub/muse2api) 装到**一台小 VPS**（2 核 / 2 GB 就行）上的部署工具包：一条命令装好、服务器上直接登录账号、冒烟测试、按额度计量。

> **致谢与边界**：muse2api 由 [@czg86389-hub](https://github.com/czg86389-hub) 及贡献者开发，MIT 许可。本仓库**不 fork、不修改上游代码**：`install.sh` 从上游仓库拉取并锁定一个 commit（默认 v1.6.12 `f7f5be5`）原样运行。唯一涉及上游代码的是 [`upstream-prs/`](upstream-prs/) 里的补丁，已作为 [#66](https://github.com/czg86389-hub/muse2api/pull/66)、[#67](https://github.com/czg86389-hub/muse2api/pull/67) 提给上游，本工具包不使用它们。

> **先看风险**：muse2api 驱动的是 Muse（Meta）的**网页端**，不是官方 API。这样用很可能违反 Meta 的服务条款，账号可能被封。只用不心疼的备用账号、只自己用，不要转售或公开成服务。详见文末[风险](#风险)。

## 适用条件

- 一台 Ubuntu 22.04+ / Debian 12+ 的 VPS（实测 Ubuntu 26.04），root 权限，x86_64（arm64：Debian 会装 chromium，Ubuntu 请自备并用 `--chrome` 指定）。实测机器：2 vCPU / 1962 MB 内存，同机还跑着 VPN。
- **VPS 在 Muse 开放的地区**：Meta 帮助页写的 limited testing 地区是美国、加拿大、日本、新加坡（以 Meta 当前说法为准）。判断看的是 VPS 的出口 IP。
- 一个能登录 muse.ai 的账号，以及能收到验证码的邮箱。

## 快速开始

```bash
git clone https://github.com/Aster110/muse2api-vps-kit.git && cd muse2api-vps-kit
sudo bash install.sh                  # 要对外提供 HTTPS 时加：--domain api.example.com
sudo /opt/muse2api/venv/bin/python /opt/muse2api/kit/login/muse_login.py --email you@example.com
sudo python3 /opt/muse2api/kit/smoke.py
```

1. `install.sh`：装 Google Chrome、Python venv，拉上游并锁 commit，建专用用户 `muse2api`，生成 key，装 systemd 服务（只听 `127.0.0.1:18610`），内存不足 3 GB 时加 2 GB swap。结尾自检 `/healthz`、带 key / 不带 key 的 `/v1/models`，并以服务用户在同样的 systemd 沙箱里试启动一次 Chrome。**不打印任何 key**，只告诉你在哪个文件。
2. `muse_login.py`：在 VPS 上用无头 Chrome 登录 muse.ai，Muse 把验证码发到你邮箱，你看到后在终端里输入；登录成功后把会话导入 muse2api 并做账号测试。遇到人机验证、身份核验、条款、注册、等候名单页面会停下并截图，不替你点「同意」。
3. `smoke.py`：对话、生图、文生视频、图生视频各调一次，打印一张小表并写 JSON 报告。会用掉一点周额度（实测一整轮约 2%），只想测对话用 `--only models,chat`。

调用（在 VPS 上）：

```bash
sudo sh -c '. /etc/muse2api/muse2api.env; curl -s http://127.0.0.1:18610/v1/chat/completions \
  -H "Authorization: Bearer $MUSE2API_KEY" -H "Content-Type: application/json" \
  -d "{\"model\":\"muse-spark\",\"messages\":[{\"role\":\"user\",\"content\":\"hi\"}]}"'
```

开了 `--domain` 时，外部用 `https://api.example.com/v1`，`Authorization: Bearer <MUSE_FRONT_KEY>`（在 `/etc/muse2api/front.env`）。443 被占用（比如 VPN）时加 `--https-port 18443`；80 端口要留给证书签发（ACME HTTP-01）。

管理页：在你自己电脑上 `ssh -L 18610:127.0.0.1:18610 root@<vps>`，打开 `http://127.0.0.1:18610/admin`，把 key **粘贴到页面输入框**里，不要用 `?key=`。

## 工具包加了什么

| | 上游裸机部署 | 本工具包 |
|---|---|---|
| 监听 | README 示例 `0.0.0.0:18610`，管理页 `?key=` | 只听 `127.0.0.1`；可选 Caddy HTTPS 前置，下游用独立前置 key，内部 key 不出机器 |
| 版本 | 管理页可一键在线升级 | 锁 commit；代码目录归 root；`MUSE2API_MANAGED_DEPLOYMENT=1` 让一键升级返回 409 |
| 小内存 | 未写内存要求（compose 给 Chromium 2 GB shm）；并发未设置时默认 8 | 并发 1（< 3 GB）、`OOMScoreAdjust=600`、`MemoryHigh` = 70% 内存、自动 swap |
| 账号 | 在本机浏览器登录后推 Cookie | 在 VPS 上从同一出口 IP 登录（验证码可手动输入 / IMAP / JSON 邮箱接口） |
| 验证 | 管理页「测试」 | `smoke.py` 一次跑通四种能力，202 / 轮询 / data URL 都处理对 |
| 计量 | `usage` 恒为 0 | `meter/`：周额度 %、任务、按路由请求数进 SQLite，`report` 给出每 1% 额度的请求数 |

所有路径和端口都能改（`bash install.sh --help`），同一台机器可以再装一个互不干扰的实例：`--name`、`--user`、`--prefix`、`--state-dir`、`--etc-dir`、`--port`、`--cdp-port`。

## 踩过的坑

详见 [docs/pitfalls.md](docs/pitfalls.md)，每条都是「现象 → 原因 → 处理」：

1. 2 GB 内存 + 无头 Chrome：空闲约 597 MB、峰值 868 MB → swap、`OOMScoreAdjust`、并发 1。
2. Muse 只在部分地区开放 → 看 VPS 出口 IP 的地区。
3. 上游只能导入 Cookie → 在服务器上登录的脚本。
4. 生图任务回 202 → 是受理，要轮询。
5. 回环 / 内网图片 URL 被拒（上游有意的 SSRF 防护）→ 用 data URL。
6. 管理 key 走 URL `?key=`（会进访问日志）→ 只听回环 + 前置 key + SSH 隧道。
7. `usage` 恒为 0 → 计费要另找计量。

外加两条：不用一键在线升级，锁 commit 读 diff；同机两个实例必须用不同的 CDP 端口，否则第二个会接管第一个的 Chrome。

## 安全模型

- **两把 key，都不打印**：`/etc/muse2api/muse2api.env` 里的 `MUSE2API_KEY`（上游的管理兼 API key，只在本机用）；`/etc/muse2api/front.env` 里的 `MUSE_FRONT_KEY`（发给下游）。两个文件 root 0600。Caddy 通过 systemd drop-in 从这两个文件读环境变量，Caddy 配置文件里没有密钥。
- **对外只有前置**：muse2api 只听 `127.0.0.1`；Caddy 只放 `/v1/*` 和 `/healthz`（都要前置 key，Caddy 换成内部 key 再转发），`/v1/media/*` 免 key（文件名随机），`/admin`、`/` 等一律 404；请求体上限 60 MB（`--max-body`），避免一个大 base64 请求把 2 GB 机器撑爆。
- **服务进程**：专用系统用户，代码目录归 root 不可写，`NoNewPrivileges`、`PrivateTmp`、`ProtectSystem=full`、`ProtectHome`。账号 Cookie 在 `/var/lib/muse2api/data/accounts.json`（上游格式，明文），目录 0700，别放进 git 或备份到公共位置。
- **已知限制**：Caddy 管理接口默认在 `localhost:2019`，本机任何进程都能读到已加载的配置（含两把 key）。本工具包假设 VPS 只有你自己用；多人共用的机器请在 Caddyfile 全局块加 `admin off`，改用 `systemctl restart caddy`。轮换 key：改 env 文件里的值，然后 `systemctl restart muse2api caddy`（不要用管理页的「轮换」按钮，它写的是另一个 `.env`，重启后会被 env 文件覆盖）。

## 计量

```bash
sudo bash install.sh --meter          # 加装 muse2api-meter.timer，每 15 分钟采一次
sudo python3 /opt/muse2api/kit/meter/muse_meter.py report --db /var/lib/muse2api-meter/meter.sqlite --days 7
```

每次采样：读 muse2api 已缓存的额度（不开浏览器）、把任务镜像进 SQLite（不存提示词）、从 journald 的访问日志按小时 / 路由 / 状态码计数（不存 URL 参数）。实时读额度（`POST /admin/accounts/{id}/quota`，约 8 秒，会占生成锁）默认每 6 小时最多一次，有任务在跑时跳过，最小间隔 1 小时（`--meter-quota-every`）。

## 实测（2 vCPU / 2 GB，并发 1，一个 Free 账号，2026-11-24）

| 能力 | 模型 | 耗时 | 结果 |
|---|---|---|---|
| 对话 | `muse-spark` | 16.5 s | 文本，`usage` 为 0 |
| 生图（`/v1/images/tasks`） | `muse-image` | 38 s | webp |
| 文生视频 | `muse-video` | 112 s | 8.1 MB mp4 |
| 图生视频（data URL） | `muse-video` | 60 s | mp4 |

对话两次、生图、文生视频这一轮让周额度从 0% 到 2%（图生视频是之后单独跑的）。muse2api 服务 cgroup 内存：空闲约 597 MB，峰值约 868 MB。2 GB 上能不能开并发 2、任务改存 SQLite 要多大改动，见 [docs/upgrade-feasibility.md](docs/upgrade-feasibility.md)。

## 升级与卸载

升级：先在 `/opt/muse2api/app` 里 `git fetch` 并 `git diff` 读改动，再 `sudo bash install.sh --commit <新 commit>`（保留 key、账号和数据，配置不变时不重启）。

卸载（默认路径；用了 `--name` 等参数时换成对应路径）：

```bash
sudo systemctl disable --now muse2api
sudo systemctl disable --now muse2api-meter.timer     # 装过 --meter 时
sudo rm -f /etc/systemd/system/muse2api.service /etc/systemd/system/muse2api-meter.service /etc/systemd/system/muse2api-meter.timer
sudo rm -rf /opt/muse2api /var/lib/muse2api /var/lib/muse2api-meter /etc/muse2api
sudo userdel muse2api
sudo systemctl daemon-reload
```

用过 `--domain` 时再删 `/etc/caddy/conf.d/muse2api.caddy` 和 `/etc/systemd/system/caddy.service.d/muse2api-front.conf`，然后 `sudo systemctl daemon-reload && sudo systemctl restart caddy`。swap 和 Chrome 不会自动删：`sudo swapoff /swapfile`，删掉 `/etc/fstab` 里的 `/swapfile` 那行和 `/swapfile`；`sudo apt remove google-chrome-stable`。

## 验证

不写测试和注释，上线后在生产读回（装好后跑 `smoke.py`）。

- `install.sh` 在实测 VPS 上用隔离的名字、用户、目录和端口装过一遍（不碰在跑的实例），再跑一遍确认幂等（不重启），然后按字面路径删干净。
- `--domain` 的 Caddy 前置：模板用 Caddy 在本地对着假服务跑过（前置 key 换内部 key、无 key 401、`/admin` 404、媒体免 key、超大请求体 413），同样的指令在实测 VPS 的 Caddy 2.6.2 上跑着生产流量；但 `install.sh` 里安装和配置 Caddy 的那一段没有在测试机上跑（那台机器的 Caddy 正在服务）。
- 登录脚本需要真人收验证码，只做了静态检查（`--help`、编译）。

## 路线图

- [x] 两份上游 PR 已提（11-24）：[部署要点文档 #66](https://github.com/czg86389-hub/muse2api/pull/66)、[可选的用量估算 #67](https://github.com/czg86389-hub/muse2api/pull/67)，草稿和测试记录在 [upstream-prs/](upstream-prs/)。
- [ ] 上游的验证码登录（PR #42）合并后，登录改用上游实现。
- [ ] meter 可选读 Caddy 的 JSON 访问日志，区分被前置 key 挡掉的请求。
- [ ] 第二个账号到位后，在 2 GB 机器上实测并发 2 的内存峰值。

## 风险

- **不是官方 API**：muse2api 驱动 Muse 的网页应用。Meta 改版就可能坏（上游 v1.6.12 就是在修 11-19 的改版），没有任何可用性保证。
- **服务条款与封号**：用自动化方式使用网页端很可能违反 Meta 的服务条款，社区里有不少封号的报告。只用备用账号，不要用和你主 Facebook / Instagram 绑定的账号；一个账号固定一个出口 IP，不要频繁换。
- **只自用**：不要转售、不要包装成公开的 API 服务。本仓库不提供、也不讨论批量注册或购买账号。
- **数据**：账号 Cookie 明文存在服务器上，拿到这台机器 root 的人就能用你的账号。

## 许可

本仓库 MIT（见 [LICENSE](LICENSE)）。上游 muse2api 同为 MIT，版权归其作者，见 [NOTICE](NOTICE)。

---

## English

**muse2api-vps-kit** deploys [czg86389-hub/muse2api](https://github.com/czg86389-hub/muse2api) (MIT, by @czg86389-hub and contributors) on one small VPS (tested on 2 vCPU / 2 GB, Ubuntu). It does not fork or modify upstream code: it clones upstream at a pinned commit (v1.6.12 by default) and runs it unchanged. Patches for upstream (submitted as #66 and #67) live in `upstream-prs/` and are not used by the kit.

Quickstart (VPS in a region where Muse is open: US, Canada, Japan or Singapore per Meta's limited-testing pages):

```bash
git clone https://github.com/Aster110/muse2api-vps-kit.git && cd muse2api-vps-kit
sudo bash install.sh                  # add --domain api.example.com for an HTTPS front
sudo /opt/muse2api/venv/bin/python /opt/muse2api/kit/login/muse_login.py --email you@example.com
sudo python3 /opt/muse2api/kit/smoke.py
```

What it adds: loopback-only service with an optional Caddy HTTPS front that checks a separate front key and swaps it for the internal admin key (only `/v1/*` and `/healthz` exposed, `/v1/media/*` keyless, everything else 404); pinned commit with the in-app upgrade disabled; small-RAM defaults (concurrency 1, `OOMScoreAdjust=600`, `MemoryHigh` at 70% of RAM, swap); a server-side email-code login so the session is created from the VPS's own IP (stops on captcha, checkpoint, terms, sign-up or waitlist pages, never clicks "agree"); a smoke test that handles `202` + polling and passes the generated image to image-to-video as a data URL; and a SQLite meter for weekly-quota %, tasks and per-route request counts, because muse2api always reports `usage = 0`.

Risks: this drives the Muse **web** app, not an official API. It may violate Meta's terms and accounts can be banned. Use a spare account, for personal use only; do not resell it as a service. See [docs/pitfalls.md](docs/pitfalls.md) and [docs/upgrade-feasibility.md](docs/upgrade-feasibility.md) for the details behind each default.
