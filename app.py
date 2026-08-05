import os
import sys
import json
import logging
import base64
import hashlib
import secrets
import uuid
import asyncio
import platform
import re
import shutil
import shlex
import tempfile
import subprocess
import tarfile
import threading
import time
import urllib.request
import zipfile
import signal
from datetime import datetime
from contextlib import asynccontextmanager
import io
from fastapi.responses import JSONResponse, RedirectResponse, HTMLResponse, StreamingResponse, FileResponse
from starlette.background import BackgroundTask
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi import FastAPI, Request, Query, UploadFile, File
from starlette.middleware.sessions import SessionMiddleware
from pydantic import BaseModel
from typing import Optional, List, Dict
import uvicorn
import httpx

# Centralized config. Importing this triggers load_dotenv() and fail-fast
# validation of SECRET_KEY / MASTER_KEY — must run before SessionMiddleware.
import config

# Rate limiter (module-level so route decorators can reference it).
from slowapi import Limiter, _rate_limit_exceeded_handler

from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware
from middleware import CsrfCookieMiddleware, SecurityHeadersMiddleware

limiter = Limiter(key_func=config._rate_limit_key, default_limits=[])

try:
    from multicolorcaptcha import CaptchaGenerator
except ImportError:
    CaptchaGenerator = None

from managers.ssh_manager import SSHManager
from managers.awg_manager import AWGManager
from managers.xray_manager import XrayManager
from managers.wireguard_manager import WireGuardManager
from managers.backup_manager import BackupManager
from managers.secrets import encrypt_secret, decrypt_secret, SecretError
from managers.amnezia_config import generate_vpn_link as _amnezia_generate_vpn_link, generate_qr_content as _amnezia_generate_qr_content
import telegram_bot as tg_bot

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Ordered list of OpenAPI tag groups — the order here drives the section order in /docs and /redoc.
OPENAPI_TAGS = [
    {"name": "System Templates", "description": "HTML pages served to browsers. These return Jinja-rendered templates rather than a JSON contract — they are not part of the public API and are listed here only for completeness."},
    {"name": "Authentication", "description": "Login, captcha, and session lifecycle."},
    {"name": "Servers", "description": "Server inventory, lifecycle and host-level operations (add, edit, delete, ping, reorder, reboot, clear, stats, status check)."},
    {"name": "Protocols", "description": "Install, uninstall, container start/stop and raw config editing for the protocols/services on a server (AWG, Xray, WireGuard, Telemt, AmneziaDNS, AdGuard Home, SOCKS5)."},
    {"name": "Connections", "description": "Per-protocol VPN client connections on a server (CRUD plus enable/disable and config retrieval)."},
    {"name": "Users", "description": "Panel user accounts and the connections assigned to them."},
    {"name": "Self-service", "description": "Endpoints called by a regular user for their own data (the /my surface)."},
    {"name": "Sharing", "description": "Public, token-protected configuration sharing for end users — no panel session required."},
    {"name": "Settings", "description": "Panel-wide settings, Telegram bot, Remnawave sync, JSON backup/restore."},
    {"name": "API Tokens", "description": "Bearer tokens for external integrations. Send the token in `Authorization: Bearer <token>`; tokens have admin-equivalent rights and are tied to the admin user that created them."},
]

app = FastAPI(
    title="Amnezia Web Panel",
    openapi_tags=OPENAPI_TAGS,
    # FastAPI's stock /redoc loads the JS bundle from `redoc@next` on jsdelivr —
    # an unstable rolling tag that breaks unpredictably. Disable the default and
    # serve our own /redoc just below, pinned to the stable v2 bundle.
    redoc_url=None,
)


@app.get("/redoc", include_in_schema=False)
async def custom_redoc():
    """Self-curated ReDoc page. Differs from FastAPI's default in two ways:
    pinned bundle (`redoc@2` instead of `@next`) and Google Fonts disabled
    (the Montserrat/Roboto stylesheet is blocked on a lot of networks and made
    the page hang for some users)."""
    from fastapi.openapi.docs import get_redoc_html
    return get_redoc_html(
        openapi_url=app.openapi_url or "/openapi.json",
        title=f"{app.title} — ReDoc",
        redoc_js_url="https://cdn.jsdelivr.net/npm/redoc@2/bundles/redoc.standalone.js",
        with_google_fonts=False,
    )
app.add_middleware(
    SessionMiddleware,
    secret_key=config.SECRET_KEY,
    https_only=config.SESSION_COOKIE_SECURE,
    same_site=config.SESSION_COOKIE_SAMESITE,
)

# Ensure 'app' module alias exists in sys.modules when app.py is executed as __main__
import sys
sys.modules['app'] = sys.modules[__name__]














# Middleware are added innermost-first; the last add_middleware is outermost.
# Outer → inner order: TrustedHost → SecurityHeaders → SlowAPI → CSRF → Session.
app.add_middleware(CsrfCookieMiddleware)
app.add_middleware(SlowAPIMiddleware)
app.add_middleware(SecurityHeadersMiddleware)

# RateLimitExceeded → JSON 429 handler.
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# TrustedHost: reject foreign Host headers. Skipped in dev so LAN/IP access
# during local testing is not blocked.
if not config.AWP_DEV and config.TRUSTED_HOSTS:
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=config.TRUSTED_HOSTS)

# Mount static files & templates
app.mount("/static", StaticFiles(directory=os.path.join(os.path.dirname(__file__), "static")), name="static")
templates = Jinja2Templates(directory=os.path.join(os.path.dirname(__file__), "templates"))

if getattr(sys, 'frozen', False):
    application_path = os.path.dirname(sys.executable)
else:
    application_path = os.path.dirname(__file__)

DATA_FILE = os.environ.get('DATA_FILE') or os.path.join(application_path, 'data.json')
# Path used before DATA_FILE honored its env var. Kept for the one-time
# upgrade rescue in _maybe_migrate_legacy_data_file() — operators who got
# persistence working under read_only rootfs left their data here.
_LEGACY_DATA_FILE = os.path.join(application_path, 'data.json')
CURRENT_VERSION = "v1.5.0"
BIN_DIR = os.environ.get('TUNNEL_BIN_DIR', os.path.join(application_path, 'bin'))
TUNNEL_STATE_FILE = os.environ.get('TUNNEL_STATE_FILE', os.path.join(application_path, 'tunnels_state.json'))


def _maybe_migrate_legacy_data_file():
    """One-time rescue of data.json from the legacy application-directory path."""
    if DATA_FILE == _LEGACY_DATA_FILE:
        return
    if os.path.exists(DATA_FILE):
        return
    if not os.path.exists(_LEGACY_DATA_FILE):
        return
    try:
        if os.path.getsize(_LEGACY_DATA_FILE) == 0:
            return
        os.makedirs(os.path.dirname(DATA_FILE) or '.', exist_ok=True)
        shutil.copy2(_LEGACY_DATA_FILE, DATA_FILE)
        logger.warning("Migrated existing data.json from legacy path %s to %s.", _LEGACY_DATA_FILE, DATA_FILE)
    except Exception as e:
        logger.error("Failed to migrate legacy data.json (%s -> %s): %s", _LEGACY_DATA_FILE, DATA_FILE, e)



class TunnelRuntime:
    def __init__(self):
        self.process = None
        self.pid = None
        self.public_url = ''
        self.last_error = ''
        self.started_at = None
        self.output = []


TUNNEL_RUNTIMES = {
    'cloudflare': TunnelRuntime(),
    'ngrok': TunnelRuntime(),
}
TUNNEL_LOCK = threading.Lock()
TUNNEL_URL_RE = re.compile(r'https://[^\s"\']+')
WARP_CLI_COMMAND = 'warp-cli.exe' if os.name == 'nt' else 'warp-cli'



# ======================== Translations ========================
TRANSLATIONS = {}

def load_translations():
    global TRANSLATIONS
    trans_dir = os.path.join(os.path.dirname(__file__), 'translations')
    if os.path.exists(trans_dir):
        for f in os.listdir(trans_dir):
            if f.endswith('.json'):
                lang = f.split('.')[0]
                try:
                    with open(os.path.join(trans_dir, f), 'r', encoding='utf-8') as tf:
                        TRANSLATIONS[lang] = json.load(tf)
                except Exception as e:
                    logger.error(f"Error loading translation {f}: {e}")
    logger.info(f"Loaded translations: {list(TRANSLATIONS.keys())}")

def _t(text_id, lang='en'):
    lang_batch = TRANSLATIONS.get(lang, TRANSLATIONS.get('en', {}))
    return lang_batch.get(text_id, text_id)

load_translations()


# ======================== Helpers ========================
from database.session import get_db, get_db_context
from database import models
from database.crud import get_setting, set_setting
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select


# legacy data functions removed


def get_ssh(server, db: Optional[AsyncSession] = None):
    if isinstance(server, dict):
        # Legacy dict mode (mostly for tests or transition)
        def on_fp(fp):
            server['host_fingerprint'] = fp
        return SSHManager(
            host=server['host'],
            port=server.get('ssh_port', 22),
            username=server['username'],
            password=decrypt_secret(server.get('password')),
            private_key=decrypt_secret(server.get('private_key')),
            host_fingerprint=server.get('host_fingerprint'),
            on_fingerprint=on_fp,
        )
    else:
        # ORM model mode
        def on_fp(fp):
            server.host_fingerprint = fp
            # We don't commit here because callers typically commit after mutating,
            # or it's just a read-only endpoint that updates the DB state in passing.
            if db:
                db.add(server)
        return SSHManager(
            host=server.host,
            port=server.ssh_port or 22,
            username=server.username,
            password=decrypt_secret(server.password),
            private_key=decrypt_secret(server.private_key),
            host_fingerprint=server.host_fingerprint,
            on_fingerprint=on_fp,
        )


async def get_panel_local_url(db: AsyncSession, request: Optional[Request] = None):
    ssl_conf = await get_setting(db, 'ssl', {})
    scheme = 'https' if ssl_conf.get('enabled') else 'http'
    host = '127.0.0.1'
    port = ssl_conf.get('panel_port', 5000) or 5000
    if request:
        scheme = request.url.scheme or scheme
        host = request.url.hostname or host
        port = request.url.port or port
    return f"{scheme}://{host}:{port}"


async def get_panel_tunnel_target_url(db: AsyncSession):
    ssl_conf = await get_setting(db, 'ssl', {})
    scheme = 'https' if ssl_conf.get('enabled') else 'http'
    port = ssl_conf.get('panel_port', 5000) or 5000
    return f"{scheme}://127.0.0.1:{port}"


def get_warp_cli_binary():
    found = shutil.which(WARP_CLI_COMMAND)
    if found:
        return found
    if os.name == 'nt':
        for base in (os.environ.get('ProgramFiles'), os.environ.get('ProgramFiles(x86)')):
            if not base:
                continue
            candidate = os.path.join(base, 'Cloudflare', 'Cloudflare WARP', 'warp-cli.exe')
            if os.path.exists(candidate):
                return candidate
    return None


def is_warp_cli_installed():
    return bool(get_warp_cli_binary())


def _warp_install_hint():
    system = platform.system().lower()
    if system == 'windows':
        return 'Install Cloudflare WARP from https://1.1.1.1/ or winget install Cloudflare.Warp, then restart this panel.'
    if system == 'darwin':
        return 'Install Cloudflare WARP from https://1.1.1.1/ or brew install --cask cloudflare-warp, then restart this panel.'
    return 'Install the official cloudflare-warp package for your Linux distribution, start warp-svc, then restart this panel.'


def run_warp_cli(*args, timeout: int = 12):
    binary = get_warp_cli_binary()
    if not binary:
        raise RuntimeError(_warp_install_hint())
    creationflags = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
    command = [binary]
    if '--accept-tos' not in args:
        command.append('--accept-tos')
    command.extend(args)
    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding='utf-8',
        errors='replace',
        timeout=timeout,
        creationflags=creationflags,
    )
    output = '\n'.join(part.strip() for part in (result.stdout, result.stderr) if part and part.strip())
    if result.returncode != 0:
        raise RuntimeError(output or f'warp-cli exited with code {result.returncode}')
    return output


def _parse_warp_status(output: str):
    lowered = (output or '').lower()
    if 'registration missing' in lowered or 'not registered' in lowered or 'not enrolled' in lowered:
        return 'not_registered'
    if 'disconnect' in lowered or 'disabled' in lowered:
        return 'disconnected'
    if 'connecting' in lowered:
        return 'connecting'
    if 'connected' in lowered:
        return 'connected'
    return 'unknown'


def get_warp_status():
    installed = is_warp_cli_installed()
    status = {
        'installed': installed,
        'running': False,
        'connected': False,
        'status': 'not_installed' if not installed else 'unknown',
        'raw': '',
        'last_error': '' if installed else _warp_install_hint(),
        'install_hint': _warp_install_hint(),
    }
    if not installed:
        return status
    try:
        output = run_warp_cli('status')
        parsed = _parse_warp_status(output)
        status.update({
            'running': parsed in ('connected', 'connecting'),
            'connected': parsed == 'connected',
            'status': parsed,
            'raw': output,
            'last_error': '',
        })
    except Exception as e:
        status['last_error'] = str(e)
        lowered = str(e).lower()
        if 'registration missing' in lowered or 'not registered' in lowered or 'not enrolled' in lowered:
            status['status'] = 'not_registered'
        elif 'service' in lowered or 'daemon' in lowered or 'warp-svc' in lowered:
            status['status'] = 'service_unavailable'
        else:
            status['status'] = 'error'
    return status


def enable_warp():
    current = get_warp_status()
    if not current.get('installed'):
        raise RuntimeError(current.get('install_hint') or _warp_install_hint())
    try:
        if current.get('status') == 'not_registered':
            run_warp_cli('registration', 'new', timeout=30)
        run_warp_cli('mode', 'warp', timeout=15)
        run_warp_cli('connect', timeout=30)
    except Exception as e:
        message = str(e)
        if 'registration' in message.lower() or 'not registered' in message.lower() or 'not enrolled' in message.lower():
            run_warp_cli('registration', 'new', timeout=30)
            run_warp_cli('mode', 'warp', timeout=15)
            run_warp_cli('connect', timeout=30)
        else:
            raise
    time.sleep(1)
    return get_warp_status()


def disable_warp():
    current = get_warp_status()
    if not current.get('installed'):
        raise RuntimeError(current.get('install_hint') or _warp_install_hint())
    run_warp_cli('disconnect', timeout=20)
    time.sleep(0.5)
    return get_warp_status()


def get_tunnel_command_name(provider: str):
    if provider == 'cloudflare':
        return 'cloudflared.exe' if os.name == 'nt' else 'cloudflared'
    if provider == 'ngrok':
        return 'ngrok.exe' if os.name == 'nt' else 'ngrok'
    raise ValueError('Unsupported tunnel provider')


def get_tunnel_binary_path(provider: str):
    return os.path.join(BIN_DIR, get_tunnel_command_name(provider))


def find_tunnel_binary(provider: str):
    bundled = get_tunnel_binary_path(provider)
    if os.path.exists(bundled):
        return bundled
    return shutil.which(get_tunnel_command_name(provider))


def is_tunnel_installed(provider: str):
    return bool(find_tunnel_binary(provider))


def load_tunnel_state():
    if not os.path.exists(TUNNEL_STATE_FILE):
        return {}
    try:
        with open(TUNNEL_STATE_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception as e:
        logger.warning(f"Failed to load tunnel state: {e}")
        return {}


def save_tunnel_state(state):
    with open(TUNNEL_STATE_FILE, 'w', encoding='utf-8') as f:
        json.dump(state, f, indent=2, ensure_ascii=False)


def update_tunnel_state(provider: str, **updates):
    state = load_tunnel_state()
    provider_state = state.get(provider, {})
    provider_state.update(updates)
    state[provider] = provider_state
    save_tunnel_state(state)


def clear_tunnel_state(provider: str):
    state = load_tunnel_state()
    if provider in state:
        state.pop(provider)
        save_tunnel_state(state)


def pid_is_running(pid):
    if not pid:
        return False
    try:
        os.kill(int(pid), 0)
        return True
    except OSError:
        return False
    except Exception:
        return False


def kill_pid(pid):
    if not pid:
        return
    if os.name == 'nt':
        subprocess.run(['taskkill', '/PID', str(pid), '/T', '/F'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if not pid_is_running(pid):
                return
            time.sleep(0.2)
    else:
        try:
            os.kill(int(pid), signal.SIGTERM)
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                if not pid_is_running(pid):
                    return
                time.sleep(0.2)
            if pid_is_running(pid):
                os.kill(int(pid), signal.SIGKILL)
        except ProcessLookupError:
            return


def wait_for_path_release(path, seconds: int = 5):
    if os.name != 'nt':
        return
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            if not os.path.exists(path):
                return
            with open(path, 'ab'):
                return
        except PermissionError:
            time.sleep(0.25)


def find_running_tunnel_pids_in_proc(provider: str, binary_path: str = ''):
    proc_dir = '/proc'
    if os.name == 'nt' or not os.path.isdir(proc_dir):
        return []
    command_name = get_tunnel_command_name(provider).lower()
    command_base = command_name.replace('.exe', '')
    markers = ['tunnel', '--url'] if provider == 'cloudflare' else ['http']
    expected_binary = os.path.abspath(binary_path).lower() if binary_path else ''
    pids = []
    try:
        for entry in os.listdir(proc_dir):
            if not entry.isdigit():
                continue
            pid = int(entry)
            if pid == os.getpid():
                continue
            cmdline_path = os.path.join(proc_dir, entry, 'cmdline')
            try:
                with open(cmdline_path, 'rb') as f:
                    cmdline = f.read().replace(b'\x00', b' ').decode('utf-8', errors='replace').strip()
            except Exception:
                continue
            lowered = cmdline.lower()
            exe_path = ''
            try:
                exe_path = os.path.abspath(os.readlink(os.path.join(proc_dir, entry, 'exe'))).lower()
            except Exception:
                pass
            path_matches = bool(expected_binary and exe_path == expected_binary)
            command_matches = (command_name in lowered or command_base in lowered) and all(marker in lowered for marker in markers)
            if path_matches or command_matches:
                if pid_is_running(pid):
                    pids.append(pid)
    except Exception as e:
        logger.debug(f"Failed to scan /proc for running {provider} tunnel process: {e}")
    return pids


def find_running_tunnel_pids(provider: str, binary_path: str = ''):
    command_name = get_tunnel_command_name(provider).lower()
    process_name = command_name[:-4] if command_name.endswith('.exe') else command_name
    markers = ['tunnel', '--url'] if provider == 'cloudflare' else ['http']
    expected_binary = os.path.normcase(os.path.abspath(binary_path or get_tunnel_binary_path(provider)))
    pids = []
    try:
        if os.name == 'nt':
            ps_script = f"Get-CimInstance Win32_Process -Filter \"name='{command_name}'\" | Select-Object ProcessId,CommandLine,ExecutablePath | ConvertTo-Json -Compress"
            result = subprocess.run(['powershell', '-NoProfile', '-Command', ps_script], capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=5)
            rows = []
            if result.returncode == 0 and result.stdout.strip():
                payload = json.loads(result.stdout)
                if payload:
                    rows = payload if isinstance(payload, list) else [payload]
            if not rows:
                ps_script = f"Get-Process -Name '{process_name}' -ErrorAction SilentlyContinue | Select-Object Id,Path | ConvertTo-Json -Compress"
                result = subprocess.run(['powershell', '-NoProfile', '-Command', ps_script], capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=5)
                if result.returncode == 0 and result.stdout.strip():
                    payload = json.loads(result.stdout)
                    proc_rows = payload if isinstance(payload, list) else ([payload] if payload else [])
                    rows = [{'ProcessId': r.get('Id'), 'ExecutablePath': r.get('Path'), 'CommandLine': ''} for r in proc_rows]
            for row in rows:
                if not row:
                    continue
                pid = int(row.get('ProcessId') or 0)
                command_line = str(row.get('CommandLine') or '').lower()
                executable = os.path.normcase(os.path.abspath(str(row.get('ExecutablePath') or ''))) if row.get('ExecutablePath') else ''
                path_matches = bool(expected_binary and executable == expected_binary)
                command_matches = command_name in command_line and all(marker in command_line for marker in markers)
                if pid and pid != os.getpid() and (path_matches or command_matches) and pid_is_running(pid):
                    pids.append(pid)
            if not pids and os.path.exists(expected_binary):
                result = subprocess.run(['tasklist', '/FI', f'IMAGENAME eq {command_name}', '/FO', 'CSV', '/NH'], capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=5)
                for line in result.stdout.splitlines():
                    match = re.match(r'"[^"]+","(\d+)"', line.strip())
                    if match:
                        pid = int(match.group(1))
                        if pid != os.getpid() and pid_is_running(pid):
                            pids.append(pid)
            return list(dict.fromkeys(pids))
        else:
            result = subprocess.run(
                ['ps', '-eo', 'pid=,args='],
                capture_output=True,
                text=True,
                encoding='utf-8',
                errors='replace',
                timeout=5,
            )
            lines = result.stdout.splitlines()

        for line in lines:
            text = line.strip()
            lowered = text.lower()
            if command_name not in lowered or not all(marker in lowered for marker in markers):
                continue
            pid_match = re.match(r'\s*(\d+)\s+', text)
            if pid_match:
                pid = int(pid_match.group(1))
                if pid != os.getpid() and pid_is_running(pid):
                    pids.append(pid)
    except Exception as e:
        logger.debug(f"Failed to scan running {provider} tunnel process: {e}")
    pids.extend(find_running_tunnel_pids_in_proc(provider, expected_binary))
    return list(dict.fromkeys(pids))


def find_running_tunnel_pid(provider: str):
    pids = find_running_tunnel_pids(provider)
    return pids[0] if pids else None


def kill_tunnel_processes(provider: str, binary_path: str = '', include_all_by_name: bool = False):
    pids = find_running_tunnel_pids(provider, binary_path)
    for pid in pids:
        kill_pid(pid)
    if os.name == 'nt' and include_all_by_name:
        subprocess.run(['taskkill', '/IM', get_tunnel_command_name(provider), '/T', '/F'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
    return pids


def get_tunnel_download(provider: str):
    system = platform.system().lower()
    machine = platform.machine().lower()

    if provider == 'cloudflare':
        if system == 'windows':
            arch = 'amd64' if machine in ('amd64', 'x86_64') else '386'
            return f'https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-{arch}.exe', 'exe'
        if system == 'darwin':
            arch = 'arm64' if 'arm' in machine else 'amd64'
            return f'https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-darwin-{arch}.tgz', 'tgz'
        arch = 'arm64' if 'aarch64' in machine or 'arm64' in machine else 'amd64'
        return f'https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-{arch}', 'bin'

    if provider == 'ngrok':
        if system == 'windows':
            os_part = 'windows'
            ext = 'zip'
        elif system == 'darwin':
            os_part = 'darwin'
            ext = 'zip'
        else:
            os_part = 'linux'
            ext = 'tgz'
        arch = 'arm64' if 'aarch64' in machine or 'arm64' in machine else 'amd64'
        return f'https://bin.equinox.io/c/bNyj1mQVY4c/ngrok-v3-stable-{os_part}-{arch}.{ext}', ext

    raise ValueError('Unsupported tunnel provider')


def install_tunnel_binary(provider: str):
    if is_tunnel_installed(provider):
        return find_tunnel_binary(provider)

    os.makedirs(BIN_DIR, exist_ok=True)
    url, archive_type = get_tunnel_download(provider)
    target = get_tunnel_binary_path(provider)
    tmp_path = os.path.join(BIN_DIR, f'{provider}.download')
    logger.info(f"Downloading {provider} tunnel binary from {url}")
    urllib.request.urlretrieve(url, tmp_path)

    if archive_type in ('bin', 'exe'):
        shutil.move(tmp_path, target)
    elif archive_type == 'zip':
        with zipfile.ZipFile(tmp_path) as zf:
            member = next((m for m in zf.namelist() if os.path.basename(m).lower() == get_tunnel_command_name(provider).lower()), None)
            if not member:
                raise RuntimeError(f'{provider} binary not found in downloaded archive')
            with zf.open(member) as src, open(target, 'wb') as dst:
                shutil.copyfileobj(src, dst)
        os.remove(tmp_path)
    elif archive_type == 'tgz':
        with tarfile.open(tmp_path, 'r:gz') as tf:
            member = next((m for m in tf.getmembers() if os.path.basename(m.name).lower() == get_tunnel_command_name(provider).lower()), None)
            if not member:
                raise RuntimeError(f'{provider} binary not found in downloaded archive')
            extracted = tf.extractfile(member)
            if not extracted:
                raise RuntimeError(f'Cannot extract {provider} binary')
            with extracted as src, open(target, 'wb') as dst:
                shutil.copyfileobj(src, dst)
        os.remove(tmp_path)
    else:
        raise RuntimeError(f'Unsupported download format: {archive_type}')

    if os.name != 'nt':
        os.chmod(target, 0o755)
    return target


def get_tunnel_public_urls(provider: str, runtime: TunnelRuntime):
    urls = []
    if runtime.public_url:
        urls.append(runtime.public_url)
    state_url = load_tunnel_state().get(provider, {}).get('public_url')
    if state_url:
        urls.append(state_url)
    if provider == 'ngrok' and runtime.process and runtime.process.poll() is None:
        try:
            with urllib.request.urlopen('http://127.0.0.1:4040/api/tunnels', timeout=1) as resp:
                payload = json.loads(resp.read().decode('utf-8'))
            for item in payload.get('tunnels', []):
                public_url = item.get('public_url')
                if public_url and public_url.startswith('https://'):
                    urls.append(public_url)
        except Exception:
            pass
    return list(dict.fromkeys(urls))


def watch_tunnel_output(provider: str):
    runtime = TUNNEL_RUNTIMES[provider]
    process = runtime.process
    if not process or not process.stdout:
        return
    try:
        for line in iter(process.stdout.readline, ''):
            if not line:
                break
            text = line.strip()
            with TUNNEL_LOCK:
                runtime.output.append(text)
                runtime.output = runtime.output[-60:]
                match = TUNNEL_URL_RE.search(text)
                if match:
                    runtime.public_url = match.group(0).rstrip('.,')
                    update_tunnel_state(provider, public_url=runtime.public_url)
    except Exception as e:
        with TUNNEL_LOCK:
            runtime.last_error = str(e)


def build_tunnel_command(provider: str, binary: str, local_url: str, authtoken: str = ''):
    if provider == 'cloudflare':
        return [binary, 'tunnel', '--url', local_url, '--no-autoupdate']
    if provider == 'ngrok':
        cmd = [binary, 'http', local_url, '--log=stdout']
        return cmd
    raise ValueError('Unsupported tunnel provider')


def start_tunnel(provider: str, local_url: str, authtoken: str = ''):
    binary = find_tunnel_binary(provider)
    if not binary:
        raise RuntimeError(f'{provider} is not installed')

    runtime = TUNNEL_RUNTIMES[provider]
    with TUNNEL_LOCK:
        if runtime.process and runtime.process.poll() is None:
            return runtime
        runtime.public_url = ''
        runtime.last_error = ''
        runtime.output = []
        runtime.started_at = datetime.now().isoformat()

        creationflags = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
        env = os.environ.copy()
        if provider == 'ngrok' and authtoken:
            env['NGROK_AUTHTOKEN'] = authtoken

        runtime.process = subprocess.Popen(
            build_tunnel_command(provider, binary, local_url, authtoken),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding='utf-8',
            errors='replace',
            cwd=application_path,
            env=env,
            creationflags=creationflags,
        )
        runtime.pid = runtime.process.pid
        update_tunnel_state(
            provider,
            pid=runtime.pid,
            public_url='',
            started_at=runtime.started_at,
            binary=os.path.abspath(binary),
        )

    threading.Thread(target=watch_tunnel_output, args=(provider,), daemon=True).start()
    return runtime


def stop_tunnel(provider: str):
    runtime = TUNNEL_RUNTIMES[provider]
    process = runtime.process
    state = load_tunnel_state().get(provider, {})
    pids_to_stop = []
    if process and process.poll() is None:
        pids_to_stop.append(process.pid)
        try:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        except Exception as e:
            with TUNNEL_LOCK:
                runtime.last_error = str(e)
            raise
    if state.get('pid') and pid_is_running(state.get('pid')):
        pids_to_stop.append(state.get('pid'))
    pids_to_stop.extend(find_running_tunnel_pids(provider, state.get('binary') or get_tunnel_binary_path(provider)))
    for pid in dict.fromkeys(pids_to_stop):
        if pid_is_running(pid):
            kill_pid(pid)

    with TUNNEL_LOCK:
        runtime.process = None
        runtime.pid = None
        runtime.public_url = ''
        runtime.started_at = None
        runtime.last_error = ''
    clear_tunnel_state(provider)
    return runtime


def delete_tunnel_binary(provider: str):
    stop_tunnel(provider)
    bundled = get_tunnel_binary_path(provider)
    if os.path.exists(bundled):
        wait_for_path_release(bundled, seconds=8)
        try:
            os.remove(bundled)
        except PermissionError:
            killed = kill_tunnel_processes(provider, bundled, include_all_by_name=True)
            if killed:
                wait_for_path_release(bundled, seconds=8)
            try:
                os.remove(bundled)
            except PermissionError:
                raise RuntimeError('Cannot delete tunnel binary because Windows still keeps it locked. All detected cloudflared/ngrok processes were stopped; close any antivirus scan, terminal, or external process using the file and try again.')
    elif shutil.which(get_tunnel_command_name(provider)):
        raise RuntimeError('This tunnel binary is installed system-wide. Remove it from PATH manually or use the panel-managed installation.')

    tmp_path = os.path.join(BIN_DIR, f'{provider}.download')
    if os.path.exists(tmp_path):
        os.remove(tmp_path)

    runtime = TUNNEL_RUNTIMES[provider]
    with TUNNEL_LOCK:
        runtime.output = []
    clear_tunnel_state(provider)
    return runtime


def get_tunnel_status(provider: str):
    runtime = TUNNEL_RUNTIMES[provider]
    state = load_tunnel_state().get(provider, {})
    running = bool(runtime.process and runtime.process.poll() is None)
    if not running and state.get('pid'):
        running = pid_is_running(state.get('pid'))
        if running:
            runtime.pid = state.get('pid')
            runtime.started_at = runtime.started_at or state.get('started_at')
            runtime.public_url = runtime.public_url or state.get('public_url', '')
        else:
            clear_tunnel_state(provider)
            state = {}
    if not running and is_tunnel_installed(provider):
        found_pid = find_running_tunnel_pid(provider)
        if found_pid:
            running = True
            runtime.pid = found_pid
            runtime.started_at = runtime.started_at or datetime.now().isoformat()
            update_tunnel_state(
                provider,
                pid=found_pid,
                public_url=runtime.public_url,
                started_at=runtime.started_at,
                binary=os.path.abspath(find_tunnel_binary(provider) or ''),
            )
            state = load_tunnel_state().get(provider, {})
    if runtime.process and not running and not runtime.last_error and runtime.output:
        runtime.last_error = runtime.output[-1]
    urls = get_tunnel_public_urls(provider, runtime)
    return {
        'installed': is_tunnel_installed(provider),
        'running': running,
        'public_url': urls[0] if urls else '',
        'public_urls': urls,
        'last_error': runtime.last_error,
        'started_at': runtime.started_at or state.get('started_at'),
    }


async def wait_for_tunnel_url(provider: str, seconds: int = 20):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        status = get_tunnel_status(provider)
        if status.get('public_url') or not status.get('running'):
            return status
        await asyncio.sleep(0.5)
    return get_tunnel_status(provider)


BASE_PROTOCOLS = ['awg', 'awg2', 'awg_legacy', 'xray', 'telemt', 'dns', 'wireguard', 'socks5', 'adguard', 'nginx']
MULTI_INSTANCE_PROTOCOLS = {'awg', 'awg2', 'awg_legacy', 'xray', 'telemt', 'socks5'}


def protocol_base(protocol: str) -> str:
    return str(protocol or 'awg').split('__', 1)[0]


def protocol_instance(protocol: str) -> int:
    parts = str(protocol or '').split('__', 1)
    if len(parts) == 2:
        try:
            return max(1, int(parts[1]))
        except ValueError:
            return 1
    return 1


def protocol_key(base: str, instance: int = 1) -> str:
    base = protocol_base(base)
    return base if int(instance or 1) <= 1 else f'{base}__{int(instance)}'


def next_protocol_key(protocols: dict, base: str) -> str:
    base = protocol_base(base)
    used = {protocol_instance(k) for k in (protocols or {}).keys() if protocol_base(k) == base}
    idx = 1
    while idx in used:
        idx += 1
    return protocol_key(base, idx)


def protocol_display_name(protocol: str) -> str:
    base = protocol_base(protocol)
    idx = protocol_instance(protocol)
    names = {
        'awg': 'AmneziaWG',
        'awg2': 'AmneziaWG 2.0',
        'awg_legacy': 'AmneziaWG Legacy',
        'xray': 'Xray',
        'telemt': 'Telemt',
        'dns': 'AmneziaDNS',
        'wireguard': 'WireGuard',
        'socks5': 'SOCKS5',
        'adguard': 'AdGuard Home',
        'nginx': 'NGINX',
    }
    name = names.get(base, base)
    return name if idx <= 1 else f'{name} #{idx}'


def protocol_container_name(protocol: str) -> Optional[str]:
    base = protocol_base(protocol)
    idx = protocol_instance(protocol)
    base_names = {
        'awg': 'amnezia-awg',
        'awg2': 'amnezia-awg2',
        'awg_legacy': 'amnezia-awg-legacy',
        'xray': 'amnezia-xray',
        'telemt': 'telemt',
        'dns': 'amnezia-dns',
        'wireguard': 'amnezia-wireguard',
        'socks5': 'amnezia-socks5proxy',
        'adguard': 'amnezia-adguard',
        'nginx': 'amnezia-nginx',
    }
    name = base_names.get(base)
    if not name:
        return None
    return name if idx <= 1 else f'{name}-{idx}'


def is_valid_protocol(protocol: str) -> bool:
    return protocol_base(protocol) in BASE_PROTOCOLS


def get_protocol_manager(ssh, protocol: str):
    base = protocol_base(protocol)
    if base == 'xray':
        from managers.xray_manager import XrayManager
        return XrayManager(ssh, protocol)
    elif base == 'telemt':
        from managers.telemt_manager import TelemtManager
        return TelemtManager(ssh, protocol)
    elif base == 'dns':
        from managers.dns_manager import DNSManager
        return DNSManager(ssh)
    elif base == 'wireguard':
        from managers.wireguard_manager import WireGuardManager
        return WireGuardManager(ssh)
    elif base == 'socks5':
        from managers.socks5_manager import Socks5Manager
        return Socks5Manager(ssh, protocol)
    elif base == 'adguard':
        from managers.adguard_manager import AdguardManager
        return AdguardManager(ssh)
    elif base == 'nginx':
        from managers.nginx_manager import NginxManager
        return NginxManager(ssh, protocol)
    from managers.awg_manager import AWGManager
    return AWGManager(ssh)



def ensure_docker_installed(ssh):
    """Ensure Docker is installed and running before installing any protocol/service."""
    out, _, code = ssh.run_command("docker --version 2>/dev/null")
    if code == 0 and out.strip():
        status, _, _ = ssh.run_command("systemctl is-active docker 2>/dev/null || service docker status 2>/dev/null")
        if 'active' in status or 'running' in status.lower():
            return 'Docker already installed'
        ssh.run_sudo_command("systemctl enable --now docker 2>/dev/null || service docker start 2>/dev/null || true", timeout=60)
        status, _, _ = ssh.run_command("systemctl is-active docker 2>/dev/null || service docker status 2>/dev/null")
        if 'active' in status or 'running' in status.lower():
            return 'Docker service started'

    script = r"""
set -e
if command -v apt-get >/dev/null 2>&1; then
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -y
  apt-get install -y docker.io
elif command -v dnf >/dev/null 2>&1; then
  dnf install -y docker
elif command -v yum >/dev/null 2>&1; then
  yum install -y docker
elif command -v zypper >/dev/null 2>&1; then
  zypper --non-interactive refresh
  zypper --non-interactive install docker
elif command -v pacman >/dev/null 2>&1; then
  pacman -Sy --noconfirm --noprogressbar docker
else
  echo "Packet manager not found" >&2
  exit 1
fi
(systemctl enable --now docker || service docker start || true)
sleep 3
docker --version
"""
    out, err, code = ssh.run_sudo_script(script, timeout=300)
    if code != 0:
        raise RuntimeError(f"Failed to install Docker: {err or out}")
    status, _, _ = ssh.run_command("systemctl is-active docker 2>/dev/null || service docker status 2>/dev/null")
    if 'active' not in status and 'running' not in status.lower():
        raise RuntimeError("Docker installed but service is not running")
    return 'Docker installed successfully'


def _manager_call(manager, method, protocol, *args, **kwargs):
    """Unified call: WireGuard manager methods don't take protocol_type argument."""
    fn = getattr(manager, method)
    if isinstance(manager, WireGuardManager):
        return fn(*args, **kwargs)
    return fn(protocol, *args, **kwargs)


def generate_vpn_link(config_text):
    """Encode config into the Amnezia-native ``vpn://`` link format."""
    return _amnezia_generate_vpn_link(config_text)

def generate_qr_content(config_text):
    """Encode config into the Amnezia-native QR code payload format."""
    return _amnezia_generate_qr_content(config_text)


# ===================== API tokens =====================

API_TOKEN_PREFIX = 'awp_'  # "Amnezia Web Panel" — makes tokens visually distinct in logs / configs
API_TOKEN_TOUCH_INTERVAL = 300  # don't re-write data.json more than once per 5 min per token


def _hash_api_token(raw: str) -> str:
    """One-way hash of a raw token. We never store the original token — only the
    SHA-256 digest, plus a short prefix for the UI to identify rotations."""
    return hashlib.sha256(raw.encode('utf-8')).hexdigest()


def _generate_api_token() -> str:
    """Generate a fresh bearer token. ~256 bits of entropy with a recognizable
    'awp_' prefix so leaked tokens are obvious in source control / pastes."""
    return f"{API_TOKEN_PREFIX}{secrets.token_urlsafe(32)}"


def _resolve_api_token(data: dict, raw_token: str):
    """Match a raw bearer token against stored hashes. Returns the user record
    that owns the token, or None if the token is unknown / its owner is gone /
    its owner is no longer admin-or-support."""
    if not raw_token:
        return None
    token_hash = _hash_api_token(raw_token)
    entry = next(
        (t for t in data.get('api_tokens', []) if t.get('token_hash') == token_hash),
        None,
    )
    if not entry:
        return None
    user = next((u for u in data.get('users', []) if u['id'] == entry.get('user_id')), None)
    if not user:
        return None
    # Disabled or downgraded admins should not have a working token any more.
    if not user.get('enabled', True):
        return None
    if user.get('role') not in ('admin', 'support'):
        return None
    return (entry, user)


def _touch_api_token(token_entry: dict) -> bool:
    """Update last_used_at on a token entry, but only if enough time has passed
    since the previous touch — avoids hot-write loops under load. Returns True
    if the entry was updated and the caller should persist data."""
    now = datetime.now()
    last = token_entry.get('last_used_at')
    if last:
        try:
            prev = datetime.fromisoformat(last)
            if (now - prev).total_seconds() < API_TOKEN_TOUCH_INTERVAL:
                return False
        except Exception:
            pass
    token_entry['last_used_at'] = now.isoformat()
    return True


def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    iterations = 600000
    h = hashlib.pbkdf2_hmac('sha256', password.encode(), salt.encode(), iterations)
    return f"{iterations}${salt}${h.hex()}"


def verify_password(password: str, password_hash: str) -> bool:
    import hmac
    try:
        parts = password_hash.split('$')
        if len(parts) == 2:
            salt, h = parts
            iterations = 100000
        elif len(parts) == 3:
            iters_str, salt, h = parts
            iterations = int(iters_str)
        else:
            return False

        new_h = hashlib.pbkdf2_hmac('sha256', password.encode(), salt.encode(), iterations)
        return hmac.compare_digest(new_h.hex(), h)
    except Exception:
        return False


async def perform_delete_user(db, user_id: str) -> bool:
    from database import models
    user = await db.get(models.User, user_id)
    if not user:
        return False
    
    # Remove user's connections from servers efficiently
    await perform_mass_operations(db, delete_uids=[user_id])
    
    await db.delete(user)
    await db.commit()
    return True


async def perform_toggle_user(db, user_id: str, enable: bool) -> bool:
    """Enable or disable a user and propagate the change to all their VPN connections."""
    from database import models
    user = await db.get(models.User, user_id)
    if not user:
        return False

    user.enabled = enable
    await db.commit()

    # Toggle connections on servers
    await perform_mass_operations(db, toggle_uids=[(user_id, enable)])
    
    return True


async def perform_mass_operations(db, delete_uids: List[str] = None, toggle_uids: List[tuple] = None, create_conns: List[dict] = None):
    """Executes multiple SSH operations efficiently."""
    from database import models
    from sqlalchemy import select
    from sqlalchemy.orm import selectinload

    server_ops = {}
    def get_ops(sid):
        if sid not in server_ops:
            server_ops[sid] = {'delete': [], 'toggle': [], 'create': []}
        return server_ops[sid]

    # Collect operations
    if delete_uids:
        stmt = select(models.UserConnection).where(models.UserConnection.user_id.in_(delete_uids))
        res = await db.execute(stmt)
        for c in res.scalars().all():
            get_ops(c.server_id)['delete'].append(c)

    if toggle_uids:
        for uid, enabled in toggle_uids:
            stmt = select(models.UserConnection).where(models.UserConnection.user_id == uid)
            res = await db.execute(stmt)
            for c in res.scalars().all():
                get_ops(c.server_id)['toggle'].append((c, enabled))

    if create_conns:
        for req in create_conns:
            get_ops(req['server_id'])['create'].append(req)

    async def run_server_ops(srv_id, ops):
        srv = await db.get(models.Server, srv_id)
        if not srv: return
        
        try:
            ssh = get_ssh(srv)
            await asyncio.to_thread(ssh.connect)
            
            # 1. Deletes
            for c in ops['delete']:
                manager = get_protocol_manager(ssh, c.protocol)
                await asyncio.to_thread(_manager_call, manager, 'remove_client', c.protocol, c.client_id)
                await db.delete(c)
                await db.commit()
            
            # 2. Toggles
            for c, enabled in ops['toggle']:
                manager = get_protocol_manager(ssh, c.protocol)
                await asyncio.to_thread(_manager_call, manager, 'toggle_client', c.protocol, c.client_id, enabled)
            
            # 3. Creates
            for c_req in ops['create']:
                stmt = select(models.ServerProtocol).where(models.ServerProtocol.server_id == srv_id, models.ServerProtocol.protocol_type == c_req['protocol'])
                res = await db.execute(stmt)
                proto_record = res.scalars().first()
                port = proto_record.port if proto_record and proto_record.port else 55424
                
                manager = get_protocol_manager(ssh, c_req['protocol'])
                if c_req['protocol'] == 'wireguard':
                    res_add = await asyncio.to_thread(manager.add_client, c_req['name'], srv.host)
                else:
                    res_add = await asyncio.to_thread(_manager_call, manager, 'add_client', c_req['protocol'], c_req['name'], srv.host, port)
                
                if res_add.get('client_id'):
                    new_conn = models.UserConnection(
                        id=str(uuid.uuid4()),
                        user_id=c_req['user_id'],
                        server_id=srv_id,
                        protocol=c_req['protocol'],
                        client_id=res_add['client_id'],
                        name=c_req['name'],
                        created_at=datetime.now(),
                    )
                    db.add(new_conn)
                    await db.commit()
            
            await asyncio.to_thread(ssh.disconnect)
        except Exception as e:
            logger.error(f"Mass ops failed for server {srv_id}: {e}")

    tasks = [run_server_ops(sid, ops) for sid, ops in server_ops.items()]
    if tasks:
        await asyncio.gather(*tasks)

    # Final user-level cleanup
    if delete_uids:
        stmt = select(models.User).where(models.User.id.in_(delete_uids))
        res = await db.execute(stmt)
        for u in res.scalars().all():
            await db.delete(u)
        await db.commit()

    if toggle_uids:
        for uid, enabled in toggle_uids:
            u = await db.get(models.User, uid)
            if u:
                u.enabled = enabled
        await db.commit()

    return True

async def sync_users_with_remnawave(db):
    from database import models
    from sqlalchemy import select
    
    settings = {}
    stmt = select(models.Setting).where(models.Setting.key == 'settings')
    res = await db.execute(stmt)
    setting_obj = res.scalars().first()
    if setting_obj and setting_obj.value:
        settings = setting_obj.value.get('sync', {})
        
    if not settings.get('remnawave_sync_users'):
        return 0, "Synchronization is disabled in settings"
    
    url = settings.get('remnawave_url')
    api_key = settings.get('remnawave_api_key')
    if not url or not api_key:
        return 0, "Remnawave URL or API Key not configured"
    
    api_url = url.rstrip('/') + '/api/users'
    headers = {"Authorization": f"Bearer {api_key}"}
    
    try:
        rw_users = []
        async with httpx.AsyncClient(timeout=30.0) as client:
            page_size = 50
            current_start = 0
            while True:
                resp = await client.get(f"{api_url}?size={page_size}&start={current_start}", headers=headers)
                if resp.status_code != 200:
                    return 0, f"Remnawave API error: {resp.status_code} {resp.text}"
                
                page_data = resp.json()
                response_obj = page_data.get('response', {})
                page_users = response_obj.get('users', [])
                total_count = response_obj.get('total', 0)
                
                if not page_users: break
                
                rw_users.extend(page_users)
                if len(rw_users) >= total_count or len(page_users) == 0: break
                current_start += len(page_users)

            rw_uuids = {u['uuid'] for u in rw_users}
            
            stmt = select(models.User)
            res = await db.execute(stmt)
            all_users = res.scalars().all()
            
            # 1. Handle deletion
            to_delete_ids = []
            for u in all_users:
                if u.remnawave_uuid and u.remnawave_uuid not in rw_uuids:
                    to_delete_ids.append(u.id)
            
            if to_delete_ids:
                logger.info(f"Removing {len(to_delete_ids)} users deleted in Remnawave")
                await perform_mass_operations(db, delete_uids=to_delete_ids)

            # 2. Sync / Create users
            synced_count = 0
            to_toggle = []
            to_create_conns = []
            
            for rw_u in rw_users:
                stmt = select(models.User).where(models.User.remnawave_uuid == rw_u['uuid'])
                res = await db.execute(stmt)
                local_u = res.scalars().first()
                if not local_u:
                    stmt = select(models.User).where(models.User.username == rw_u['username'])
                    res = await db.execute(stmt)
                    local_u = res.scalars().first()

                is_active = (rw_u.get('status') == 'ACTIVE')
                
                if local_u:
                    local_u.username = rw_u['username']
                    local_u.telegramId = rw_u.get('telegramId')
                    local_u.email = rw_u.get('email')
                    local_u.description = rw_u.get('description')
                    local_u.remnawave_uuid = rw_u['uuid']
                    
                    if getattr(local_u, 'enabled', True) != is_active:
                        to_toggle.append((local_u.id, is_active))
                        local_u.enabled = is_active
                        
                    await db.commit()
                    synced_count += 1
                else:
                    new_id = str(uuid.uuid4())
                    new_user = models.User(
                        id=new_id,
                        username=rw_u['username'],
                        password_hash='',
                        role='user',
                        telegramId=rw_u.get('telegramId'),
                        email=rw_u.get('email'),
                        description=rw_u.get('description'),
                        enabled=is_active,
                        remnawave_uuid=rw_u['uuid'],
                        share_enabled=False,
                        share_token=secrets.token_urlsafe(16),
                    )
                    db.add(new_user)
                    await db.commit()
                    
                    if settings.get('remnawave_create_conns'):
                        sid = settings.get('remnawave_server_id')
                        if sid is not None:
                            to_create_conns.append({
                                'user_id': new_id,
                                'server_id': sid,
                                'protocol': settings.get('remnawave_protocol', 'awg'),
                                'name': f"{rw_u['username']}_vpn"
                            })
                    synced_count += 1
            
            if to_toggle or to_create_conns:
                logger.info(f"Executing mass ops for Remnawave sync: toggle={len(to_toggle)}, create={len(to_create_conns)}")
                await perform_mass_operations(db, toggle_uids=to_toggle, create_conns=to_create_conns)
            
            return synced_count, "Successfully synchronized with Remnawave"
            
    except Exception as e:
        logger.exception("Synchronization error")
        return 0, f"Error: {str(e)}"

async def get_current_user(db, request: Request):
    user_id = request.session.get('user_id')
    if not user_id:
        return None
    from database import models
    return await db.get(models.User, user_id)


async def _check_admin(db, request: Request):
    user = await get_current_user(db, request)
    if user and user.role in ('admin', 'support'):
        return user
    auth_header = request.headers.get('Authorization', '')
    if auth_header.lower().startswith('bearer '):
        raw_token = auth_header[7:].strip()
        from sqlalchemy import select
        from sqlalchemy.orm import selectinload
        import hashlib
        from database import models
        token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
        stmt = select(models.ApiToken).where(models.ApiToken.token_hash == token_hash).options(selectinload(models.ApiToken.user))
        res = await db.execute(stmt)
        token = res.scalars().first()
        if token and token.user and token.user.role in ('admin', 'support') and token.user.enabled:
            now = models.utcnow()
            if not token.last_used_at or (now - token.last_used_at).total_seconds() > 300:
                token.last_used_at = now
                await db.commit()
            return token.user
    return None



async def tpl(db, request, template, **kwargs):
    from database.models import Setting
    from sqlalchemy import select
    res = await db.execute(select(Setting))
    settings = {s.key: s.value for s in res.scalars().all()}
    main_settings = settings.get('settings', {})
    
    lang = request.cookies.get('lang', 'en')
    current_user = await get_current_user(db, request)
    
    ctx = {
        'request': request,
        'current_user': current_user,
        'site_settings': main_settings.get('appearance', {}),
        'captcha_settings': main_settings.get('captcha', {}),
        'telegram_settings': main_settings.get('telegram', {}),
        'bot_running': tg_bot.is_running(),
        'lang': lang,
        '_': lambda text_id: _t(text_id, lang),
        'translations_json': json.dumps(TRANSLATIONS.get(lang, TRANSLATIONS.get('en', {}))),
        'all_translations_json': json.dumps(TRANSLATIONS)
    }
    ctx.update(kwargs)
    return templates.TemplateResponse(template, ctx)


# ======================== Pydantic Models ========================

class LoginRequest(BaseModel):
    username: str
    password: str
    captcha: Optional[str] = None


class AddServerRequest(BaseModel):
    host: str = ''
    ssh_port: int = 22
    username: str = ''
    password: str = ''
    private_key: str = ''
    name: str = ''


class EditServerRequest(BaseModel):
    name: str = ''
    host: str = ''
    ssh_port: int = 22
    username: str = ''
    # Optional[str] = None lets the client distinguish "leave field as is"
    # (omit / null) from "explicitly clear" (empty string). Both credential
    # fields can be omitted to keep current auth unchanged.
    password: Optional[str] = None
    private_key: Optional[str] = None


class ReorderServersRequest(BaseModel):
    # `order[i]` is the *old* server index now at position `i` in the new layout.
    order: List[int]


class InstallProtocolRequest(BaseModel):
    protocol: str = 'awg'
    port: str = '55424'
    install_another: Optional[bool] = False
    tls_emulation: Optional[bool] = None
    tls_domain: Optional[str] = None
    max_connections: Optional[int] = None
    # SOCKS5
    socks5_username: Optional[str] = None
    socks5_password: Optional[str] = None
    # AdGuard Home
    adguard_mode: Optional[str] = None  # 'replace' or 'sidebyside'
    adguard_web_port: Optional[int] = None
    adguard_expose_web: Optional[bool] = None
    adguard_dot_port: Optional[int] = None
    adguard_doh_port: Optional[int] = None
    adguard_expose_dns: Optional[bool] = None
    adguard_expose_dot: Optional[bool] = None
    adguard_expose_doh: Optional[bool] = None
    # NGINX
    nginx_domain: Optional[str] = None
    nginx_email: Optional[str] = None


class Socks5SettingsRequest(BaseModel):
    protocol: str = 'socks5'
    port: Optional[int] = None
    username: Optional[str] = None
    password: Optional[str] = None


class ProtocolRequest(BaseModel):
    protocol: str = 'awg'


class AddConnectionRequest(BaseModel):
    protocol: str = 'awg'
    name: str = 'Connection'
    user_id: Optional[str] = None
    telemt_quota: Optional[str] = None
    telemt_max_ips: Optional[int] = None
    telemt_expiry: Optional[str] = None
    telemt_secret: Optional[str] = None
    telemt_ad_tag: Optional[str] = None
    telemt_max_conns: Optional[int] = None


class EditConnectionRequest(BaseModel):
    protocol: str = 'telemt'
    client_id: str = ''
    telemt_quota: Optional[str] = None
    telemt_max_ips: Optional[int] = None
    telemt_expiry: Optional[str] = None
    telemt_secret: Optional[str] = None
    telemt_ad_tag: Optional[str] = None
    telemt_max_conns: Optional[int] = None


class ConnectionActionRequest(BaseModel):
    protocol: str = 'awg'
    client_id: str = ''


class ToggleConnectionRequest(BaseModel):
    protocol: str = 'awg'
    client_id: str = ''
    enable: bool = True


class AddUserRequest(BaseModel):
    username: str
    password: str
    role: str = 'user'
    telegramId: Optional[str] = None
    email: Optional[str] = None
    description: Optional[str] = None
    traffic_limit: Optional[float] = 0
    traffic_reset_strategy: Optional[str] = 'never'
    server_id: Optional[int] = None
    protocol: Optional[str] = None
    connection_name: Optional[str] = None
    expiration_date: Optional[str] = None
    telemt_quota: Optional[str] = None
    telemt_max_ips: Optional[int] = None
    telemt_expiry: Optional[str] = None
    telemt_secret: Optional[str] = None
    telemt_ad_tag: Optional[str] = None
    telemt_max_conns: Optional[int] = None



class ServerConfigSaveRequest(BaseModel):
    protocol: str
    config: str


class NginxSiteSaveRequest(BaseModel):
    protocol: str = 'nginx'
    html: str = ''


class BackupDownloadRequest(BaseModel):
    protocol: str
    filename: str


class AppearanceSettings(BaseModel):
    title: str = 'Amnezia'
    logo: str = '🛡'
    subtitle: str = 'Web Panel'


class SyncSettings(BaseModel):
    remnawave_url: str = ''
    remnawave_api_key: str = ''
    remnawave_sync: bool = False
    remnawave_sync_users: bool = False
    remnawave_create_conns: bool = False
    remnawave_server_id: int = 0
    remnawave_protocol: str = 'awg'

class CaptchaSettings(BaseModel):
    enabled: bool = False


class SSLSettings(BaseModel):
    enabled: bool = False
    domain: str = ''
    cert_path: str = ''
    key_path: str = ''
    cert_text: str = ''
    key_text: str = ''
    panel_port: int = 5000

class TelegramSettings(BaseModel):
    token: str = ''
    enabled: bool = False




class UpdateUserRequest(BaseModel):
    telegramId: Optional[str] = None
    email: Optional[str] = None
    description: Optional[str] = None
    traffic_limit: Optional[float] = 0
    traffic_reset_strategy: Optional[str] = None
    expiration_date: Optional[str] = None
    password: Optional[str] = None



class SaveSettingsRequest(BaseModel):
    appearance: AppearanceSettings
    sync: SyncSettings
    captcha: CaptchaSettings
    telegram: TelegramSettings
    ssl: SSLSettings


class ToggleUserRequest(BaseModel):
    enabled: bool


class AddUserConnectionRequest(BaseModel):
    server_id: int
    protocol: str = 'awg'
    name: str = 'VPN Connection'
    client_id: Optional[str] = None
    telemt_quota: Optional[str] = None
    telemt_max_ips: Optional[int] = None
    telemt_expiry: Optional[str] = None
    telemt_secret: Optional[str] = None
    telemt_ad_tag: Optional[str] = None
    telemt_max_conns: Optional[int] = None


class CreateApiTokenRequest(BaseModel):
    name: str


class ShareSetupRequest(BaseModel):
    enabled: bool
    password: Optional[str] = None


class ShareAuthRequest(BaseModel):
    password: str


class TunnelStartRequest(BaseModel):
    authtoken: Optional[str] = None


# ======================== Startup ========================

async def _startup():
    from database.session import engine, async_session
    from database.models import Base
    from database.repositories import UserRepository, SettingRepository

    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
    except Exception as e:
        logger.error(
            "Could not connect to database (%s).\n"
            "If using PostgreSQL in dev mode, make sure the container is running:\n"
            "    docker compose -f docker-compose.dev.yml up -d\n"
            "Or set DATABASE_URL=sqlite+aiosqlite:///app.db in your .env for local SQLite.",
            e
        )
        raise e

    async with async_session() as db:
        await UserRepository.ensure_default_admin(db)
        
        settings_dict = await SettingRepository.get(db, 'settings', default={})
        if 'ssl' not in settings_dict:
            settings_dict['ssl'] = {
                'enabled': False,
                'domain': '',
                'cert_path': '',
                'key_path': '',
                'cert_text': '',
                'key_text': '',
                'panel_port': 5000
            }
            await SettingRepository.set(db, 'settings', settings_dict)
            logger.info("Migrated SSL settings")

        # Telegram
        tg_cfg = settings_dict.get('telegram', {})
        if tg_cfg.get('enabled') and tg_cfg.get('token'):
            logger.info("Starting Telegram bot from saved settings...")
            tg_bot.launch_bot(tg_cfg['token'], _amnezia_generate_vpn_link)

    global _bg_traffic, _bg_backup
    _bg_traffic = asyncio.create_task(periodic_background_tasks())
    _bg_backup = asyncio.create_task(_background_backup())


def _scrape_server_traffic(server, sid, my_conns):
    server_updates = []
    try:
        ssh = get_ssh(server)
        ssh.connect()
        for proto in ['awg', 'awg2', 'awg_legacy', 'xray', 'telemt', 'wireguard']:
            if proto in server.get('protocols', {}):
                manager = get_protocol_manager(ssh, proto)
                clients = _manager_call(manager, 'get_clients', proto)
                client_bytes = {}
                for c in clients:
                    rx = c.get('userData', {}).get('dataReceivedBytes', 0)
                    tx = c.get('userData', {}).get('dataSentBytes', 0)
                    client_bytes[c.get('clientId')] = rx + tx
                    
                for uc in my_conns:
                    if uc['protocol'] == proto and uc['client_id'] in client_bytes:
                        curr_bytes = client_bytes[uc['client_id']]
                        last_bytes = uc.get('last_bytes', 0)
                        delta = curr_bytes - last_bytes if curr_bytes >= last_bytes else curr_bytes
                        server_updates.append((uc['id'], delta, curr_bytes))
        ssh.disconnect()
    except Exception as e:
        logger.error(f"Traffic sync err server {sid}: {e}")
    return server_updates


async def _background_backup():
    """Periodically copy data.json to a timestamped backup, pruning old copies.

    BACKUP_INTERVAL_HOURS=0 disables the loop. All backups inherit the Fernet
    encryption applied to SSH secrets — nothing plaintext is ever written.
    """
    while True:
        interval = config.BACKUP_INTERVAL_HOURS * 3600
        if interval <= 0:
            await asyncio.sleep(3600)
            continue
        await asyncio.sleep(interval)
        try:
            backup_dir = os.environ.get('BACKUP_DIR', os.path.join(os.path.dirname(DATA_FILE), 'backups'))
            os.makedirs(backup_dir, exist_ok=True)
            stamp = datetime.now().strftime('%Y%m%d-%H%M%S')
            dest = os.path.join(backup_dir, f'data-{stamp}.json')
            shutil.copy2(DATA_FILE, dest)
            logger.info(f'Auto-backup saved to {dest}')

            # Prune oldest files when count exceeds BACKUP_KEEP_COUNT.
            keep = config.BACKUP_KEEP_COUNT
            entries = sorted(
                f for f in os.listdir(backup_dir)
                if f.startswith('data-') and f.endswith('.json')
            )
            while len(entries) > keep:
                removed = entries.pop(0)
                os.remove(os.path.join(backup_dir, removed))
                logger.debug(f'Pruned old backup: {removed}')
        except Exception as e:
            logger.error(f'Auto-backup failed: {e}')


async def periodic_background_tasks():
    from database.session import async_session
    from database import models
    from sqlalchemy import select
    from sqlalchemy.orm import selectinload
    while True:
        try:
            await asyncio.sleep(60)
            logger.info("Starting background traffic sync...")
            
            async with async_session() as db:
                stmt = select(models.Server).options(selectinload(models.Server.protocols))
                res = await db.execute(stmt)
                servers = res.scalars().all()
                
                stmt = select(models.UserConnection)
                res = await db.execute(stmt)
                all_conns = res.scalars().all()
                conns_by_server = {}
                for uc in all_conns:
                    conns_by_server.setdefault(uc.server_id, []).append(uc)
                    
                updates = []
                for srv in servers:
                    if srv.id not in conns_by_server: continue
                    # Transform server and conns to dicts for _scrape_server_traffic
                    srv_dict = {
                        "host": srv.host, "ssh_port": srv.ssh_port, "username": srv.username,
                        "password": srv.password, "private_key": srv.private_key,
                        "protocols": {p.protocol_type: {} for p in srv.protocols}
                    }
                    conns_dict = [{"id": uc.id, "protocol": uc.protocol, "client_id": uc.client_id, "last_bytes": uc.last_bytes} for uc in conns_by_server[srv.id]]
                    server_updates = await asyncio.to_thread(_scrape_server_traffic, srv_dict, srv.id, conns_dict)
                    if server_updates:
                        updates.extend(server_updates)
                        
                to_disable_uids = []
                if updates:
                    now = datetime.now()
                    for uc_id, delta, curr_bytes in updates:
                        uc = await db.get(models.UserConnection, uc_id)
                        if uc:
                            uc.last_bytes = curr_bytes
                            uid = uc.user_id
                            u = await db.get(models.User, uid)
                            if u:
                                strategy = u.traffic_reset_strategy or 'never'
                                last_reset_iso = u.last_reset_at
                                reset_needed = False
                                if strategy != 'never' and last_reset_iso:
                                    try:
                                        if strategy == 'daily':
                                            reset_needed = now.date() > last_reset_iso.date()
                                        elif strategy == 'weekly':
                                            reset_needed = now.isocalendar()[1] != last_reset_iso.isocalendar()[1] or now.year != last_reset_iso.year
                                        elif strategy == 'monthly':
                                            reset_needed = now.month != last_reset_iso.month or now.year != last_reset_iso.year
                                    except: pass
                                    
                                if reset_needed:
                                    u.traffic_used = 0
                                    u.last_reset_at = now
                                    
                                u.traffic_used = (u.traffic_used or 0) + delta
                                u.traffic_total = (u.traffic_total or 0) + delta
                                
                                limit = u.traffic_limit or 0
                                if limit > 0 and u.traffic_used >= limit and u.enabled:
                                    if uid not in to_disable_uids:
                                        to_disable_uids.append(uid)
                                        
                                if u.expiration_date and u.enabled:
                                    try:
                                        if now > u.expiration_date:
                                            if uid not in to_disable_uids:
                                                to_disable_uids.append(uid)
                                    except: pass
                    await db.commit()
                    
                if to_disable_uids:
                    await perform_mass_operations(db, toggle_uids=[(uid, False) for uid in to_disable_uids])
                    
                logger.info("Starting background Remnawave sync...")
                count, msg = await sync_users_with_remnawave(db)
                logger.info(f"Background Remnawave sync finished: {count} users updated. {msg}")

        except Exception as e:
            logger.error(f"Error in periodic_background_tasks: {e}")
            
        await asyncio.sleep(600)


# ======================== ROUTERS REGISTRATION ========================

from routers import auth, templates as templates_router, servers as servers_router, protocols as protocols_router, connections as connections_router, users as users_router, sharing as sharing_router, settings as settings_router, api_tokens as api_tokens_router

app.include_router(auth.router)
app.include_router(templates_router.router)
app.include_router(servers_router.router)
app.include_router(protocols_router.router)
app.include_router(connections_router.router)
app.include_router(users_router.router)
app.include_router(sharing_router.router)
app.include_router(settings_router.router)
app.include_router(api_tokens_router.router)


# ======================== PAGE ROUTES ========================

login_page = templates_router.login_page

set_lang = templates_router.set_lang
logout = templates_router.logout
index = templates_router.index
server_detail = templates_router.server_detail
users_page = templates_router.users_page
my_connections_page = templates_router.my_connections_page




# ======================== AUTH API ========================

api_captcha = auth.api_captcha
api_login = auth.api_login
LoginRequest = auth.LoginRequest






AddServerRequest = servers_router.AddServerRequest
EditServerRequest = servers_router.EditServerRequest
ReorderServersRequest = servers_router.ReorderServersRequest

api_add_server = servers_router.api_add_server
api_edit_server = servers_router.api_edit_server
api_server_ping = servers_router.api_server_ping
api_reset_server_fingerprint = servers_router.api_reset_server_fingerprint
api_reorder_servers = servers_router.api_reorder_servers
api_delete_server = servers_router.api_delete_server
api_reboot_server = servers_router.api_reboot_server
api_clear_server = servers_router.api_clear_server
api_server_stats = servers_router.api_server_stats
InstallProtocolRequest = protocols_router.InstallProtocolRequest
Socks5SettingsRequest = protocols_router.Socks5SettingsRequest
ProtocolRequest = protocols_router.ProtocolRequest
ServerConfigSaveRequest = protocols_router.ServerConfigSaveRequest
NginxSiteSaveRequest = protocols_router.NginxSiteSaveRequest
BackupDownloadRequest = protocols_router.BackupDownloadRequest

api_install_protocol = protocols_router.api_install_protocol
api_socks5_get_credentials = protocols_router.api_socks5_get_credentials
api_socks5_update_credentials = protocols_router.api_socks5_update_credentials
api_uninstall_protocol = protocols_router.api_uninstall_protocol
api_protocol_backups_list = protocols_router.api_protocol_backups_list
api_protocol_backup_create = protocols_router.api_protocol_backup_create
api_protocol_backup_download = protocols_router.api_protocol_backup_download
api_container_toggle = protocols_router.api_container_toggle
api_server_config = protocols_router.api_server_config
api_server_config_save = protocols_router.api_server_config_save
api_nginx_site_get = protocols_router.api_nginx_site_get
api_nginx_site_save = protocols_router.api_nginx_site_save


AddConnectionRequest = connections_router.AddConnectionRequest
EditConnectionRequest = connections_router.EditConnectionRequest
ConnectionActionRequest = connections_router.ConnectionActionRequest
ToggleConnectionRequest = connections_router.ToggleConnectionRequest

api_get_connections = connections_router.api_get_connections
api_add_connection = connections_router.api_add_connection
api_remove_connection = connections_router.api_remove_connection
api_edit_connection = connections_router.api_edit_connection
api_get_connection_config = connections_router.api_get_connection_config
api_toggle_connection = connections_router.api_toggle_connection




AddUserRequest = users_router.AddUserRequest
UpdateUserRequest = users_router.UpdateUserRequest
ToggleUserRequest = users_router.ToggleUserRequest
AddUserConnectionRequest = users_router.AddUserConnectionRequest

api_list_users = users_router.api_list_users
api_add_user = users_router.api_add_user
api_update_user = users_router.api_update_user
api_delete_user = users_router.api_delete_user
api_toggle_user = users_router.api_toggle_user
api_add_user_connection = users_router.api_add_user_connection
api_get_user_connections = users_router.api_get_user_connections
api_my_connections = users_router.api_my_connections




ShareSetupRequest = sharing_router.ShareSetupRequest
ShareAuthRequest = sharing_router.ShareAuthRequest

api_user_share_setup = sharing_router.api_user_share_setup
share_page = sharing_router.share_page
api_share_auth = sharing_router.api_share_auth
api_share_connections = sharing_router.api_share_connections
api_share_config = sharing_router.api_share_config
api_my_connection_config = sharing_router.api_my_connection_config




TunnelStartRequest = settings_router.TunnelStartRequest
AppearanceSettings = settings_router.AppearanceSettings
SyncSettings = settings_router.SyncSettings
CaptchaSettings = settings_router.CaptchaSettings
TelegramSettings = settings_router.TelegramSettings
SSLSettings = settings_router.SSLSettings
SaveSettingsRequest = settings_router.SaveSettingsRequest
CreateApiTokenRequest = api_tokens_router.CreateApiTokenRequest

settings_page = settings_router.settings_page
api_get_settings = settings_router.api_get_settings
api_tunnels_status = settings_router.api_tunnels_status
api_tunnel_install = settings_router.api_tunnel_install
api_tunnel_start = settings_router.api_tunnel_start
api_tunnel_stop = settings_router.api_tunnel_stop
api_tunnel_delete = settings_router.api_tunnel_delete
api_warp_connect = settings_router.api_warp_connect
api_warp_disconnect = settings_router.api_warp_disconnect
save_settings = settings_router.save_settings
api_telegram_toggle = settings_router.api_telegram_toggle
api_sync_now = settings_router.api_sync_now
api_sync_delete = settings_router.api_sync_delete
api_get_server_clients = settings_router.api_get_server_clients
api_list_tokens = api_tokens_router.api_list_tokens
api_create_token = api_tokens_router.api_create_token
api_revoke_token = api_tokens_router.api_revoke_token
api_backup_download = settings_router.api_backup_download
api_backup_restore = settings_router.api_backup_restore




@asynccontextmanager
async def _app_lifespan(app):
    """Modern ASGI lifespan — replaces the deprecated @app.on_event('startup')."""
    # ── startup ──
    await _startup()

    # ── running ──
    yield

    # ── graceful shutdown ──
    logger.info("Shutting down gracefully...")
    # Cancel background tasks so they don't keep the event loop alive.
    for task_ref in ["_bg_traffic", "_bg_backup"]:
        t = globals().get(task_ref)
        if t and not t.done():
            t.cancel()
            try:
                await t
            except asyncio.CancelledError:
                pass
    for provider in list(TUNNEL_RUNTIMES.keys()):
        try:
            await asyncio.to_thread(stop_tunnel, provider)
        except Exception as e:
            logger.warning("Error stopping %s tunnel: %s", provider, e)
    try:
        await tg_bot.stop_bot()
    except Exception as e:
        logger.warning("Error stopping Telegram bot: %s", e)


app.router.lifespan_context = _app_lifespan


if __name__ == '__main__':
    async def _get_ssl_conf():
        try:
            from database.session import async_session
            from database.repositories import SettingRepository
            async with asyncio.timeout(3.0):
                async with async_session() as db:
                    settings = await SettingRepository.get(db, 'settings', default={})
                    return settings.get('ssl', {})
        except Exception as e:
            logger.warning("Could not fetch SSL config from DB (%s), proceeding with default SSL settings.", e)
            return {}

    ssl_conf = asyncio.run(_get_ssl_conf())
    
    cert_file = ssl_conf.get('cert_path')
    key_file = ssl_conf.get('key_path')
    
    # If text is provided, create temporary files
    temp_dir = os.path.join(os.getcwd(), 'ssl_temp')
    if ssl_conf.get('enabled'):
        if ssl_conf.get('cert_text') or ssl_conf.get('key_text'):
            if not os.path.exists(temp_dir):
                os.makedirs(temp_dir)
            
            if ssl_conf.get('cert_text'):
                cert_file = os.path.join(temp_dir, 'cert.pem')
                with open(cert_file, 'w') as f:
                    f.write(ssl_conf['cert_text'].strip() + '\n')
            
            if ssl_conf.get('key_text'):
                key_file = os.path.join(temp_dir, 'key.pem')
                with open(key_file, 'w') as f:
                    f.write(ssl_conf['key_text'].strip() + '\n')

    uvicorn_kwargs = {
        "app": app,
        "host": "0.0.0.0",
        "port": ssl_conf.get('panel_port', 5000)
    }
    
    if ssl_conf.get('enabled') and cert_file and key_file:
        if os.path.exists(cert_file) and os.path.exists(key_file):
            logger.info(f"Starting panel with HTTPS enabled on domain: {ssl_conf.get('domain')} at port {uvicorn_kwargs['port']}")
            uvicorn_kwargs["ssl_certfile"] = cert_file
            uvicorn_kwargs["ssl_keyfile"] = key_file
        else:
            logger.error("SSL certificates not found at specified paths. Starting with HTTP.")

    uvicorn.run(**uvicorn_kwargs)
