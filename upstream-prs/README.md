# Upstream PR drafts

Drafts for [czg86389-hub/muse2api](https://github.com/czg86389-hub/muse2api). **Submitted 2026-11-24**, rebased onto upstream `main` `7034398` (v1.6.14): [#66](https://github.com/czg86389-hub/muse2api/pull/66) (docs) and [#67](https://github.com/czg86389-hub/muse2api/pull/67) (estimated usage). The text below is the original draft against `f7f5be5`. Each draft has a title, a body to paste, and a `.patch` made with `git diff` against the pinned commit `f7f5be5` (v1.6.12); both apply cleanly there (`git apply --check`).

| Draft | What it proposes | Code change | Tests |
|---|---|---|---|
| [01-docs-small-vps](01-docs-small-vps.md) | README section: loopback + front-key reverse proxy (Caddy example), open `/admin` without `?key=`, data URLs for self-hosted reference images, 202 means accepted, concurrency vs accounts and memory, `MUSE2API_MANAGED_DEPLOYMENT=1` for pinned installs, `usage` is always 0 | README only | upstream suite unchanged (35 pass / 4 fail, same 4 as untouched `f7f5be5`) |
| [02-estimated-usage](02-estimated-usage.md) | opt-in `MUSE2API_ESTIMATE_USAGE=1`: chat / responses report character-based token estimates and an `X-Muse2api-Usage: estimated` header; default stays all-zero | `config.py`, `app.py`, `.env.example`, README env table, new `tests/test_estimated_usage.py` | new test 7/7 OK; full suite 36 pass / 4 fail (same 4 as untouched `f7f5be5`) |

The 4 failures exist on the untouched commit too: three Chromium UI tests that need a Linux Chromium (`os.killpg` is refused in our macOS sandbox, one cannot start Chrome there), and `tests/test_extension_upgrade_notice.py`, which asserts the extension version `1.0.3` appears in `version.json` highlights, and v1.6.12's highlights do not mention it.

Not drafted on purpose: a SQLite task store (see [../docs/upgrade-feasibility.md](../docs/upgrade-feasibility.md); it is large enough to deserve an issue first) and the server-side login (upstream already has an open PR for code login, #42).
