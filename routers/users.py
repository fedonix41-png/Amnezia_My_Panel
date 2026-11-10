import uuid
import secrets
import asyncio
from datetime import datetime, timezone
from typing import Optional
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

import app as app_module
from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, or_, cast, String
from sqlalchemy.orm import selectinload
from database.session import get_db
from database import models

router = APIRouter(tags=["Users", "Self-service"])


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
    expiration_date: Optional[datetime] = None
    telemt_quota: Optional[str] = None
    telemt_max_ips: Optional[int] = None
    telemt_expiry: Optional[str] = None
    telemt_secret: Optional[str] = None
    telemt_ad_tag: Optional[str] = None
    telemt_max_conns: Optional[int] = None


class UpdateUserRequest(BaseModel):
    telegramId: Optional[str] = None
    email: Optional[str] = None
    description: Optional[str] = None
    traffic_limit: Optional[float] = None
    traffic_reset_strategy: Optional[str] = None
    expiration_date: Optional[datetime] = None
    password: Optional[str] = None


class ToggleUserRequest(BaseModel):
    enabled: bool = True


class AddUserConnectionRequest(BaseModel):
    server_id: int
    protocol: str = 'awg'
    name: str = 'Connection'
    client_id: Optional[str] = None
    telemt_quota: Optional[str] = None
    telemt_max_ips: Optional[int] = None
    telemt_expiry: Optional[str] = None
    telemt_secret: Optional[str] = None
    telemt_ad_tag: Optional[str] = None
    telemt_max_conns: Optional[int] = None


@router.get('/api/users', tags=["Users"])
async def api_list_users(request: Request, search: str = '', page: int = 1, size: int = 10, db: AsyncSession = Depends(get_db)):
    if not await app_module._check_admin(db, request):
        return JSONResponse({'error': 'Forbidden'}, status_code=403)
        
    stmt = select(models.User)
    if search:
        search_term = f"%{search.lower()}%"
        stmt = stmt.where(or_(
            func.lower(models.User.username).like(search_term),
            func.lower(models.User.email).like(search_term),
            func.lower(cast(models.User.telegramId, String)).like(search_term)
        ))
        
    total_res = await db.execute(select(func.count()).select_from(stmt.subquery()))
    total = total_res.scalar()
    
    stmt = stmt.offset((page - 1) * size).limit(size).options(selectinload(models.User.connections))
    res = await db.execute(stmt)
    page_items = res.scalars().all()
    
    users = []
    for u in page_items:
        users.append({
            'id': u.id, 'username': u.username, 'role': u.role,
            'enabled': u.enabled,
            'created_at': u.created_at,
            'telegramId': u.telegramId,
            'email': u.email,
            'description': u.description,
            'connections_count': len(u.connections),
            'traffic_used': u.traffic_used,
            'traffic_total': u.traffic_total,
            'traffic_limit': u.traffic_limit,
            'traffic_reset_strategy': u.traffic_reset_strategy,
            'last_reset_at': u.last_reset_at,
            'expiration_date': u.expiration_date,
            'share_enabled': u.share_enabled,
            'share_token': u.share_token,
            'has_share_password': bool(u.share_password_hash),
            'source': 'Remnawave' if u.remnawave_uuid else 'Local'
        })
    return {
        'users': users,
        'total': total,
        'page': page,
        'size': size,
        'pages': (total + size - 1) // size if size > 0 else 0
    }


@router.post('/api/users/add', tags=["Users"])
async def api_add_user(request: Request, req: AddUserRequest, db: AsyncSession = Depends(get_db)):
    cur = await app_module._check_admin(db, request)
    if not cur or cur.role != 'admin':
        return JSONResponse({'error': 'Forbidden'}, status_code=403)
    try:
        lang = request.cookies.get('lang', 'ru')
        
        stmt = select(models.User).where(models.User.username == req.username)
        res = await db.execute(stmt)
        if res.scalar_one_or_none():
            return JSONResponse({'error': app_module._t('user_exists', lang)}, status_code=400)
            
        if req.role not in ('admin', 'support', 'user'):
            return JSONResponse({'error': 'Invalid role'}, status_code=400)
            
        new_user = models.User(
            id=str(uuid.uuid4()),
            username=req.username,
            password_hash=app_module.hash_password(req.password),
            role=req.role,
            telegramId=req.telegramId,
            email=req.email,
            description=req.description,
            traffic_limit=int(req.traffic_limit * 1024**3) if req.traffic_limit else 0,
            traffic_reset_strategy=req.traffic_reset_strategy or 'never',
            traffic_used=0,
            traffic_total=0,
            last_reset_at=datetime.now(timezone.utc),
            expiration_date=req.expiration_date,
            enabled=True,
            created_at=datetime.now(timezone.utc),
            share_enabled=False,
            share_token=secrets.token_urlsafe(16),
        )
        db.add(new_user)
        await db.commit()
        await db.refresh(new_user)

        result = {'status': 'success', 'user_id': new_user.id}

        if req.server_id is not None and req.protocol:
            server = await db.get(models.Server, req.server_id)
            if server:
                stmt_proto = select(models.ServerProtocol).where(
                    models.ServerProtocol.server_id == req.server_id,
                    models.ServerProtocol.protocol_type == req.protocol
                )
                proto_info = (await db.execute(stmt_proto)).scalar_one_or_none()
                port = proto_info.port if proto_info else '55424'
                
                conn_name = req.connection_name or f"{req.username}_vpn"
                ssh = app_module.get_ssh(server, db)
                try:
                    await asyncio.to_thread(ssh.connect)
                    manager = app_module.get_protocol_manager(ssh, req.protocol)
                    def _add_user_client():
                        if app_module.protocol_base(req.protocol) == 'telemt':
                            return manager.add_client(
                                req.protocol, conn_name, server.host, port,
                                telemt_quota=req.telemt_quota,
                                telemt_max_ips=req.telemt_max_ips,
                                telemt_expiry=req.telemt_expiry,
                                secret=req.telemt_secret,
                                user_ad_tag=req.telemt_ad_tag,
                                max_tcp_conns=req.telemt_max_conns
                            )
                        else:
                            return manager.add_client(req.protocol, conn_name, server.host, port)

                    conn_result = await asyncio.to_thread(_add_user_client)
                finally:
                    try:
                        await asyncio.to_thread(ssh.disconnect)
                    except Exception:
                        pass


                if conn_result.get('client_id'):
                    conn = models.Connection(
                        user_id=new_user.id,
                        server_id=req.server_id,
                        protocol=req.protocol,
                        client_id=conn_result['client_id'],
                        name=conn_name
                    )
                    db.add(conn)
                    await db.commit()
                    
                    result['connection_created'] = True
                    if conn_result.get('config'):
                        result['config'] = conn_result['config']
                        result['vpn_link'] = app_module.generate_vpn_link(conn_result['config'], req.protocol)
                        result['qr_content'] = app_module.generate_qr_content(conn_result['config'], req.protocol)
        app_module.capture_posthog_event(
            request,
            cur,
            "user_created",
            {
                "role": new_user.role,
                "connection_created": bool(result.get("connection_created")),
            },
        )
        return result
    except Exception as e:
        app_module.logger.exception("Error adding user")
        return JSONResponse({'error': str(e)}, status_code=500)


@router.post('/api/users/{user_id}/update', tags=["Users"])
async def api_update_user(request: Request, user_id: str, req: UpdateUserRequest, db: AsyncSession = Depends(get_db)):
    if not await app_module._check_admin(db, request):
        return JSONResponse({'error': 'Forbidden'}, status_code=403)
    try:
        user = await db.get(models.User, user_id)
        if not user:
            return JSONResponse({'error': 'User not found'}, status_code=404)
            
        if req.telegramId is not None: user.telegramId = req.telegramId
        if req.email is not None: user.email = req.email
        if req.description is not None: user.description = req.description
        if req.traffic_limit is not None: 
            new_limit = int(req.traffic_limit * 1024**3)
            user.traffic_limit = new_limit
        
        if req.traffic_reset_strategy is not None:
            user.traffic_reset_strategy = req.traffic_reset_strategy
            user.last_reset_at = datetime.now(timezone.utc)
            
        req_fields = getattr(req, 'model_fields_set', getattr(req, '__fields_set__', set()))
        if 'expiration_date' in req_fields:
            user.expiration_date = req.expiration_date or None

        if req.password:
            user.password_hash = app_module.hash_password(req.password)
            
        await db.commit()
        
        if req.traffic_limit is not None:
            if new_limit > 0 and user.traffic_used < new_limit and not user.enabled:
                await app_module.perform_toggle_user(db, user_id, True)

        return {'status': 'success'}
    except Exception as e:
        app_module.logger.exception("Error updating user")
        return JSONResponse({'error': str(e)}, status_code=500)


@router.post('/api/users/{user_id}/delete', tags=["Users"])
async def api_delete_user(request: Request, user_id: str, db: AsyncSession = Depends(get_db)):
    cur = await app_module._check_admin(db, request)
    if not cur or cur.role != 'admin':
        return JSONResponse({'error': 'Forbidden'}, status_code=403)
    lang = request.cookies.get('lang', 'ru')
    if cur.id == user_id:
        return JSONResponse({'error': app_module._t('cannot_delete_self', lang)}, status_code=400)
    try:
        success = await app_module.perform_delete_user(db, user_id)
        if not success:
            return JSONResponse({'error': 'User not found'}, status_code=404)
        return {'status': 'success'}
    except Exception as e:
        app_module.logger.exception("Error deleting user")
        return JSONResponse({'error': str(e)}, status_code=500)


@router.post('/api/users/{user_id}/toggle', tags=["Users"])
async def api_toggle_user(request: Request, user_id: str, req: ToggleUserRequest, db: AsyncSession = Depends(get_db)):
    cur = await app_module._check_admin(db, request)
    if not cur or cur.role != 'admin':
        return JSONResponse({'error': 'Forbidden'}, status_code=403)
    try:
        success = await app_module.perform_toggle_user(db, user_id, req.enabled)
        if not success:
            return JSONResponse({'error': 'User not found'}, status_code=404)
        app_module.capture_posthog_event(
            request, cur, "user_status_changed", {"enabled": req.enabled}
        )
        return {'status': 'success', 'enabled': req.enabled}
    except Exception as e:
        app_module.logger.exception("Error toggling user")
        return JSONResponse({'error': str(e)}, status_code=500)


@router.post('/api/users/{user_id}/connections/add', tags=["Users"])
async def api_add_user_connection(request: Request, user_id: str, req: AddUserConnectionRequest, db: AsyncSession = Depends(get_db)):
    if not await app_module._check_admin(db, request):
        return JSONResponse({'error': 'Forbidden'}, status_code=403)
    try:
        user = await db.get(models.User, user_id)
        if not user:
            return JSONResponse({'error': 'User not found'}, status_code=404)
        server = await db.get(models.Server, req.server_id)
        if not server:
            return JSONResponse({'error': 'Server not found'}, status_code=404)
            
        stmt_proto = select(models.ServerProtocol).where(
            models.ServerProtocol.server_id == req.server_id,
            models.ServerProtocol.protocol_type == req.protocol
        )
        proto_info = (await db.execute(stmt_proto)).scalar_one_or_none()
        port = proto_info.port if proto_info else '55424'
        
        ssh = app_module.get_ssh(server, db)
        await asyncio.to_thread(ssh.connect)
        manager = app_module.get_protocol_manager(ssh, req.protocol)
        
        if req.client_id:
            target_client_id = req.client_id
            config = await asyncio.to_thread(manager.get_client_config, req.protocol, req.client_id, server.host, port)
            result = {'client_id': target_client_id, 'config': config}
        else:
            if app_module.protocol_base(req.protocol) == 'telemt':
                result = await asyncio.to_thread(
                    manager.add_client, req.protocol, req.name, server.host, port,
                    telemt_quota=req.telemt_quota,
                    telemt_max_ips=req.telemt_max_ips,
                    telemt_expiry=req.telemt_expiry,
                    secret=req.telemt_secret,
                    user_ad_tag=req.telemt_ad_tag,
                    max_tcp_conns=req.telemt_max_conns
                )
            else:
                result = await asyncio.to_thread(manager.add_client, req.protocol, req.name, server.host, port)
        
        await asyncio.to_thread(ssh.disconnect)

        if result.get('client_id'):
            conn = models.Connection(
                user_id=user_id,
                server_id=req.server_id,
                protocol=req.protocol,
                client_id=result['client_id'],
                name=req.name
            )
            db.add(conn)
            await db.commit()

        resp = {'status': 'success'}
        if result.get('config'):
            resp['config'] = result['config']
            resp['vpn_link'] = app_module.generate_vpn_link(result['config'], req.protocol)
            resp['qr_content'] = app_module.generate_qr_content(result['config'], req.protocol)
        return resp
    except Exception as e:
        app_module.logger.exception("Error adding user connection")
        return JSONResponse({'error': str(e)}, status_code=500)


@router.get('/api/users/{user_id}/connections', tags=["Users"])
async def api_get_user_connections(request: Request, user_id: str, db: AsyncSession = Depends(get_db)):
    user = await app_module.get_current_user(db, request)
    if not user:
        return JSONResponse({'error': 'Forbidden'}, status_code=403)
    if user.role == 'user' and user.id != user_id:
        return JSONResponse({'error': 'Forbidden'}, status_code=403)
        
    stmt = select(models.Connection).where(models.Connection.user_id == user_id).options(selectinload(models.Connection.server))
    res = await db.execute(stmt)
    conns_models = res.scalars().all()
    
    conns = []
    for c in conns_models:
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
    return {'connections': conns}


@router.get('/api/my/connections', tags=["Self-service"])
async def api_my_connections(request: Request, db: AsyncSession = Depends(get_db)):
    user = await app_module.get_current_user(db, request)
    if not user:
        return JSONResponse({'error': 'Forbidden'}, status_code=403)
        
    stmt = select(models.Connection).where(models.Connection.user_id == user.id).options(selectinload(models.Connection.server))
    res = await db.execute(stmt)
    conns_models = res.scalars().all()
    
    conns = []
    for c in conns_models:
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
    return {'connections': conns}
