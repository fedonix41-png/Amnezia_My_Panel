import secrets
from typing import Optional
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, HTMLResponse
from pydantic import BaseModel

import config
import app as app_module

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from sqlalchemy.orm import selectinload
from database.session import get_db
from database import models

router = APIRouter(tags=["Sharing", "Self-service"])


class ShareSetupRequest(BaseModel):
    enabled: bool = True
    password: Optional[str] = None


class ShareAuthRequest(BaseModel):
    password: str = ''


@router.post('/api/users/{user_id}/share/setup', tags=["Users"])
async def api_user_share_setup(user_id: str, req: ShareSetupRequest, request: Request, db: AsyncSession = Depends(get_db)):
    if not await app_module._check_admin(db, request):
        return JSONResponse({'error': 'Forbidden'}, status_code=403)
    user = await db.get(models.User, user_id)
    if not user:
        return JSONResponse({'error': 'User not found'}, status_code=404)
    
    user.share_enabled = req.enabled
    if not user.share_token:
        user.share_token = secrets.token_urlsafe(16)
    if req.password:
        user.share_password_hash = app_module.hash_password(req.password)
    elif req.password == "":
        user.share_password_hash = None
        
    await db.commit()
    return {'status': 'success', 'share_token': user.share_token}


@router.get('/share/{token}', response_class=HTMLResponse, tags=["System Templates"])
@app_module.limiter.limit(config.SHARE_RATE_LIMIT)
async def share_page(token: str, request: Request, db: AsyncSession = Depends(get_db)):
    stmt = select(models.User).where(models.User.share_token == token)
    res = await db.execute(stmt)
    user = res.scalar_one_or_none()
    
    if not user or not user.share_enabled:
        lang = request.cookies.get('lang', 'ru')
        return HTMLResponse(f"<h1>{app_module._t('share_not_found', lang)}</h1><p>{app_module._t('share_not_found_desc', lang)}</p>", status_code=404)
    
    auth_session_key = f'share_auth_{token}'
    need_password = bool(user.share_password_hash) and not request.session.get(auth_session_key)
    
    user_dict = {
        'id': user.id,
        'username': user.username,
        'share_token': user.share_token,
        'share_enabled': user.share_enabled,
        'share_password_hash': user.share_password_hash
    }
    return await app_module.tpl(db, request, 'user_share.html', 
               share_user=user_dict, 
               need_password=need_password, 
               token=token)


@router.post('/api/share/{token}/auth')
@app_module.limiter.limit(config.SHARE_RATE_LIMIT)
async def api_share_auth(token: str, req: ShareAuthRequest, request: Request, db: AsyncSession = Depends(get_db)):
    stmt = select(models.User).where(models.User.share_token == token)
    res = await db.execute(stmt)
    user = res.scalar_one_or_none()
    
    if not user or not user.share_enabled:
        return JSONResponse({'error': 'Link expired or disabled'}, status_code=404)
    
    if app_module.verify_password(req.password, user.share_password_hash or ''):
        request.session[f'share_auth_{token}'] = True
        return {'status': 'success'}
    else:
        lang = request.cookies.get('lang', 'ru')
        return JSONResponse({'error': app_module._t('wrong_share_password', lang)}, status_code=401)


@router.get('/api/share/{token}/connections')
@app_module.limiter.limit(config.SHARE_RATE_LIMIT)
async def api_share_connections(token: str, request: Request, db: AsyncSession = Depends(get_db)):
    stmt = select(models.User).where(models.User.share_token == token)
    res = await db.execute(stmt)
    user = res.scalar_one_or_none()
    
    if not user or not user.share_enabled:
        return JSONResponse({'error': 'Forbidden'}, status_code=403)
    
    if user.share_password_hash:
        if not request.session.get(f'share_auth_{token}'):
            return JSONResponse({'error': 'Unauthorized'}, status_code=401)
            
    stmt_conns = select(models.Connection).where(models.Connection.user_id == user.id).options(selectinload(models.Connection.server))
    conns_res = await db.execute(stmt_conns)
    
    conns = []
    for c in conns_res.scalars().all():
        conns.append({
            'id': c.id,
            'user_id': c.user_id,
            'server_id': c.server_id,
            'protocol': c.protocol,
            'client_id': c.client_id,
            'name': c.name,
            'created_at': c.created_at,
            'server_name': c.server.name if c.server else 'Unknown'
        })
            
    return {'connections': conns, 'username': user.username}


@router.post('/api/share/{token}/config/{connection_id}')
@app_module.limiter.limit(config.SHARE_RATE_LIMIT)
async def api_share_config(token: str, connection_id: str, request: Request, db: AsyncSession = Depends(get_db)):
    stmt = select(models.User).where(models.User.share_token == token)
    res = await db.execute(stmt)
    user = res.scalar_one_or_none()
    
    if not user or not user.share_enabled:
        return JSONResponse({'error': 'Forbidden'}, status_code=403)
    
    if user.share_password_hash:
        if not request.session.get(f'share_auth_{token}'):
            return JSONResponse({'error': 'Unauthorized'}, status_code=401)
            
    stmt_conn = select(models.Connection).where(
        models.Connection.id == connection_id,
        models.Connection.user_id == user.id
    ).options(selectinload(models.Connection.server))
    
    conn_res = await db.execute(stmt_conn)
    conn = conn_res.scalar_one_or_none()
    
    if not conn:
        return JSONResponse({'error': 'Not found'}, status_code=404)
        
    try:
        server = conn.server
        if not server:
            return JSONResponse({'error': 'Server not found'}, status_code=404)
            
        stmt_proto = select(models.ServerProtocol).where(
            models.ServerProtocol.server_id == server.id,
            models.ServerProtocol.protocol_type == conn.protocol
        )
        proto_res = await db.execute(stmt_proto)
        proto_info = proto_res.scalar_one_or_none()
        port = proto_info.port if proto_info else '55424'
        
        ssh = app_module.get_ssh(server, db)
        try:
            await asyncio.to_thread(ssh.connect)
            manager = app_module.get_protocol_manager(ssh, conn.protocol)
            config_text = await asyncio.to_thread(app_module._manager_call, manager, 'get_client_config', conn.protocol, conn.client_id, server.host, port)
        finally:
            try:
                await asyncio.to_thread(ssh.disconnect)
            except Exception:
                pass
        vpn_link = app_module.generate_vpn_link(config_text) if config_text else ''
        qr_content = app_module.generate_qr_content(config_text) if config_text else ''
        return {'config': config_text, 'vpn_link': vpn_link, 'qr_content': qr_content}
    except Exception as e:
        app_module.logger.exception("Error getting shared config")
        return JSONResponse({'error': str(e)}, status_code=500)


@router.post('/api/my/connections/{connection_id}/config')
async def api_my_connection_config(request: Request, connection_id: str, db: AsyncSession = Depends(get_db)):
    user = await app_module.get_current_user(db, request)
    if not user:
        return JSONResponse({'error': 'Forbidden'}, status_code=403)
    try:
        stmt_conn = select(models.Connection).where(
            models.Connection.id == connection_id,
            models.Connection.user_id == user.id
        ).options(selectinload(models.Connection.server))
        
        conn_res = await db.execute(stmt_conn)
        conn = conn_res.scalar_one_or_none()
        
        if not conn:
            return JSONResponse({'error': 'Connection not found'}, status_code=404)
            
        server = conn.server
        if not server:
            return JSONResponse({'error': 'Server not found'}, status_code=404)
            
        stmt_proto = select(models.ServerProtocol).where(
            models.ServerProtocol.server_id == server.id,
            models.ServerProtocol.protocol_type == conn.protocol
        )
        proto_res = await db.execute(stmt_proto)
        proto_info = proto_res.scalar_one_or_none()
        port = proto_info.port if proto_info else '55424'
        
        ssh = app_module.get_ssh(server, db)
        try:
            await asyncio.to_thread(ssh.connect)
            manager = app_module.get_protocol_manager(ssh, conn.protocol)
            config_text = await asyncio.to_thread(app_module._manager_call, manager, 'get_client_config', conn.protocol, conn.client_id, server.host, port)
        finally:
            try:
                await asyncio.to_thread(ssh.disconnect)
            except Exception:
                pass
        vpn_link = app_module.generate_vpn_link(config_text) if config_text else ''
        qr_content = app_module.generate_qr_content(config_text) if config_text else ''
        return {'config': config_text, 'vpn_link': vpn_link, 'qr_content': qr_content}
    except Exception as e:
        app_module.logger.exception("Error getting my connection config")
        return JSONResponse({'error': str(e)}, status_code=500)
