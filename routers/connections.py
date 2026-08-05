import uuid
from datetime import datetime
from typing import Optional
from fastapi import APIRouter, Request, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel

import app as app_module
from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from sqlalchemy.orm import selectinload
from database.session import get_db
from database.repositories import ServerRepository, ConnectionRepository
from database import models

router = APIRouter(tags=["Connections"])


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


@router.get('/api/servers/{server_id}/connections')
async def api_get_connections(request: Request, server_id: int, protocol: str = Query(default='awg'), db: AsyncSession = Depends(get_db)):
    if not protocol:
        protocol = 'awg'
    if not await app_module._check_admin(db, request):
        return JSONResponse({'error': 'Forbidden'}, status_code=403)
    try:
        server = await ServerRepository.get_by_id(db, server_id)
        if not server:
            return JSONResponse({'error': 'Server not found'}, status_code=404)
        ssh = app_module.get_ssh(server, db)
        ssh.connect()
        manager = app_module.get_protocol_manager(ssh, protocol)
        clients = app_module._manager_call(manager, 'get_clients', protocol)
        ssh.disconnect()

        stmt = select(models.Connection).where(
            models.Connection.server_id == server_id,
            models.Connection.protocol == protocol
        ).options(selectinload(models.Connection.user))
        res = await db.execute(stmt)
        user_conns = list(res.scalars().all())

        for client in clients:
            cid = client.get('clientId', '')
            for uc in user_conns:
                if uc.client_id == cid:
                    if uc.user:
                        client['assigned_user'] = uc.user.username
                        client['assigned_user_id'] = uc.user_id
                    break
        return {'clients': clients}
    except Exception as e:
        app_module.logger.exception("Error getting connections")
        return JSONResponse({'error': str(e)}, status_code=500)


@router.post('/api/servers/{server_id}/connections/add')
async def api_add_connection(request: Request, server_id: int, req: AddConnectionRequest, db: AsyncSession = Depends(get_db)):
    actor = await app_module._check_admin(db, request)
    if not actor:
        return JSONResponse({'error': 'Forbidden'}, status_code=403)
    try:
        server = await ServerRepository.get_by_id(db, server_id)
        if not server:
            return JSONResponse({'error': 'Server not found'}, status_code=404)
        
        stmt_proto = select(models.ServerProtocol).where(
            models.ServerProtocol.server_id == server_id,
            models.ServerProtocol.protocol_type == req.protocol
        )
        res_proto = await db.execute(stmt_proto)
        proto_info = res_proto.scalar_one_or_none()
        port = proto_info.port if proto_info else '55424'

        ssh = app_module.get_ssh(server, db)
        ssh.connect()
        manager = app_module.get_protocol_manager(ssh, req.protocol)
        
        if app_module.protocol_base(req.protocol) == 'telemt':
            result = manager.add_client(
                req.protocol, req.name, server.host, port,
                telemt_quota=req.telemt_quota,
                telemt_max_ips=req.telemt_max_ips,
                telemt_expiry=req.telemt_expiry,
                secret=req.telemt_secret,
                user_ad_tag=req.telemt_ad_tag,
                max_tcp_conns=req.telemt_max_conns
            )
        elif app_module.protocol_base(req.protocol) == 'wireguard':
            result = manager.add_client(req.name, server.host)
        else:
            result = manager.add_client(req.protocol, req.name, server.host, port)
        ssh.disconnect()

        if result.get('config'):
            result['vpn_link'] = app_module.generate_vpn_link(result['config'])
            result['qr_content'] = app_module.generate_qr_content(result['config'])

        if req.user_id and result.get('client_id'):
            conn = models.Connection(
                user_id=req.user_id,
                server_id=server_id,
                protocol=req.protocol,
                client_id=result['client_id'],
                name=req.name
            )
            db.add(conn)
            await db.commit()

        app_module.capture_posthog_event(
            request,
            actor,
            "connection_added",
            {"protocol": app_module.protocol_base(req.protocol)},
        )
        return result
    except Exception as e:
        app_module.logger.exception("Error adding connection")
        return JSONResponse({'error': str(e)}, status_code=500)


@router.post('/api/servers/{server_id}/connections/remove')
async def api_remove_connection(request: Request, server_id: int, req: ConnectionActionRequest, db: AsyncSession = Depends(get_db)):
    actor = await app_module._check_admin(db, request)
    if not actor:
        return JSONResponse({'error': 'Forbidden'}, status_code=403)
    try:
        server = await ServerRepository.get_by_id(db, server_id)
        if not server:
            return JSONResponse({'error': 'Server not found'}, status_code=404)
        if not req.client_id:
            return JSONResponse({'error': 'Client ID is required'}, status_code=400)
        ssh = app_module.get_ssh(server, db)
        ssh.connect()
        manager = app_module.get_protocol_manager(ssh, req.protocol)
        app_module._manager_call(manager, 'remove_client', req.protocol, req.client_id)
        ssh.disconnect()
        
        stmt = select(models.Connection).where(
            models.Connection.client_id == req.client_id,
            models.Connection.server_id == server_id
        )
        res = await db.execute(stmt)
        for c in res.scalars().all():
            await db.delete(c)
        await db.commit()

        app_module.capture_posthog_event(
            request,
            actor,
            "connection_removed",
            {"protocol": app_module.protocol_base(req.protocol)},
        )
        return {'status': 'success'}
    except Exception as e:
        app_module.logger.exception("Error removing connection")
        return JSONResponse({'error': str(e)}, status_code=500)


@router.post('/api/servers/{server_id}/connections/edit')
async def api_edit_connection(request: Request, server_id: int, req: EditConnectionRequest, db: AsyncSession = Depends(get_db)):
    if not await app_module._check_admin(db, request):
        return JSONResponse({'error': 'Forbidden'}, status_code=403)
    try:
        server = await ServerRepository.get_by_id(db, server_id)
        if not server:
            return JSONResponse({'error': 'Server not found'}, status_code=404)
        
        ssh = app_module.get_ssh(server, db)
        ssh.connect()
        manager = app_module.get_protocol_manager(ssh, req.protocol)
        
        edit_params = {}
        if app_module.protocol_base(req.protocol) == 'telemt':
            edit_params['telemt_quota'] = req.telemt_quota
            edit_params['telemt_max_ips'] = req.telemt_max_ips
            edit_params['telemt_expiry'] = req.telemt_expiry
            edit_params['secret'] = req.telemt_secret
            edit_params['user_ad_tag'] = req.telemt_ad_tag
            edit_params['max_tcp_conns'] = req.telemt_max_conns
            
        result = manager.edit_client(req.protocol, req.client_id, edit_params)
        ssh.disconnect()
        return result
    except Exception as e:
        app_module.logger.exception("Error editing connection")
        return JSONResponse({'error': str(e)}, status_code=500)


@router.post('/api/servers/{server_id}/connections/config')
async def api_get_connection_config(request: Request, server_id: int, req: ConnectionActionRequest, db: AsyncSession = Depends(get_db)):
    user = await app_module.get_current_user(db, request)
    if not user:
        return JSONResponse({'error': 'Forbidden'}, status_code=403)
    try:
        server = await ServerRepository.get_by_id(db, server_id)
        if not server:
            return JSONResponse({'error': 'Server not found'}, status_code=404)
        if user.role == 'user':
            stmt = select(models.Connection).where(
                models.Connection.client_id == req.client_id,
                models.Connection.server_id == server_id,
                models.Connection.user_id == user.id
            )
            res = await db.execute(stmt)
            if not res.scalar_one_or_none():
                return JSONResponse({'error': 'Forbidden'}, status_code=403)
                
        if not req.client_id:
            return JSONResponse({'error': 'Client ID is required'}, status_code=400)
            
        stmt_proto = select(models.ServerProtocol).where(
            models.ServerProtocol.server_id == server_id,
            models.ServerProtocol.protocol_type == req.protocol
        )
        res_proto = await db.execute(stmt_proto)
        proto_info = res_proto.scalar_one_or_none()
        port = proto_info.port if proto_info else '55424'
        
        ssh = app_module.get_ssh(server, db)
        ssh.connect()
        manager = app_module.get_protocol_manager(ssh, req.protocol)
        config = app_module._manager_call(manager, 'get_client_config', req.protocol, req.client_id, server.host, port)
        ssh.disconnect()
        vpn_link = app_module.generate_vpn_link(config) if config else ''
        qr_content = app_module.generate_qr_content(config) if config else ''
        if app_module.posthog_client:
            app_module.posthog_client.capture(
                "vpn_config_requested",
                properties={"protocol": app_module.protocol_base(req.protocol)},
            )
        return {'config': config, 'vpn_link': vpn_link, 'qr_content': qr_content}
    except Exception as e:
        app_module.logger.exception("Error getting connection config")
        return JSONResponse({'error': str(e)}, status_code=500)


@router.post('/api/servers/{server_id}/connections/toggle')
async def api_toggle_connection(request: Request, server_id: int, req: ToggleConnectionRequest, db: AsyncSession = Depends(get_db)):
    actor = await app_module._check_admin(db, request)
    if not actor:
        return JSONResponse({'error': 'Forbidden'}, status_code=403)
    try:
        server = await ServerRepository.get_by_id(db, server_id)
        if not server:
            return JSONResponse({'error': 'Server not found'}, status_code=404)
        if not req.client_id:
            return JSONResponse({'error': 'Client ID is required'}, status_code=400)
        ssh = app_module.get_ssh(server, db)
        ssh.connect()
        manager = app_module.get_protocol_manager(ssh, req.protocol)
        app_module._manager_call(manager, 'toggle_client', req.protocol, req.client_id, req.enable)
        ssh.disconnect()
        status = 'enabled' if req.enable else 'disabled'
        app_module.capture_posthog_event(
            request,
            actor,
            "connection_status_changed",
            {
                "protocol": app_module.protocol_base(req.protocol),
                "enabled": req.enable,
            },
        )
        return {'status': 'success', 'enabled': req.enable, 'message': f'Connection {status}'}
    except Exception as e:
        app_module.logger.exception("Error toggling connection")
        return JSONResponse({'error': str(e)}, status_code=500)
