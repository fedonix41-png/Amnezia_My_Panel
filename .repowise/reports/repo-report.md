# Repo report: Amnezia_My_Panel

main @ c83332d0 · indexed 2026-11-25 · 130 files · health 5.3/10 (Needs work)

Written from the index by a template: no model, $0.

Health is 5.3/10 (Needs work). Found: 5 refactor-first items, 9 exports safe to delete, 1 ownership item. Ownership comes first.

8 of the 20 lowest-health files had a bug fix in the last 6 months, 2.2x the 18% baseline. (measured)

## Do these first

1. Ownership: 6 of 6 components rest on one person. Pair a second reviewer on them.
2. Code health: Break up _dispatch (CCN 45, 122 lines). Complex method; 3 files import it; 4 changes in 90 days. (`telegram_bot.py:1558`)
3. Dead code: 9 unused exports, 214 lines, safe to delete: nothing imports, calls or loads them. Remove only the names listed. (`warp_service.py`)

## 1. Security

No live secrets in tracked files.

## 2. Code health: refactor first

8 of the 20 lowest-health files had a bug fix in the last 6 months, 2.2x the 18% baseline. Start with the Fix-first items, then the lowest-scoring files.

- Break up _dispatch (CCN 45, 122 lines). Complex method; 3 files import it; 4 changes in 90 days.
  `telegram_bot.py:1558` (_dispatch)
  verify: `pytest tests/test_telegram_bot_ui.py`
  inferred · medium confidence
- Start breaking up find_running_tunnel_pids (CCN 40, 64 lines): first lift lines 603-616. Nested complexity; 21 files import it; 5 changes in 90 days.
  `app.py:595` (find_running_tunnel_pids)
  verify: `pytest tests/test_api_tokens.py tests/test_awg_plugin_api.py tests/test_core_routes.py tests/test_helpers.py tests/test_share.py`
  inferred · medium confidence
- Health 1.0/10 over 447 lines, one of the lowest-scoring production files. Open its health page for the findings behind the score.
  `routers/servers.py`
  inferred · medium confidence
- Health 1.0/10 over 1,808 lines, one of the lowest-scoring production files. Open its health page for the findings behind the score.
  `app.py`
  inferred · medium confidence
- Health 1.0/10 over 4,188 lines, one of the lowest-scoring production files. Open its health page for the findings behind the score.
  `managers/awg_manager.py`
  inferred · medium confidence

## 3. Dead code

9 unused exports are safe to delete (1 file). 2 lower-confidence findings are in the app.

- 9 unused exports, 214 lines, safe to delete: nothing imports, calls or loads them. Remove only the names listed.
  `warp_service.py` (WarpStateStore, aggregate_state, classify_instance_state, exit_record, quota_exhausted, registers_prune, sanitize_warp_profile, warp_client_mtu, warp_ctx_for)
  inferred · high confidence

## 4. Ownership

Counted across 6 components; owners are counted, never named.

- 6 of 6 components rest on one person. Pair a second reviewer on them.
  measured · medium confidence

## Turn on more

- Keep the index in sync on commit: `repowise hook install`
- Add a test coverage report: `repowise coverage setup`
- Connect runtime evidence: profile a real run: `repowise evidence setup`

---

Report at c83332d0. Since then: `repowise report repo --since-last`
