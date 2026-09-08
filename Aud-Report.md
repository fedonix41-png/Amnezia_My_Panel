Comprehensive Code Audit Report
1. CRITICAL: Event Loop Blocking from Synchronous SSH Operations
Severity: Critical — affects all endpoints involving SSH operations

Problem: Across almost all routers and Telegram bot handlers, SSHManager.connect() and manager methods are called synchronously inside async endpoints. The paramiko SSH connect handshake (TCP + key exchange + auth) is a blocking network call. Since there's no await asyncio.to_thread(ssh.connect), this blocks the entire FastAPI event loop for the duration of each SSH connection (potentially 5-15+ seconds with timeouts).

Affected files and call sites:

File	Lines	Pattern
routers/servers.py	L59, L118, L231-237, L257-336, L384-498	ssh.connect() not wrapped in asyncio.to_thread
routers/protocols.py	L95, L211, L229-235, L269-276, L316-326, L344-346, L383-401, L432-448, L459-482, L497-528, L541-568	ssh.connect() and ssh.disconnect() synchronously
routers/connections.py	L91, L157-175, L215-219, L251-265, L302-306, L331-335	ssh.connect() not wrapped
routers/settings.py	L297-301	ssh.connect() not wrapped in api_get_server_clients
routers/users.py	L172-187	ssh.connect() not wrapped in api_add_user
telegram_bot.py	L1853-1871 (_scrape_server_traffic), L413-450 (_get_ssh_and_manager used in sync wrappers), L1363-1390, L1432-1444, L1454-1471	SSH operations inside _get_cfg closures — some wrapped in asyncio.to_thread but many not
app.py	L1850-1871 (_scrape_server_traffic) — called via asyncio.to_thread so OK, but function itself is sync and does blocking SSH	Correctly wrapped but still a blocking function
Only correct usage: routers/users.py L314-336 (api_add_user_connection) correctly uses asyncio.to_thread for ssh.connect and ssh.disconnect, and asyncio.to_thread for manager.add_client.

Impact: In a production deployment with concurrent users, each SSH-dependent endpoint blocks all other requests. A slow SSH connection or timeout effectively DoS-es the panel.

Recommendation: All ssh.connect() calls in async endpoints must be wrapped in asyncio.to_thread. Similarly, ssh.disconnect() and long-running manager methods should be wrapped. The pattern used in routers/users.py:314-336 and telegram_bot.py (wrapping sync SSH operations in asyncio.to_thread) should be applied consistently.

2. HIGH: SSH Connection Leaks on Exception
Severity: High — connection leak leading to resource exhaustion

Problem: In multiple router endpoints, ssh.connect() is called, but ssh.disconnect() is placed in the normal flow path, NOT in a finally block. If any exception occurs between connect() and disconnect(), the SSH connection is never closed.

Affected locations:

routers/servers.py L231-237 (api_reboot_server): ssh.connect() then ssh.run_sudo_command(), if the command raises, ssh.disconnect() is in a separate try/except that may not execute
routers/servers.py L257-336 (api_clear_server): ssh.connect() → ssh.run_sudo_script() → ssh.disconnect(), no finally
routers/servers.py L384-498 (api_check_server): ssh.connect() at L385, ssh.disconnect() at L497, but many code paths in between can throw
routers/servers.py L299-336 (api_server_stats): ssh.connect() then commands, ssh.disconnect() at L336 with no try/finally
routers/protocols.py L94-190 (api_install_protocol): ssh.connect() at L95, ssh.disconnect() at L190, but if manager.install_protocol() throws, disconnect is skipped
routers/protocols.py L209-213, L229-235, L269-276: similar disconnect-in-normal-path patterns
routers/connections.py L91-94, L157-175, L215-219, L251-265, L302-306, L331-335
routers/settings.py L297-301
Contrast with correct pattern: routers/protocols.py L304-418 (api_protocol_backups_list, api_protocol_backup_create) correctly use ssh = None + try/finally with ssh.disconnect().

Recommendation: Adopt a consistent try/finally pattern for all SSH operations, or better yet, use the SSHManager.__enter__/__exit__ context manager (defined at ssh_manager.py:280-284) which guarantees disconnect.

3. HIGH: PostHog Config Logic Inverted
Severity: High — analytics misconfiguration / false startup errors

Problem: In config.py:121-131, when AWP_DEV=1 (dev mode), PostHog variables are required. In production, they're optional. This is backwards — dev mode should not require PostHog (it should be optional for local testing), while production should require it if analytics are to work.

# config.py:121
if AWP_DEV:
    for _posthog_variable, _posthog_value in (
        ("POSTHOG_PROJECT_TOKEN", POSTHOG_PROJECT_TOKEN),
        ("POSTHOG_HOST", POSTHOG_HOST),
    ):
        if not _posthog_value:
            raise RuntimeError(...)
Impact: In dev mode, the app crashes on startup if PostHog env vars aren't configured. This prevents local development without unnecessary extra configuration. In production, missing PostHog config is silently ignored.

Recommendation: The dev-mode requirement should be removed — PostHog should be optional in both modes, simply disabled if not configured.

4. HIGH: Inconsistent Password Hash Formats and Iterations
Severity: High — security and consistency

Problem: Two different password hashing implementations exist with different iteration counts and formats:

app.py:1178-1182 (hash_password): PBKDF2 600k iterations, format iterations$salt$hash
database/repositories.py:18-21 (hash_password): PBKDF2 100k iterations, format salt$hash
The verify_password function (app.py:1185-1201) handles both formats, so they are interoperable. However:

UserRepository.ensure_default_admin (repositories.py:86-102) uses the 100k-iteration version to create the default admin account (admin/admin), while routers/users.py:139 calls app_module.hash_password (600k iterations) for regular user creation. This means the default admin has a weaker password hash.
telegram_bot.py:944 calls app_module.hash_password(username) — this uses the 600k iteration version.
sharing.py:41 calls app_module.hash_password(req.password) — 600k version.
The inconsistency in iteration counts is a security concern for the default admin account and represents code duplication that should be consolidated.

Recommendation: Consolidate to a single hash_password/verify_password implementation with consistent iteration counts. The default admin should use the same hashing path.

5. MEDIUM: _resolve_api_token is Dead Code
Severity: Medium — dead code referencing outdated data structures

Problem: app.py:1137-1158 defines _resolve_api_token(data: dict, raw_token: str) which expects a data dict with an 'api_tokens' key and a 'users' list. This function reads from an in-memory dictionary structure (legacy data.json pattern), but the actual bearer token authentication in _check_admin (app.py:1482-1503) reads directly from the database ApiToken table. _resolve_api_token is never called anywhere in the codebase.

Impact: Confusing dead code that suggests an outdated authentication approach. Could mislead future maintainers into thinking bearer auth reads from an in-memory dict.

Recommendation: Remove _resolve_api_token and _touch_api_token if also unused. Let me verify:

_resolve_api_token is tested but never actually called at runtime — the real auth path in _check_admin uses the database ApiToken model directly. _touch_api_token is tested in test_helpers.py. These are dead code in the production path but kept for compatibility testing.

6. MEDIUM: Models Used as Dicts in Background Tasks (_scrape_server_traffic)
Severity: Medium — unnecessary ORM-to-dict conversion causing double decryption

Problem: In app.py:1937-1943 (periodic_background_tasks), the ORM Server object is converted to a plain dict with password and private_key fields, then passed to _scrape_server_traffic which calls get_ssh(server) in dict mode (app.py:271-301). The dict mode calls decrypt_secret() on password/key fields. This means secrets are decrypted in the background task context, outside of any per-request security boundary.

Impact: Not a direct vulnerability, but the dict conversion is unnecessary — get_ssh already handles ORM model objects directly. The server object could be passed directly to _scrape_server_traffic instead of being converted to a dict.

7. MEDIUM: Missing asyncio.to_thread for ensure_docker_installed
Severity: Medium — event loop blocking during protocol installation

Problem: In routers/protocols.py:96, ensure_docker_installed(ssh) is called synchronously (not wrapped in asyncio.to_thread). This function runs multiple SSH commands (potentially including a full Docker installation via ssh.run_sudo_script with a 300-second timeout), all of which block the event loop.

Impact: During protocol installation, the entire event loop is blocked for up to 300 seconds, preventing any other request from being processed.

8. LOW: Unused now_local Variable in Background Tasks
Severity: Low — dead code

Problem: In app.py:1950, now_local = datetime.now() is computed but never used anywhere in the periodic_background_tasks function. The traffic reset logic uses now_utc exclusively.

9. LOW: Inconsistent Model Import Pattern in Telegram Bot
Severity: Low — minor inconsistency

Problem: In telegram_bot.py:425-450, _get_ssh_and_manager imports all managers directly and replicates the get_protocol_manager logic from app.py. This duplicates the manager dispatch logic. Any new protocol added would require changes in two places.

Also: The bot's _manager_call (telegram_bot.py:453-458) uses a try/except TypeError approach to handle the WireGuardManager signature difference, while app.py:1102-1107 uses an isinstance check. Both approaches work, but the duplication creates maintenance risk.

10. MEDIUM: Connection vs UserConnection Model Alias Confusion
Severity: Medium — potential for inconsistent model usage

Problem: database/models.py:134 defines Connection = UserConnection as an alias. Throughout the codebase, both names are used interchangeably:

routers/connections.py uses models.Connection (L105, L182, L221, L282)
app.py uses models.UserConnection in some places (L1954, perform_mass_operations)
database/repositories.py uses UserConnection (L189-190)
While this works due to the alias, it's confusing and any future refactoring could miss one reference. The telegram_bot.py _build_data_dict creates its own dict representation of connections (L47-58) rather than using the model, which could lead to field name mismatches.

11. MEDIUM: servers dict vs list confusion in Telegram bot
Severity: Medium — potential KeyError

Problem: In telegram_bot.py, _build_data_dict creates data["servers"] as a dict keyed by server ID (L44: data["servers"][s.id] = srv), but several functions treat servers as a list:

L1040: data.get("servers", []) — treats as list but it's a dict
L1053: servers = data.get("servers", []) — same issue
L1107: servers = data.get("servers", []) with server_id >= len(servers) check — len() on a dict works (counts keys), but indexing servers[server_id] works for dict but not list semantics
L1306-1349: The bot uses data["servers"][server_id] which works because it's a dict
This is actually consistent (dict works for the usage), but the type annotations and variable names (servers plural, used like a list in some places) are misleading.

Here's a summary of all findings with priorities:

Priority	Issue	Severity	Impact
1	SSH calls blocking event loop	Critical	All endpoints become unresponsive under concurrent load
2	SSH connection leaks on exceptions	High	Connection exhaustion on errors
3	PostHog config requirement inverted	High	Dev mode crashes without PostHog; prod ignores missing config
4	Duplicate/inconsistent password hashing	High	Default admin has weaker hash; code duplication
5	Dead code _resolve_api_token	Medium	Misleading, suggests outdated auth pattern
6	Unnecessary ORM-to-dict conversion in bg tasks	Medium	Extra decryption, redundant conversion
7	ensure_docker_installed not async-wrapped	Medium	Event loop blocked during Docker install
8	Unused now_local variable	Low	Dead code
9	Duplicated manager dispatch in Telegram bot	Low	Maintenance risk
10	Connection/UserConnection alias confusion	Medium	Maintenance risk
11	servers dict/list type confusion in bot	Medium	Misleading code, potential bugs
Most critical issues to fix: The synchronous SSH operations blocking the event loop (#1), connection leaks (#2), and PostHog inversion (#3).