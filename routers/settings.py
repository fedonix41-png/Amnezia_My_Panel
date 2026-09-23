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

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from sqlalchemy.orm.attributes import flag_modified
from database.session import get_db, engine, async_session
from database import models


class TunnelStartRequest(BaseModel):
    authtoken: Optional[str] = None


class AppearanceSettings(BaseModel):
    title: Optional[str] = "Amnezia Web Panel"
    theme: Optional[str] = "dark"
    accent: Optional[str] = "#5865F2"
    logo: Optional[str] = ""
    subtitle: Optional[str] = ""


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


router = APIRouter(tags=["Settings", "System Templates"])


@router.get('/settings', response_class=HTMLResponse, tags=["System Templates"])
async def settings_page(request: Request, db: AsyncSession = Depends(get_db)):
    user = await app_module._check_admin(db, request)
    if not user:
        return RedirectResponse('/login')
        
    stmt_settings = select(models.Setting).where(models.Setting.key == 'settings')
    res_settings = await db.execute(stmt_settings)
    settings_obj = res_settings.scalar_one_or_none()
    settings_dict = settings_obj.value if settings_obj else {}
    
    stmt_servers = select(models.Server).order_by(models.Server.display_order.asc(), models.Server.id.asc())
    res_servers = await db.execute(stmt_servers)
    servers = [{"id": s.id, "name": s.name, "host": s.host} for s in res_servers.scalars().all()]
    
    return await app_module.tpl(db, request, 'settings.html', settings=settings_dict, servers=servers, current_version=app_module.CURRENT_VERSION)


@router.get('/api/settings', tags=["Settings"])
async def api_get_settings(request: Request, db: AsyncSession = Depends(get_db)):
    if not await app_module._check_admin(db, request):
        return JSONResponse({'error': 'Forbidden'}, status_code=403)
        
    stmt_settings = select(models.Setting).where(models.Setting.key == 'settings')
    res_settings = await db.execute(stmt_settings)
    settings_obj = res_settings.scalar_one_or_none()
    return settings_obj.value if settings_obj else {}


@router.get('/api/settings/tunnels/status', tags=["Settings"])
async def api_tunnels_status(request: Request, db: AsyncSession = Depends(get_db)):
    if not await app_module._check_admin(db, request):
        return JSONResponse({'error': 'Forbidden'}, status_code=403)
    return {
        'local_server': await app_module.get_panel_local_url(db, request),
        'cloudflare': app_module.get_tunnel_status('cloudflare'),
        'ngrok': app_module.get_tunnel_status('ngrok'),
        'warp': app_module.get_warp_status(),
    }


@router.post('/api/settings/tunnels/{provider}/install', tags=["Settings"])
async def api_tunnel_install(request: Request, provider: str, db: AsyncSession = Depends(get_db)):
    if not await app_module._check_admin(db, request):
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
async def api_tunnel_start(request: Request, provider: str, payload: TunnelStartRequest = TunnelStartRequest(), db: AsyncSession = Depends(get_db)):
    if not await app_module._check_admin(db, request):
        return JSONResponse({'error': 'Forbidden'}, status_code=403)
    if provider not in app_module.TUNNEL_RUNTIMES:
        return JSONResponse({'error': 'Unsupported tunnel provider'}, status_code=400)
    try:
        local_url = await app_module.get_panel_tunnel_target_url(db)
        await asyncio.to_thread(app_module.start_tunnel, provider, local_url, payload.authtoken or '')
        status = await app_module.wait_for_tunnel_url(provider)
        if not status.get('running'):
            return JSONResponse({'error': status.get('last_error') or 'Tunnel process stopped'}, status_code=500)
        return status
    except Exception as e:
        app_module.logger.exception(f"Error starting {provider} tunnel")
        return JSONResponse({'error': str(e)}, status_code=500)


@router.post('/api/settings/tunnels/{provider}/stop', tags=["Settings"])
async def api_tunnel_stop(request: Request, provider: str, db: AsyncSession = Depends(get_db)):
    if not await app_module._check_admin(db, request):
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
async def api_tunnel_delete(request: Request, provider: str, db: AsyncSession = Depends(get_db)):
    if not await app_module._check_admin(db, request):
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
async def api_warp_connect(request: Request, db: AsyncSession = Depends(get_db)):
    if not await app_module._check_admin(db, request):
        return JSONResponse({'error': 'Forbidden'}, status_code=403)
    try:
        return await asyncio.to_thread(app_module.enable_warp)
    except Exception as e:
        app_module.logger.exception("Error connecting Cloudflare WARP")
        return JSONResponse({'error': str(e)}, status_code=500)


@router.post('/api/settings/warp/disconnect', tags=["Settings"])
async def api_warp_disconnect(request: Request, db: AsyncSession = Depends(get_db)):
    if not await app_module._check_admin(db, request):
        return JSONResponse({'error': 'Forbidden'}, status_code=403)
    try:
        return await asyncio.to_thread(app_module.disable_warp)
    except Exception as e:
        app_module.logger.exception("Error disconnecting Cloudflare WARP")
        return JSONResponse({'error': str(e)}, status_code=500)


@router.post('/api/settings/save', tags=["Settings"])
async def save_settings(request: Request, payload: SaveSettingsRequest, db: AsyncSession = Depends(get_db)):
    if not await app_module._check_admin(db, request):
        return JSONResponse({'error': 'Forbidden'}, status_code=403)
        
    stmt_settings = select(models.Setting).where(models.Setting.key == 'settings')
    res_settings = await db.execute(stmt_settings)
    settings_obj = res_settings.scalar_one_or_none()
    if not settings_obj:
        settings_obj = models.Setting(key='settings', value={})
        db.add(settings_obj)
        
    settings_dict = settings_obj.value
    settings_dict['appearance'] = payload.appearance.model_dump()
    settings_dict['sync'] = payload.sync.model_dump()
    settings_dict['captcha'] = payload.captcha.model_dump()
    settings_dict['telegram'] = payload.telegram.model_dump()
    settings_dict['ssl'] = payload.ssl.model_dump()
    
    flag_modified(settings_obj, "value")
    await db.commit()
    app_module.logger.info("Settings saved (including captcha and telegram)")

    tg_cfg = payload.telegram
    if tg_cfg.enabled and tg_cfg.token:
        if not tg_bot.is_running():
            app_module.logger.info("Starting Telegram bot (settings save)...")
            tg_bot.launch_bot(tg_cfg.token, app_module.generate_vpn_link)
    else:
        if tg_bot.is_running():
            app_module.logger.info("Stopping Telegram bot (settings save)...")
            asyncio.create_task(tg_bot.stop_bot())

    return {"status": "success", "bot_running": tg_bot.is_running()}


@router.post('/api/settings/telegram/toggle', tags=["Settings"])
async def api_telegram_toggle(request: Request, db: AsyncSession = Depends(get_db)):
    if not await app_module._check_admin(db, request):
        return JSONResponse({'error': 'Forbidden'}, status_code=403)
        
    stmt_settings = select(models.Setting).where(models.Setting.key == 'settings')
    res_settings = await db.execute(stmt_settings)
    settings_obj = res_settings.scalar_one_or_none()
    if not settings_obj:
        return JSONResponse({'error': 'Settings not found'}, status_code=400)
        
    settings_dict = settings_obj.value
    tg_cfg = settings_dict.get('telegram', {})
    token = tg_cfg.get('token', '')
    if not token:
        return JSONResponse({'error': 'Telegram token not set in settings'}, status_code=400)

    if tg_bot.is_running():
        await tg_bot.stop_bot()
        tg_cfg['enabled'] = False
        settings_dict['telegram'] = tg_cfg
        flag_modified(settings_obj, "value")
        await db.commit()
        return {'status': 'stopped', 'bot_running': False}
    else:
        tg_bot.launch_bot(token, app_module.generate_vpn_link)
        tg_cfg['enabled'] = True
        settings_dict['telegram'] = tg_cfg
        flag_modified(settings_obj, "value")
        await db.commit()
        return {'status': 'started', 'bot_running': True}


@router.post('/api/settings/sync_now', tags=["Settings"])
async def api_sync_now(request: Request, db: AsyncSession = Depends(get_db)):
    if not await app_module._check_admin(db, request):
        return JSONResponse({'error': 'Forbidden'}, status_code=403)
    count, msg = await app_module.sync_users_with_remnawave(db)
    return {'status': 'success', 'count': count, 'message': msg}


@router.post('/api/settings/sync_delete', tags=["Settings"])
async def api_sync_delete(request: Request, db: AsyncSession = Depends(get_db)):
    if not await app_module._check_admin(db, request):
        return JSONResponse({'error': 'Forbidden'}, status_code=403)
        
    stmt = select(models.User.id).where(models.User.remnawave_uuid.is_not(None))
    res = await db.execute(stmt)
    to_delete_ids = list(res.scalars().all())
    
    if to_delete_ids:
        await app_module.perform_mass_operations(db, delete_uids=to_delete_ids)
    return {'status': 'success', 'count': len(to_delete_ids)}


@router.get('/api/servers/{server_id}/{protocol}/clients', tags=["Connections"])
async def api_get_server_clients(request: Request, server_id: int, protocol: str, db: AsyncSession = Depends(get_db)):
    if not await app_module._check_admin(db, request):
        return JSONResponse({'error': 'Forbidden'}, status_code=403)
    try:
        server = await db.get(models.Server, server_id)
        if not server:
            return JSONResponse({'error': 'Server not found'}, status_code=404)
            
        ssh = app_module.get_ssh(server, db)
        try:
            await asyncio.to_thread(ssh.connect)
            manager = app_module.get_protocol_manager(ssh, protocol)
            clients = await asyncio.to_thread(app_module._manager_call, manager, 'get_clients', protocol)
        finally:
            try:
                await asyncio.to_thread(ssh.disconnect)
            except Exception:
                pass

        
        stmt = select(models.Connection.client_id).where(
            models.Connection.server_id == server_id,
            models.Connection.protocol == protocol
        )
        res = await db.execute(stmt)
        assigned_ids = set(res.scalars().all())
        
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
async def api_backup_download(request: Request, db: AsyncSession = Depends(get_db)):
    if not await app_module._check_admin(db, request):
        return JSONResponse({'error': 'Forbidden'}, status_code=403)
    
    export_data = {
        'servers': [s.to_dict() for s in (await db.execute(select(models.Server))).scalars().all()],
        'users': [u.to_dict() for u in (await db.execute(select(models.User))).scalars().all()],
        'connections': [c.to_dict() for c in (await db.execute(select(models.Connection))).scalars().all()],
        'settings': [s.to_dict() for s in (await db.execute(select(models.Setting))).scalars().all()],
        'server_protocols': [p.to_dict() for p in (await db.execute(select(models.ServerProtocol))).scalars().all()]
    }
    for k in export_data:
        for item in export_data[k]:
            item.pop('_sa_instance_state', None)
            
    for user in export_data['users']:
        user.pop('password_hash', None)
        user.pop('share_password_hash', None)
        user.pop('share_token', None)
        
    for server in export_data['servers']:
        server.pop('password', None)
        server.pop('private_key', None)
    
    import tempfile
    with tempfile.NamedTemporaryFile(mode='w', suffix=".json", delete=False) as f:
        json.dump(export_data, f, default=str)
        tmp_file = f.name
    
    return FileResponse(tmp_file, media_type='application/json', filename='awp_backup.json')


@router.post('/api/settings/backup/restore', tags=["Settings"])
async def api_backup_restore(request: Request, file: UploadFile = File(...), db: AsyncSession = Depends(get_db)):
    if not await app_module._check_admin(db, request):
        return JSONResponse({'error': 'Forbidden'}, status_code=403)
    
    # We will ignore restoring for now to prevent breaking the db. 
    # Proper DB backups should be handled by Postgres pg_dump and pg_restore.
    return JSONResponse({'error': 'Restoring from JSON is not supported in the database version. Use pg_restore.'}, status_code=400)
