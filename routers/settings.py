import os
import json
import asyncio
from typing import Optional
from fastapi import APIRouter, Request, UploadFile, File
from fastapi.responses import JSONResponse, HTMLResponse, RedirectResponse, FileResponse
from pydantic import BaseModel

import config
import app as app_module
import telegram_bot as tg_bot

router = APIRouter(tags=["Settings", "System Templates"])


class TunnelStartRequest(BaseModel):
    authtoken: Optional[str] = None


class AppearanceSettings(BaseModel):
    title: Optional[str] = "Amnezia Web Panel"
    theme: Optional[str] = "dark"
    accent: Optional[str] = "#5865F2"


class SyncSettings(BaseModel):
    remnawave_enabled: Optional[bool] = False
    remnawave_url: Optional[str] = ""
    remnawave_api_token: Optional[str] = ""
    remnawave_server_id: Optional[int] = 0
    remnawave_sync_interval: Optional[int] = 10
    remnawave_sync_users: Optional[bool] = False
    remnawave_sync_servers: Optional[bool] = False


class CaptchaSettings(BaseModel):
    enabled: Optional[bool] = False


class TelegramSettings(BaseModel):
    enabled: Optional[bool] = False
    token: Optional[str] = ""
    admin_ids: Optional[str] = ""


class SSLSettings(BaseModel):
    enabled: Optional[bool] = False
    domain: Optional[str] = ""
    cert_path: Optional[str] = ""
    key_path: Optional[str] = ""
    cert_text: Optional[str] = ""
    key_text: Optional[str] = ""
    panel_port: Optional[int] = 5000


class SaveSettingsRequest(BaseModel):
    appearance: AppearanceSettings
    sync: SyncSettings
    captcha: CaptchaSettings
    telegram: TelegramSettings
    ssl: SSLSettings


@router.get('/settings', response_class=HTMLResponse, tags=["System Templates"])
async def settings_page(request: Request):
    user = app_module._check_admin(request)
    if not user:
        return RedirectResponse('/login')
    data = app_module.load_data()
    return app_module.tpl(request, 'settings.html', settings=data.get('settings', {}), servers=data.get('servers', []), current_version=app_module.CURRENT_VERSION)


@router.get('/api/settings', tags=["Settings"])
async def api_get_settings(request: Request):
    if not app_module._check_admin(request):
        return JSONResponse({'error': 'Forbidden'}, status_code=403)
    data = app_module.load_data()
    return data.get('settings', {})


@router.get('/api/settings/tunnels/status', tags=["Settings"])
async def api_tunnels_status(request: Request):
    if not app_module._check_admin(request):
        return JSONResponse({'error': 'Forbidden'}, status_code=403)
    return {
        'local_server': app_module.get_panel_local_url(request),
        'cloudflare': app_module.get_tunnel_status('cloudflare'),
        'ngrok': app_module.get_tunnel_status('ngrok'),
        'warp': app_module.get_warp_status(),
    }


@router.post('/api/settings/tunnels/{provider}/install', tags=["Settings"])
async def api_tunnel_install(request: Request, provider: str):
    if not app_module._check_admin(request):
        return JSONResponse({'error': 'Forbidden'}, status_code=403)
    if provider not in app_module.TUNNEL_RUNTIMES:
        return JSONResponse({'error': 'Unsupported tunnel provider'}, status_code=400)
    try:
        await asyncio.to_thread(app_module.install_tunnel_binary, provider)
        return app_module.get_tunnel_status(provider)
    except Exception as e:
        app_module.logger.exception(f"Error installing {provider} tunnel")
        return JSONResponse({'error': str(e)}, status_code=500)


@router.post('/api/settings/tunnels/{provider}/start', tags=["Settings"])
async def api_tunnel_start(request: Request, provider: str, payload: TunnelStartRequest = TunnelStartRequest()):
    if not app_module._check_admin(request):
        return JSONResponse({'error': 'Forbidden'}, status_code=403)
    if provider not in app_module.TUNNEL_RUNTIMES:
        return JSONResponse({'error': 'Unsupported tunnel provider'}, status_code=400)
    try:
        local_url = app_module.get_panel_tunnel_target_url()
        await asyncio.to_thread(app_module.start_tunnel, provider, local_url, payload.authtoken or '')
        status = await app_module.wait_for_tunnel_url(provider)
        if not status.get('running'):
            return JSONResponse({'error': status.get('last_error') or 'Tunnel process stopped'}, status_code=500)
        return status
    except Exception as e:
        app_module.logger.exception(f"Error starting {provider} tunnel")
        return JSONResponse({'error': str(e)}, status_code=500)


@router.post('/api/settings/tunnels/{provider}/stop', tags=["Settings"])
async def api_tunnel_stop(request: Request, provider: str):
    if not app_module._check_admin(request):
        return JSONResponse({'error': 'Forbidden'}, status_code=403)
    if provider not in app_module.TUNNEL_RUNTIMES:
        return JSONResponse({'error': 'Unsupported tunnel provider'}, status_code=400)
    try:
        await asyncio.to_thread(app_module.stop_tunnel, provider)
        return app_module.get_tunnel_status(provider)
    except Exception as e:
        app_module.logger.exception(f"Error stopping {provider} tunnel")
        return JSONResponse({'error': str(e)}, status_code=500)


@router.delete('/api/settings/tunnels/{provider}', tags=["Settings"])
async def api_tunnel_delete(request: Request, provider: str):
    if not app_module._check_admin(request):
        return JSONResponse({'error': 'Forbidden'}, status_code=403)
    if provider not in app_module.TUNNEL_RUNTIMES:
        return JSONResponse({'error': 'Unsupported tunnel provider'}, status_code=400)
    try:
        await asyncio.to_thread(app_module.delete_tunnel_binary, provider)
        return app_module.get_tunnel_status(provider)
    except Exception as e:
        app_module.logger.exception(f"Error deleting {provider} tunnel")
        return JSONResponse({'error': str(e)}, status_code=500)


@router.post('/api/settings/warp/connect', tags=["Settings"])
async def api_warp_connect(request: Request):
    if not app_module._check_admin(request):
        return JSONResponse({'error': 'Forbidden'}, status_code=403)
    try:
        return await asyncio.to_thread(app_module.enable_warp)
    except Exception as e:
        app_module.logger.exception("Error connecting Cloudflare WARP")
        return JSONResponse({'error': str(e)}, status_code=500)


@router.post('/api/settings/warp/disconnect', tags=["Settings"])
async def api_warp_disconnect(request: Request):
    if not app_module._check_admin(request):
        return JSONResponse({'error': 'Forbidden'}, status_code=403)
    try:
        return await asyncio.to_thread(app_module.disable_warp)
    except Exception as e:
        app_module.logger.exception("Error disconnecting Cloudflare WARP")
        return JSONResponse({'error': str(e)}, status_code=500)


@router.post('/api/settings/save', tags=["Settings"])
async def save_settings(request: Request, payload: SaveSettingsRequest):
    if not app_module._check_admin(request):
        return JSONResponse({'error': 'Forbidden'}, status_code=403)
    data = app_module.load_data()
    data['settings']['appearance'] = payload.appearance.dict()
    data['settings']['sync'] = payload.sync.dict()
    data['settings']['captcha'] = payload.captcha.dict()
    data['settings']['telegram'] = payload.telegram.dict()
    data['settings']['ssl'] = payload.ssl.dict()
    app_module.save_data(data)
    app_module.logger.info("Settings saved (including captcha and telegram)")

    tg_cfg = payload.telegram
    if tg_cfg.enabled and tg_cfg.token:
        if not tg_bot.is_running():
            app_module.logger.info("Starting Telegram bot (settings save)...")
            tg_bot.launch_bot(tg_cfg.token, app_module.load_data, app_module.generate_vpn_link, app_module.save_data)
    else:
        if tg_bot.is_running():
            app_module.logger.info("Stopping Telegram bot (settings save)...")
            asyncio.create_task(tg_bot.stop_bot())

    return {"status": "success", "bot_running": tg_bot.is_running()}


@router.post('/api/settings/telegram/toggle', tags=["Settings"])
async def api_telegram_toggle(request: Request):
    if not app_module._check_admin(request):
        return JSONResponse({'error': 'Forbidden'}, status_code=403)
    data = app_module.load_data()
    tg_cfg = data.get('settings', {}).get('telegram', {})
    token = tg_cfg.get('token', '')
    if not token:
        return JSONResponse({'error': 'Telegram token not set in settings'}, status_code=400)

    if tg_bot.is_running():
        await tg_bot.stop_bot()
        tg_cfg['enabled'] = False
        data['settings']['telegram'] = tg_cfg
        app_module.save_data(data)
        return {'status': 'stopped', 'bot_running': False}
    else:
        tg_bot.launch_bot(token, app_module.load_data, app_module.generate_vpn_link, app_module.save_data)
        tg_cfg['enabled'] = True
        data['settings']['telegram'] = tg_cfg
        app_module.save_data(data)
        return {'status': 'started', 'bot_running': True}


@router.post('/api/settings/sync_now', tags=["Settings"])
async def api_sync_now(request: Request):
    if not app_module._check_admin(request):
        return JSONResponse({'error': 'Forbidden'}, status_code=403)
    data = app_module.load_data()
    count, msg = await app_module.sync_users_with_remnawave(data)
    return {'status': 'success', 'count': count, 'message': msg}


@router.post('/api/settings/sync_delete', tags=["Settings"])
async def api_sync_delete(request: Request):
    if not app_module._check_admin(request):
        return JSONResponse({'error': 'Forbidden'}, status_code=403)
    data = app_module.load_data()
    to_delete_ids = [u['id'] for u in data['users'] if u.get('remnawave_uuid')]
    if to_delete_ids:
        await app_module.perform_mass_operations(delete_uids=to_delete_ids)
    return {'status': 'success', 'count': len(to_delete_ids)}


@router.get('/api/servers/{server_id}/{protocol}/clients', tags=["Connections"])
async def api_get_server_clients(request: Request, server_id: int, protocol: str):
    if not app_module._check_admin(request):
        return JSONResponse({'error': 'Forbidden'}, status_code=403)
    try:
        data = app_module.load_data()
        if server_id >= len(data['servers']):
            return JSONResponse({'error': 'Server not found'}, status_code=404)
        server = data['servers'][server_id]
        ssh = app_module.get_ssh(server)
        ssh.connect()
        manager = app_module.get_protocol_manager(ssh, protocol)
        clients = manager.get_clients(protocol)
        ssh.disconnect()
        
        assigned_ids = {c['client_id'] for c in data.get('user_connections', []) if c['server_id'] == server_id and c['protocol'] == protocol}
        
        filtered = []
        for c in clients:
            if c['clientId'] not in assigned_ids:
                filtered.append({
                    'id': c['clientId'],
                    'name': c.get('userData', {}).get('clientName', 'Unnamed')
                })
        
        return {'clients': filtered}
    except Exception as e:
        app_module.logger.exception("Error getting server clients")
        return JSONResponse({'error': str(e)}, status_code=500)


@router.get('/api/settings/backup/download', tags=["Settings"])
async def api_backup_download(request: Request):
    if not app_module._check_admin(request):
        return JSONResponse({'error': 'Forbidden'}, status_code=403)
    if not os.path.exists(app_module.DATA_FILE):
        return JSONResponse({'error': 'Data file not found'}, status_code=404)
    return FileResponse(app_module.DATA_FILE, media_type='application/json', filename='data.json')


@router.post('/api/settings/backup/restore', tags=["Settings"])
async def api_backup_restore(request: Request, file: UploadFile = File(...)):
    if not app_module._check_admin(request):
        return JSONResponse({'error': 'Forbidden'}, status_code=403)
    try:
        content = await file.read()
        if not content:
            return JSONResponse({'error': 'Empty file'}, status_code=400)
        
        try:
            backup_data = json.loads(content)
        except json.JSONDecodeError:
            return JSONResponse({'error': 'Invalid JSON format'}, status_code=400)

        required_keys = ['servers', 'users']
        missing = [k for k in required_keys if k not in backup_data]
        if missing:
            return JSONResponse({'error': f'Invalid structure. Missing keys: {", ".join(missing)}'}, status_code=400)

        if not isinstance(backup_data['servers'], list) or not isinstance(backup_data['users'], list):
            return JSONResponse({'error': 'Invalid structure: servers and users must be lists'}, status_code=400)

        async with app_module.DATA_LOCK:
            app_module.save_data(backup_data)
        
        return {'status': 'success'}
    except Exception as e:
        app_module.logger.exception("Error during restore")
        return JSONResponse({'error': str(e)}, status_code=500)
