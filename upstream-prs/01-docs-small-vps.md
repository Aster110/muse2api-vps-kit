# Draft PR 01: docs: small-VPS and front-key deployment notes

**Patch**: [01-docs-small-vps.patch](01-docs-small-vps.patch) (README.md only, +42 lines, inserted at the end of 「方式二：裸机 / VPS 部署」)

## Title

docs: 小内存 VPS 与前置网关部署要点 / small-VPS and front-key deployment notes

## Body

在一台 2 vCPU / 2 GB 的 VPS 上用 systemd 裸机部署 v1.6.12 时，下面几件事 README 里要么没写、要么写在不容易找到的地方，这个 PR 把它们集中成一小节：

- **只听 127.0.0.1，外面套一层独立的前置 key**：管理接口和 `/v1` 共用 `MUSE2API_KEY`，发给下游等于交出管理权。附一个 Caddy 示例：校验前置 key、换成内部 key 转发，只放 `/v1/*`，`/v1/media/*` 免 key，其余 404；两把 key 从环境变量读，配置文件里没有密钥。管理页走 SSH 隧道。
- **管理页不要用 `?key=` 打开**：直接开 `/admin` 在输入框里粘贴（页面本来就支持，存在 localStorage）。带 key 的 URL 会进 uvicorn 访问日志和浏览器历史，我们的 journald 里确实留下过一条。
- **自托管的参考图用 data URL**：把本服务生成的 `http://127.0.0.1:18610/v1/media/…` 直接当 `image_url` 会被 SSRF 防护拒绝（这是对的），README 里只在环境变量表提到 `MUSE2API_REF_IMAGE_PRIVATE_FETCH`，没说常见的触发场景。
- **`/v1/images/tasks` 的 202 是受理**：指回已有的 v1.5.2 一节；顺带说明 `/v1/videos` 回 200 + queued。
- **并发与内存**：`config.py` 在未设置时默认 8，而 README 表格和 `.env.example` 写 2；每次对话或生成独占一个账号，单账号时并发 >1 只多占内存。附 2 GB 机器的实测（空闲约 600 MB、一轮峰值约 870 MB）和 `OOMScoreAdjust` / `MemoryHigh` / swap 建议。
- **锁定版本时设 `MUSE2API_MANAGED_DEPLOYMENT=1`**：一键升级返回 409，升级改为人工 checkout + 读 diff。
- **`usage` 恒为 0**：计量要在网关侧做，或读额度接口（约 8 秒、会占生成锁）。

English: a short README section for people running muse2api on a small VPS behind a reverse proxy. No code changes.

Notes for reviewers: the README table says the `MUSE2API_CONCURRENCY` default is 2 while `config.py` defaults to 8 when the variable is unset (commit 1795dab). This PR documents the actual behaviour instead of changing it; happy to change the table or the code default instead if you prefer.

## Test result

README-only. Upstream suite on macOS (Python 3.14.7, venv from `requirements.txt`): **35 pass / 4 fail, identical to untouched `f7f5be5`** (the 4 are environment or pre-existing failures, see [README.md](README.md)). The Caddy snippet in the patch was checked with `caddy validate` (Caddy 2.11.9); the same directives run in production on Caddy 2.6.2.
