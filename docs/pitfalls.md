# 踩过的坑 / Pitfalls

在一台 2 vCPU / 2 GB 内存、同时跑着个人 VPN 的美西 VPS 上部署 muse2api v1.6.12（2026-11-24）时遇到的问题。每条都是「现象 → 原因 → 本工具包怎么处理」，数字来自那次部署的实测记录。

---

## 1. 2 GB 内存 + 无头 Chrome（2 GB RAM and headless Chrome）

**现象**：上游 README 没写内存要求，docker-compose 给 Chromium 开了 2 GB 共享内存，我们动手前估的是 4 GB 起；手上只有 2 GB，而且同机还有一个不能被挤掉的 VPN 进程。

**原因**：muse2api 用一个无头 Chrome 驱动 muse.ai 网页。实测 systemd cgroup 内存：Chrome 起来后空闲约 **597 MB**；跑一轮对话 + 生图 + 文生视频 + 图生视频，峰值 **868 MB**（`MemoryPeak`）。整机 1962 MB，其余进程常驻约 0.4 GB，没有余量给第二个并发标签页，更没有余量给 OOM 时误杀 VPN。

**处理**（`install.sh` 自动做）：
- 内存 < 3 GB 且没有 swap 时加 2 GB `/swapfile`（`--no-swap` 关掉）。
- systemd 里 `OOMScoreAdjust=600`：真到 OOM，内核先杀 muse2api 和它的 Chrome（子进程继承分数），不杀 sshd / VPN。
- `MemoryHigh` = 内存的 70%（2 GB 机器约 1.4 GB）：超过就先回收、限速，而不是直接 OOM。
- 内存 < 3 GB 时 `MUSE2API_CONCURRENCY=1`。注意：上游 `config.py` 在**没设置这个变量时默认 8**（`.env.example` 写的是 2），裸机部署不写 `.env` 就是 8。并发为什么在单账号下没用，见 [upgrade-feasibility.md](upgrade-feasibility.md)。

## 2. Muse 只在部分地区开放 → 出口 IP 要在开放地区（Region availability → exit IP）

**现象**：账号在国内或其他地区登录 muse.ai 只能进等候名单。

**原因**：Muse 目前是 Meta 的 limited testing，帮助页列的是美国、加拿大、日本、新加坡（以 Meta 帮助页当前说法为准）。判断看的是**发请求的那台机器的出口 IP**，不是你注册时在哪。

**处理**：VPS 选在开放地区。部署前可以先探一下入口：`curl -s -o /dev/null -w '%{http_code}\n' https://muse.ai/api/session`，没登录时回 401（Authentication required）说明入口没拦这个 IP；地区不对通常直接进 waitlist 页面。机房 IP 能不能生成要等真号验证；muse2api 支持给每个账号挂 HTTP / SOCKS5 代理兜底。

## 3. 上游只能导入 Cookie → 服务器上直接登录（Upstream only imports cookies → server-side login）

**现象**：muse2api 的账号来源只有「浏览器扩展 / 油猴脚本推送 Cookie」：你在自己电脑的浏览器里登录，再把 `hatch_sess` 等 Cookie 推到服务器。会话在你家 IP 上建立、却在 VPS 的 IP 上使用；而且你本机未必在开放地区。

**处理**：[`login/muse_login.py`](../login/muse_login.py) 在 VPS 上用无头 Chrome 从 VPS 自己的出口登录：填邮箱 → 取一次性验证码（默认你从邮箱里看到后手动输入；也支持 IMAP 或一个返回 JSON 的邮箱接口）→ 等 `/api/session` 返回 200 → 读 Cookie → `POST /admin/accounts`（同名账号则更新 Cookie）→ 账号测试。遇到人机验证、身份核验、条款、注册、生日、等候名单页面一律停下并截图（退出码 3），**不点任何「同意」**。上游有一个还没合并的验证码登录 PR（#42），合并后可以改用上游的。

## 4. 生图任务回 202 → 是受理，要轮询（Image task 202 → poll）

**现象**：我们第一版冒烟脚本对 `POST /v1/images/tasks` 判断 `status == 200`，把正常受理的生图判成失败。

**原因**：`/v1/images/tasks` 按设计返回 **HTTP 202** 和任务 `id`（上游 README「v1.5.2」一节写了），要轮询 `GET /v1/images/tasks/{id}` 到 `completed` / `failed`。`POST /v1/videos` 则返回 200 + `status: queued`，同样要轮询。

**处理**：[`smoke.py`](../smoke.py) 把 200 和 202 都当受理，轮询到终态。实测：生图 38 秒（webp），文生视频 112 秒（8.1 MB mp4），图生视频 60 秒，对话 16.5 秒。

## 5. 私网图片 URL 被拒 → 用 data URL（Private image URL rejected → data URL）

**现象**：图生视频时把刚生成的图片地址 `http://127.0.0.1:18610/v1/media/xxx.webp` 当 `image_url` 传，被拒绝。

**原因**：这是上游有意的 SSRF 防护（`engine.py` 的 `_open_public_ref_image`）：参考图 URL 逐跳解析 DNS，只允许 `ip.is_global` 的公网地址。回环和内网地址都会被拒，除非显式设 `MUSE2API_REF_IMAGE_PRIVATE_FETCH=1`（只适合可信的单用户部署）。

**处理**：先把图片下载下来，转成 `data:image/webp;base64,...` 再传。`smoke.py` 的图生视频步骤就是这么做的。不建议为了省事打开私网抓取开关。

## 6. 管理 key 走 URL（`?key=`）→ 只听回环 + 前置 key + SSH 隧道（Admin key in the URL → loopback + front key + SSH tunnel）

**现象**：上游 README 让你用 `http://<IP>:18610/admin?key=<MUSE2API_KEY>` 打开管理页；同一把 `MUSE2API_KEY` 也是 `/v1` 的 API key。我们的机器上，journald 的 muse2api 日志里确实留下了一条带 `?key=` 的 `GET /admin` 记录。

**原因**：带 key 的 URL 会进 uvicorn 访问日志、反代日志、浏览器历史；把 API key 发给下游等于把管理权（删账号、看 Cookie 状态、轮换 key）一起发出去。

**处理**：
- 服务只听 `127.0.0.1`（`install.sh` 写死）。
- 加 `--domain` 时装 Caddy 做 HTTPS 前置：下游用**另一把前置 key**（`mfk_…`），Caddy 校验后换成内部 key 再转发，内部 key 不出机器。只开放 `/v1/*` 和 `/healthz`（都要前置 key），`/v1/media/*` 免 key（文件名随机，下游要能直接下载），`/admin`、`/` 等一律 404。两把 key 都只在 root 0600 的 env 文件里，Caddy 配置文件里不写密钥。
- 管理页走 SSH 隧道：`ssh -L 18610:127.0.0.1:18610 root@<vps>`，打开 `http://127.0.0.1:18610/admin`，**在页面的 API Key 输入框里粘贴**（页面存在本机 localStorage），不要在地址栏带 `?key=`。

## 7. usage 永远是 0 → 计费要另找计量（usage is always 0 → meter elsewhere）

**现象**：所有 `/v1` 响应的 `usage` 都是 0。接到自己的网关后，按 token 计费的结算永远是 0。

**原因**：muse.ai 网页不返回 token 数，上游在 `app.py` 里把 usage 硬编码成 0。

**处理**：真正会动的是账号的**周额度百分比**（实测：两次对话 + 生图 + 一段 10 秒文生视频，Free 账号周额度 0% → 2%）。[`meter/muse_meter.py`](../meter/muse_meter.py) 把额度百分比、任务、按路由的请求数记进 SQLite，`report` 算出「每 1% 周额度大约做了多少次请求」，拿它定价或设上限。另外 [`upstream-prs/02-estimated-usage`](../upstream-prs/02-estimated-usage.md) 是给上游的草稿：可选的按字符估算 usage（默认关闭）。

---

## 另外两条

### 不用一键在线升级，锁 commit、读 diff（Pin a commit, don't one-click upgrade）

管理页有「一键升级」：拉作者仓库最新代码、覆盖本地文件并重启。上游更新很勤（v1.6.x 几天一版），一键升级等于把服务器代码的决定权交给别人，而且升级过程中正在跑的任务会被拒或中断。

本工具包：默认锁在 v1.6.12（`f7f5be5`），代码目录归 root、服务用户写不了；env 里设 `MUSE2API_MANAGED_DEPLOYMENT=1`，管理页的升级接口直接返回 409（隔离测试里读回过）。升级方式：

```bash
cd /opt/muse2api/app && sudo git fetch origin
sudo git diff --stat HEAD <新commit>        # 先读改了什么
sudo bash /path/to/muse2api-vps-kit/install.sh --commit <新commit>
```

### 同机跑两个实例要用不同的 CDP 端口（Two instances need different CDP ports）

muse2api 启动浏览器前会先看 `MUSE2API_CDP_PORT`（默认 19210）上有没有现成的 Chrome，有就直接接管。同一台机器再起一个实例（比如测试实例）而不改这个端口，第二个实例会悄悄驱动第一个实例的 Chrome。`install.sh` 在端口被别的用户占用时直接报错，`--port` 和 `--cdp-port` 都要换。
