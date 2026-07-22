import uuid
from datetime import datetime
from typing import Optional
from fastapi import APIRouter, Request, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel

import app as app_module

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
async def api_get_connections(request: Request, server_id: int, protocol: str = Query(default='awg')):
    if not protocol:
        protocol = 'awg'
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
        clients = app_module._manager_call(manager, 'get_clients', protocol)
        ssh.disconnect()

        user_conns = data.get('user_connections', [])
        users = data.get('users', [])
        users_map = {u['id']: u for u in users}
        for client in clients:
            cid = client.get('clientId', '')
            for uc in user_conns:
                if uc.get('client_id') == cid and uc.get('server_id') == server_id and uc.get('protocol') == protocol:
                    uid = uc.get('user_id')
                    u = users_map.get(uid)
                    if u:
                        client['assigned_user'] = u['username']
                        client['assigned_user_id'] = uid
                    break
        return {'clients': clients}
    except Exception as e:
        app_module.logger.exception("Error getting connections")
        return JSONResponse({'error': str(e)}, status_code=500)


@router.post('/api/servers/{server_id}/connections/add')
async def api_add_connection(request: Request, server_id: int, req: AddConnectionRequest):
    if not app_module._check_admin(request):
        return JSONResponse({'error': 'Forbidden'}, status_code=403)
    try:
        data = app_module.load_data()
        if server_id >= len(data['servers']):
            return JSONResponse({'error': 'Server not found'}, status_code=404)
        server = data['servers'][server_id]
        proto_info = server.get('protocols', {}).get(req.protocol, {})
        port = proto_info.get('port', '55424')
        ssh = app_module.get_ssh(server)
        ssh.connect()
        manager = app_module.get_protocol_manager(ssh, req.protocol)
        
        if app_module.protocol_base(req.protocol) == 'telemt':
            result = manager.add_client(
                req.protocol, req.name, server['host'], port,
                telemt_quota=req.telemt_quota,
                telemt_max_ips=req.telemt_max_ips,
                telemt_expiry=req.telemt_expiry,
                secret=req.telemt_secret,
                user_ad_tag=req.telemt_ad_tag,
                max_tcp_conns=req.telemt_max_conns
            )
        elif app_module.protocol_base(req.protocol) == 'wireguard':
            result = manager.add_client(req.name, server['host'])
        else:
            result = manager.add_client(req.protocol, req.name, server['host'], port)
        ssh.disconnect()

        if result.get('config'):
            result['vpn_link'] = app_module.generate_vpn_link(result['config'])
            result['qr_content'] = app_module.generate_qr_content(result['config'])

        if req.user_id and result.get('client_id'):
            conn = {
                'id': str(uuid.uuid4()),
                'user_id': req.user_id,
                'server_id': server_id,
                'protocol': req.protocol,
                'client_id': result['client_id'],
                'name': req.name,
                'created_at': datetime.now().isoformat(),
            }
            data['user_connections'].append(conn)
            app_module.save_data(data)

        return result
    except Exception as e:
        app_module.logger.exception("Error adding connection")
        return JSONResponse({'error': str(e)}, status_code=500)


@router.post('/api/servers/{server_id}/connections/remove')
async def api_remove_connection(request: Request, server_id: int, req: ConnectionActionRequest):
    if not app_module._check_admin(request):
        return JSONResponse({'error': 'Forbidden'}, status_code=403)
    try:
        data = app_module.load_data()
        if server_id >= len(data['servers']):
            return JSONResponse({'error': 'Server not found'}, status_code=404)
        server = data['servers'][server_id]
        if not req.client_id:
            return JSONResponse({'error': 'Client ID is required'}, status_code=400)
        ssh = app_module.get_ssh(server)
        ssh.connect()
        manager = app_module.get_protocol_manager(ssh, req.protocol)
        app_module._manager_call(manager, 'remove_client', req.protocol, req.client_id)
        ssh.disconnect()
        data['user_connections'] = [
            c for c in data.get('user_connections', [])
            if not (c.get('client_id') == req.client_id and c.get('server_id') == server_id)
        ]
        app_module.save_data(data)
        return {'status': 'success'}
    except Exception as e:
        app_module.logger.exception("Error removing connection")
        return JSONResponse({'error': str(e)}, status_code=500)


@router.post('/api/servers/{server_id}/connections/edit')
async def api_edit_connection(request: Request, server_id: int, req: EditConnectionRequest):
    if not app_module._check_admin(request):
        return JSONResponse({'error': 'Forbidden'}, status_code=403)
    try:
        data = app_module.load_data()
        if server_id >= len(data['servers']):
            return JSONResponse({'error': 'Server not found'}, status_code=404)
        server = data['servers'][server_id]
        
        ssh = app_module.get_ssh(server)
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
async def api_get_connection_config(request: Request, server_id: int, req: ConnectionActionRequest):
    user = app_module.get_current_user(request)
    if not user:
        return JSONResponse({'error': 'Forbidden'}, status_code=403)
    try:
        data = app_module.load_data()
        if server_id >= len(data['servers']):
            return JSONResponse({'error': 'Server not found'}, status_code=404)
        if user['role'] == 'user':
            owned = any(
                c for c in data.get('user_connections', [])
                if c.get('client_id') == req.client_id and c.get('server_id') == server_id and c.get('user_id') == user['id']
            )
            if not owned:
                return JSONResponse({'error': 'Forbidden'}, status_code=403)
        server = data['servers'][server_id]
        if not req.client_id:
            return JSONResponse({'error': 'Client ID is required'}, status_code=400)
        proto_info = server.get('protocols', {}).get(req.protocol, {})
        port = proto_info.get('port', '55424')
        ssh = app_module.get_ssh(server)
        ssh.connect()
        manager = app_module.get_protocol_manager(ssh, req.protocol)
        config = app_module._manager_call(manager, 'get_client_config', req.protocol, req.client_id, server['host'], port)
        ssh.disconnect()
        vpn_link = app_module.generate_vpn_link(config) if config else ''
        qr_content = app_module.generate_qr_content(config) if config else ''
        return {'config': config, 'vpn_link': vpn_link, 'qr_content': qr_content}
    except Exception as e:
        app_module.logger.exception("Error getting connection config")
        return JSONResponse({'error': str(e)}, status_code=500)


@router.post('/api/servers/{server_id}/connections/toggle')
async def api_toggle_connection(request: Request, server_id: int, req: ToggleConnectionRequest):
    if not app_module._check_admin(request):
        return JSONResponse({'error': 'Forbidden'}, status_code=403)
    try:
        data = app_module.load_data()
        if server_id >= len(data['servers']):
            return JSONResponse({'error': 'Server not found'}, status_code=404)
        server = data['servers'][server_id]
        if not req.client_id:
            return JSONResponse({'error': 'Client ID is required'}, status_code=400)
        ssh = app_module.get_ssh(server)
        ssh.connect()
        manager = app_module.get_protocol_manager(ssh, req.protocol)
        app_module._manager_call(manager, 'toggle_client', req.protocol, req.client_id, req.enable)
        ssh.disconnect()
        status = 'enabled' if req.enable else 'disabled'
        return {'status': 'success', 'enabled': req.enable, 'message': f'Connection {status}'}
    except Exception as e:
        app_module.logger.exception("Error toggling connection")
        return JSONResponse({'error': str(e)}, status_code=500)
