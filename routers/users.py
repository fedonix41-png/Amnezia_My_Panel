import uuid
import secrets
import asyncio
from datetime import datetime
from typing import Optional
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

import app as app_module

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
    expiration_date: Optional[str] = None
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
    expiration_date: Optional[str] = None
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
async def api_list_users(request: Request, search: str = '', page: int = 1, size: int = 10):
    if not app_module._check_admin(request):
        return JSONResponse({'error': 'Forbidden'}, status_code=403)
    data = app_module.load_data()
    all_users = data.get('users', [])
    conns = data.get('user_connections', [])
    
    filtered = []
    search = search.lower()
    for u in all_users:
        if search:
            match = (search in u['username'].lower() or 
                     (u.get('email') and search in u['email'].lower()) or 
                     (u.get('telegramId') and search in str(u['telegramId']).lower()))
            if not match:
                continue
        filtered.append(u)
        
    total = len(filtered)
    start = (page - 1) * size
    end = start + size
    page_items = filtered[start:end]
    
    users = []
    for u in page_items:
        users.append({
            'id': u['id'], 'username': u['username'], 'role': u['role'],
            'enabled': u.get('enabled', True),
            'created_at': u.get('created_at', ''),
            'telegramId': u.get('telegramId'),
            'email': u.get('email'),
            'description': u.get('description'),
            'connections_count': sum(1 for c in conns if c['user_id'] == u['id']),
            'traffic_used': u.get('traffic_used', 0),
            'traffic_total': u.get('traffic_total', 0),
            'traffic_limit': u.get('traffic_limit', 0),
            'traffic_reset_strategy': u.get('traffic_reset_strategy', 'never'),
            'last_reset_at': u.get('last_reset_at'),
            "expiration_date": u.get("expiration_date"),
            'share_enabled': u.get('share_enabled', False),
            'share_token': u.get('share_token'),
            'has_share_password': bool(u.get('share_password_hash')),
            'source': 'Remnawave' if u.get('remnawave_uuid') else 'Local'
        })
    return {
        'users': users,
        'total': total,
        'page': page,
        'size': size,
        'pages': (total + size - 1) // size
    }


@router.post('/api/users/add', tags=["Users"])
async def api_add_user(request: Request, req: AddUserRequest):
    cur = app_module.get_current_user(request)
    if not cur or cur['role'] != 'admin':
        return JSONResponse({'error': 'Forbidden'}, status_code=403)
    try:
        data = app_module.load_data()
        lang = request.cookies.get('lang', 'ru')
        if any(u['username'] == req.username for u in data.get('users', [])):
            return JSONResponse({'error': app_module._t('user_exists', lang)}, status_code=400)
        if req.role not in ('admin', 'support', 'user'):
            return JSONResponse({'error': 'Invalid role'}, status_code=400)
        new_user = {
            'id': str(uuid.uuid4()),
            'username': req.username,
            'password_hash': app_module.hash_password(req.password),
            'role': req.role,
            'telegramId': req.telegramId,
            'email': req.email,
            'description': req.description,
            'traffic_limit': int(req.traffic_limit * 1024**3) if req.traffic_limit else 0,
            'traffic_reset_strategy': req.traffic_reset_strategy or 'never',
            'traffic_used': 0,
            'traffic_total': 0,
            'last_reset_at': datetime.now().isoformat(),
            'expiration_date': req.expiration_date,
            'enabled': True,
            'created_at': datetime.now().isoformat(),
            'remnawave_uuid': None,
            'share_enabled': False,
            'share_token': secrets.token_urlsafe(16),
            'share_password_hash': None,
        }
        data['users'].append(new_user)
        app_module.save_data(data)

        result = {'status': 'success', 'user_id': new_user['id']}

        if req.server_id is not None and req.protocol:
            if req.server_id < len(data['servers']):
                server = data['servers'][req.server_id]
                proto_info = server.get('protocols', {}).get(req.protocol, {})
                port = proto_info.get('port', '55424')
                conn_name = req.connection_name or f"{req.username}_vpn"
                ssh = app_module.get_ssh(server)
                ssh.connect()
                manager = app_module.get_protocol_manager(ssh, req.protocol)
                if app_module.protocol_base(req.protocol) == 'telemt':
                    conn_result = manager.add_client(
                        req.protocol, conn_name, server['host'], port,
                        telemt_quota=req.telemt_quota,
                        telemt_max_ips=req.telemt_max_ips,
                        telemt_expiry=req.telemt_expiry,
                        secret=req.telemt_secret,
                        user_ad_tag=req.telemt_ad_tag,
                        max_tcp_conns=req.telemt_max_conns
                    )
                else:
                    conn_result = manager.add_client(req.protocol, conn_name, server['host'], port)
                ssh.disconnect()

                if conn_result.get('client_id'):
                    conn = {
                        'id': str(uuid.uuid4()),
                        'user_id': new_user['id'],
                        'server_id': req.server_id,
                        'protocol': req.protocol,
                        'client_id': conn_result['client_id'],
                        'name': conn_name,
                        'created_at': datetime.now().isoformat(),
                    }
                    data = app_module.load_data()
                    data['user_connections'].append(conn)
                    app_module.save_data(data)
                    result['connection_created'] = True
                    if conn_result.get('config'):
                        result['config'] = conn_result['config']
                        result['vpn_link'] = app_module.generate_vpn_link(conn_result['config'])
                        result['qr_content'] = app_module.generate_qr_content(conn_result['config'])
        return result
    except Exception as e:
        app_module.logger.exception("Error adding user")
        return JSONResponse({'error': str(e)}, status_code=500)


@router.post('/api/users/{user_id}/update', tags=["Users"])
async def api_update_user(request: Request, user_id: str, req: UpdateUserRequest):
    if not app_module._check_admin(request):
        return JSONResponse({'error': 'Forbidden'}, status_code=403)
    try:
        data = app_module.load_data()
        user = next((u for u in data['users'] if u['id'] == user_id), None)
        if not user:
            return JSONResponse({'error': 'User not found'}, status_code=404)
            
        if req.telegramId is not None: user['telegramId'] = req.telegramId
        if req.email is not None: user['email'] = req.email
        if req.description is not None: user['description'] = req.description
        if req.traffic_limit is not None: 
            new_limit = int(req.traffic_limit * 1024**3)
            user['traffic_limit'] = new_limit
        
        if req.traffic_reset_strategy is not None:
            user['traffic_reset_strategy'] = req.traffic_reset_strategy
            user['last_reset_at'] = datetime.now().isoformat()
            
        req_fields = getattr(req, 'model_fields_set', getattr(req, '__fields_set__', set()))
        if 'expiration_date' in req_fields:
            user['expiration_date'] = req.expiration_date or None

        if req.password:
            user['password_hash'] = app_module.hash_password(req.password)
            
        app_module.save_data(data)
        
        if req.traffic_limit is not None:
            if new_limit > 0 and user.get('traffic_used', 0) < new_limit and not user.get('enabled', True):
                await app_module.perform_toggle_user(data, user_id, True)
                app_module.save_data(data)

        return {'status': 'success'}
    except Exception as e:
        app_module.logger.exception("Error updating user")
        return JSONResponse({'error': str(e)}, status_code=500)


@router.post('/api/users/{user_id}/delete', tags=["Users"])
async def api_delete_user(request: Request, user_id: str):
    cur = app_module.get_current_user(request)
    if not cur or cur['role'] != 'admin':
        return JSONResponse({'error': 'Forbidden'}, status_code=403)
    lang = request.cookies.get('lang', 'ru')
    if cur['id'] == user_id:
        return JSONResponse({'error': app_module._t('cannot_delete_self', lang)}, status_code=400)
    try:
        data = app_module.load_data()
        success = await app_module.perform_delete_user(data, user_id)
        if not success:
            return JSONResponse({'error': 'User not found'}, status_code=404)
        app_module.save_data(data)
        return {'status': 'success'}
    except Exception as e:
        app_module.logger.exception("Error deleting user")
        return JSONResponse({'error': str(e)}, status_code=500)


@router.post('/api/users/{user_id}/toggle', tags=["Users"])
async def api_toggle_user(request: Request, user_id: str, req: ToggleUserRequest):
    cur = app_module.get_current_user(request)
    if not cur or cur['role'] != 'admin':
        return JSONResponse({'error': 'Forbidden'}, status_code=403)
    try:
        data = app_module.load_data()
        success = await app_module.perform_toggle_user(data, user_id, req.enabled)
        if not success:
            return JSONResponse({'error': 'User not found'}, status_code=404)
        app_module.save_data(data)
        return {'status': 'success', 'enabled': req.enabled}
    except Exception as e:
        app_module.logger.exception("Error toggling user")
        return JSONResponse({'error': str(e)}, status_code=500)


@router.post('/api/users/{user_id}/connections/add', tags=["Users"])
async def api_add_user_connection(request: Request, user_id: str, req: AddUserConnectionRequest):
    if not app_module._check_admin(request):
        return JSONResponse({'error': 'Forbidden'}, status_code=403)
    try:
        data = app_module.load_data()
        user = next((u for u in data['users'] if u['id'] == user_id), None)
        if not user:
            return JSONResponse({'error': 'User not found'}, status_code=404)
        if req.server_id >= len(data['servers']):
            return JSONResponse({'error': 'Server not found'}, status_code=404)
        server = data['servers'][req.server_id]
        proto_info = server.get('protocols', {}).get(req.protocol, {})
        port = proto_info.get('port', '55424')
        ssh = app_module.get_ssh(server)
        await asyncio.to_thread(ssh.connect)
        manager = app_module.get_protocol_manager(ssh, req.protocol)
        
        if req.client_id:
            target_client_id = req.client_id
            config = await asyncio.to_thread(manager.get_client_config, req.protocol, req.client_id, server['host'], port)
            result = {'client_id': target_client_id, 'config': config}
        else:
            if app_module.protocol_base(req.protocol) == 'telemt':
                result = await asyncio.to_thread(
                    manager.add_client, req.protocol, req.name, server['host'], port,
                    telemt_quota=req.telemt_quota,
                    telemt_max_ips=req.telemt_max_ips,
                    telemt_expiry=req.telemt_expiry,
                    secret=req.telemt_secret,
                    user_ad_tag=req.telemt_ad_tag,
                    max_tcp_conns=req.telemt_max_conns
                )
            else:
                result = await asyncio.to_thread(manager.add_client, req.protocol, req.name, server['host'], port)
        
        await asyncio.to_thread(ssh.disconnect)

        if result.get('client_id'):
            conn = {
                'id': str(uuid.uuid4()),
                'user_id': user_id,
                'server_id': req.server_id,
                'protocol': req.protocol,
                'client_id': result['client_id'],
                'name': req.name,
                'created_at': datetime.now().isoformat(),
            }
            data = app_module.load_data()
            data['user_connections'].append(conn)
            app_module.save_data(data)

        resp = {'status': 'success'}
        if result.get('config'):
            resp['config'] = result['config']
            resp['vpn_link'] = app_module.generate_vpn_link(result['config'])
            resp['qr_content'] = app_module.generate_qr_content(result['config'])
        return resp
    except Exception as e:
        app_module.logger.exception("Error adding user connection")
        return JSONResponse({'error': str(e)}, status_code=500)


@router.get('/api/users/{user_id}/connections', tags=["Users"])
async def api_get_user_connections(request: Request, user_id: str):
    user = app_module.get_current_user(request)
    if not user:
        return JSONResponse({'error': 'Forbidden'}, status_code=403)
    if user['role'] == 'user' and user['id'] != user_id:
        return JSONResponse({'error': 'Forbidden'}, status_code=403)
    data = app_module.load_data()
    conns = [c for c in data.get('user_connections', []) if c['user_id'] == user_id]
    for c in conns:
        sid = c.get('server_id', 0)
        if sid < len(data['servers']):
            c['server_name'] = data['servers'][sid].get('name', '')
    return {'connections': conns}


@router.get('/api/my/connections', tags=["Self-service"])
async def api_my_connections(request: Request):
    user = app_module.get_current_user(request)
    if not user:
        return JSONResponse({'error': 'Forbidden'}, status_code=403)
    data = app_module.load_data()
    conns = [c for c in data.get('user_connections', []) if c['user_id'] == user['id']]
    for c in conns:
        sid = c.get('server_id', 0)
        if sid < len(data['servers']):
            c['server_name'] = data['servers'][sid].get('name', '')
        else:
            c['server_name'] = 'Unknown'
    return {'connections': conns}
