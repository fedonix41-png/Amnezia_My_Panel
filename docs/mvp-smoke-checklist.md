# MVP Smoke Checklist

Manual validation checklist for a release-ready MVP deploy. Run end-to-end on
a clean host before tagging. Each item maps to a security/correctness contract
that automated tests do not cover (SSH, Docker, browser flows, tunnels).

> Required env up front: copy `.env.example` → `.env` and fill in
> `SECRET_KEY` (≥32 bytes) and `MASTER_KEY`.
>
> ```bash
> python -c "import secrets; print(secrets.token_urlsafe(48))"            # SECRET_KEY
> python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"  # MASTER_KEY
> ```

---

## 1. Local bring-up

- [ ] `cp .env.example .env` and populate `SECRET_KEY` + `MASTER_KEY` as above.
- [ ] `docker compose up -d` starts without `Refusing to start` in logs.

## 2. Container health

- [ ] `docker ps` shows `amnezia_panel` becoming `(healthy)` within ~40s
      (`start_period`).
- [ ] `docker compose logs` contains no `Refusing to start` / traceback.

## 3. Login

- [ ] Browser → `http://localhost:${APP_PORT}` → `/login` renders.
- [ ] `admin` / `admin` logs in successfully.
- [ ] Password change is enforced / accepted; new password works on re-login.

## 4. Server add

- [ ] Add a test server (mock-SSH or a real Ubuntu host) via the UI.
- [ ] On a second edit/save of the same server, the `host_fingerprint` is
      **not** re-requested (SSH pinning persists across writes).

## 5. Protocol install

- [ ] Install WireGuard or AWG on a real server from the UI.
- [ ] Create a client → download its config → connect from a client device →
      traffic flows.

## 6. Connection lifecycle

- [ ] Create a `user_connection` for a user.
- [ ] Delete it → verify the corresponding client is removed on the server
      and traffic accounting is cleaned up.

## 7. Share link

- [ ] `/settings` → enable share for a user → open the share URL.
- [ ] Enter the share password → the VPN config is retrievable.

## 8. Encryption at rest

- [ ] All secrets in `data.json` are Fernet-encrypted (prefixed `v1:`). Run:
      ```bash
      sudo docker exec amnezia_panel grep -E '"(password|private_key)":\s*"[^v]' /app/data/data.json
      ```
      → **0 matches** (any hit is a plaintext-secret leak).

## 9. Session stability across restart

- [ ] `docker compose restart`.
- [ ] Browser stays logged in (no re-login prompt) — proves `SECRET_KEY` is
      stable across restarts.

## 10. Rate limiting

- [ ] 7 rapid `POST /api/auth/login` attempts with a wrong password → the
      6th/7th return `429` (limit `5/minute`, `LOGIN_RATE_LIMIT`).

## 11. CSRF

- [ ] Without an `X-CSRF-Token` header, an unsafe method is rejected:
      ```bash
      curl -X POST http://localhost:${APP_PORT}/api/servers/add \
        -H 'Content-Type: application/json' \
        -d '{"host":"x","username":"y"}'
      ```
      → `403 CSRF token missing or invalid`.

## 12. Backup

- [ ] Either wait `BACKUP_INTERVAL_HOURS`, or set `BACKUP_INTERVAL_HOURS=1`
      for the test, then:
      ```bash
      docker exec amnezia_panel ls -la /app/data/backups/
      ```
      → a fresh encrypted backup file is present.

## 13. Public tunnel

- [ ] `/settings` → start a Cloudflare Quick Tunnel (or ngrok) → obtain a
      `https://*.trycloudflare.com` URL.
- [ ] Add the tunnel domain to `TRUSTED_HOSTS` (otherwise
      `TrustedHostMiddleware` returns `400`).
- [ ] Repeat login + server + protocol flows over the public URL.

## 14. Brute-force logging

- [ ] Repeated wrong-password attempts are logged.
- [ ] Rate-limit caps bursts at ≤5/minute per source IP.
