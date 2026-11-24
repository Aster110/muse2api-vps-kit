# Draft PR 02: feat: opt-in estimated usage for chat (MUSE2API_ESTIMATE_USAGE=1)

**Patch**: [02-estimated-usage.patch](02-estimated-usage.patch) (`config.py`, `app.py`, `.env.example`, README env table, new `tests/test_estimated_usage.py`)

## Title

feat: 可选的对话用量估算 MUSE2API_ESTIMATE_USAGE / opt-in estimated usage for chat endpoints

## Body

**问题**：muse.ai 网页不返回 token 数，`/v1/chat/completions` 和 `/v1/responses` 的 `usage` 一直是 0。接到按 token 记账或限额的网关（one-api / new-api 一类，或自建网关）后，这些请求全部记成 0，限额也失效。

**改动**（默认关闭，行为不变）：

- 新环境变量 `MUSE2API_ESTIMATE_USAGE`（默认 `0`）。设为 `1` 时，对话类接口按字符粗估：CJK 字符每字 1 个 token，其余每 4 个字符 1 个（向上取整）。prompt 用的是实际发给 muse.ai 的拼接后提示词（`build_chat_prompt` 的结果）。
- 覆盖：`/v1/chat/completions` 非流式 `usage`；流式在 `stream_options.include_usage=true` 时的最后一个 usage 分片；`/v1/responses` 非流式和 `response.completed` 事件里的 `input_tokens` / `output_tokens`。
- 开启时响应加头 `X-Muse2api-Usage: estimated`，让下游知道这是估算值，不是上游计量。
- 不动生图 / 视频接口（它们按件计费更合适）。

**为什么用字符估算而不是 tokenizer**：不引入新依赖，Muse 的分词器也不公开；估算只用来防止「全记 0」，所以明确标成 estimated。

English: `usage` is hard-coded to zeros because the Muse web app exposes no token counts, which makes token-based gateways bill and cap these requests at 0. This adds an opt-in, dependency-free character-based estimate (CJK char = 1 token, otherwise 4 chars = 1 token) for chat and responses, streaming and non-streaming, plus an `X-Muse2api-Usage: estimated` header. Default off; with the variable unset every response is byte-for-byte the same as before.

## Test result

New `tests/test_estimated_usage.py` follows the repo's offline style (temp `MUSE2API_HOME`, `safe_chat_stream` patched, no account, no browser). It checks: default zeros and no header; estimates for non-stream, stream usage chunk and responses; one request through the real ASGI app (auth, `Response` injection, serialization).

```
$ python tests/test_estimated_usage.py
test_default_keeps_zero_usage_and_no_header ... ok
test_estimate_tokens ... ok
test_http_round_trip_sets_header ... ok
test_opt_in_estimates_non_stream ... ok
test_opt_in_estimates_stream_usage_chunk ... ok
test_responses_api ... ok
test_stream_default_usage_chunk_is_zero ... ok
Ran 7 tests in 0.163s
OK
```

Full suite on macOS (Python 3.14.7): **36 pass / 4 fail**; untouched `f7f5be5` is 35 pass / the same 4 fail (see [README.md](README.md)). No existing test changed result.
