import secrets
from typing import Optional
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, HTMLResponse
from pydantic import BaseModel

import config
import app as app_module

router = APIRouter(tags=["Sharing", "Self-service"])


class ShareSetupRequest(BaseModel):
    enabled: bool = True
    password: Optional[str] = None


class ShareAuthRequest(BaseModel):
    password: str = ''


@router.post('/api/users/{user_id}/share/setup', tags=["Users"])
async def api_user_share_setup(user_id: str, req: ShareSetupRequest, request: Request):
    if not app_module._check_admin(request):
        return JSONResponse({'error': 'Forbidden'}, status_code=403)
    data = app_module.load_data()
    user = next((u for u in data['users'] if u['id'] == user_id), None)
    if not user:
        return JSONResponse({'error': 'User not found'}, status_code=404)
    
    user['share_enabled'] = req.enabled
    if not user.get('share_token'):
        user['share_token'] = secrets.token_urlsafe(16)
    if req.password:
        user['share_password_hash'] = app_module.hash_password(req.password)
    elif req.password == "":
        user['share_password_hash'] = None
        
    app_module.save_data(data)
    return {'status': 'success', 'share_token': user.get('share_token')}


@router.get('/share/{token}', response_class=HTMLResponse, tags=["System Templates"])
@app_module.limiter.limit(config.SHARE_RATE_LIMIT)
async def share_page(token: str, request: Request):
    data = app_module.load_data()
    user = next((u for u in data['users'] if u.get('share_token') == token), None)
    if not user or not user.get('share_enabled'):
        lang = request.cookies.get('lang', 'ru')
        return HTMLResponse(f"<h1>{app_module._t('share_not_found', lang)}</h1><p>{app_module._t('share_not_found_desc', lang)}</p>", status_code=404)
    
    auth_session_key = f'share_auth_{token}'
    need_password = bool(user.get('share_password_hash')) and not request.session.get(auth_session_key)
    
    return app_module.tpl(request, 'user_share.html', 
               share_user=user, 
               need_password=need_password, 
               token=token)


@router.post('/api/share/{token}/auth')
@app_module.limiter.limit(config.SHARE_RATE_LIMIT)
async def api_share_auth(token: str, req: ShareAuthRequest, request: Request):
    data = app_module.load_data()
    user = next((u for u in data['users'] if u.get('share_token') == token), None)
    if not user or not user.get('share_enabled'):
        return JSONResponse({'error': 'Link expired or disabled'}, status_code=404)
    
    if app_module.verify_password(req.password, user.get('share_password_hash', '')):
        request.session[f'share_auth_{token}'] = True
        return {'status': 'success'}
    else:
        lang = request.cookies.get('lang', 'ru')
        return JSONResponse({'error': app_module._t('wrong_share_password', lang)}, status_code=401)


@router.get('/api/share/{token}/connections')
@app_module.limiter.limit(config.SHARE_RATE_LIMIT)
async def api_share_connections(token: str, request: Request):
    data = app_module.load_data()
    user = next((u for u in data['users'] if u.get('share_token') == token), None)
    if not user or not user.get('share_enabled'):
        return JSONResponse({'error': 'Forbidden'}, status_code=403)
    
    if user.get('share_password_hash'):
        if not request.session.get(f'share_auth_{token}'):
            return JSONResponse({'error': 'Unauthorized'}, status_code=401)
            
    conns = [dict(c) for c in data.get('user_connections', []) if c['user_id'] == user['id']]
    for c in conns:
        sid = c['server_id']
        if sid < len(data['servers']):
            c['server_name'] = data['servers'][sid].get('name') or data['servers'][sid]['host']
        else:
            c['server_name'] = 'Unknown'
            
    return {'connections': conns, 'username': user['username']}


@router.post('/api/share/{token}/config/{connection_id}')
@app_module.limiter.limit(config.SHARE_RATE_LIMIT)
async def api_share_config(token: str, connection_id: str, request: Request):
    data = app_module.load_data()
    user = next((u for u in data['users'] if u.get('share_token') == token), None)
    if not user or not user.get('share_enabled'):
        return JSONResponse({'error': 'Forbidden'}, status_code=403)
    
    if user.get('share_password_hash'):
        if not request.session.get(f'share_auth_{token}'):
            return JSONResponse({'error': 'Unauthorized'}, status_code=401)
            
    conn = next((c for c in data.get('user_connections', []) if c['id'] == connection_id and c['user_id'] == user['id']), None)
    if not conn:
        return JSONResponse({'error': 'Not found'}, status_code=404)
        
    try:
        sid = conn['server_id']
        server = data['servers'][sid]
        proto_info = server.get('protocols', {}).get(conn['protocol'], {})
        port = proto_info.get('port', '55424')
        ssh = app_module.get_ssh(server)
        ssh.connect()
        manager = app_module.get_protocol_manager(ssh, conn['protocol'])
        config_text = app_module._manager_call(manager, 'get_client_config', conn['protocol'], conn['client_id'], server['host'], port)
        ssh.disconnect()
        vpn_link = app_module.generate_vpn_link(config_text) if config_text else ''
        qr_content = app_module.generate_qr_content(config_text) if config_text else ''
        return {'config': config_text, 'vpn_link': vpn_link, 'qr_content': qr_content}
    except Exception as e:
        app_module.logger.exception("Error getting shared config")
        return JSONResponse({'error': str(e)}, status_code=500)


@router.post('/api/my/connections/{connection_id}/config')
async def api_my_connection_config(request: Request, connection_id: str):
    user = app_module.get_current_user(request)
    if not user:
        return JSONResponse({'error': 'Forbidden'}, status_code=403)
    try:
        data = app_module.load_data()
        conn = next(
            (c for c in data.get('user_connections', []) if c['id'] == connection_id and c['user_id'] == user['id']),
            None
        )
        if not conn:
            return JSONResponse({'error': 'Connection not found'}, status_code=404)
        sid = conn['server_id']
        if sid >= len(data['servers']):
            return JSONResponse({'error': 'Server not found'}, status_code=404)
        server = data['servers'][sid]
        proto_info = server.get('protocols', {}).get(conn['protocol'], {})
        port = proto_info.get('port', '55424')
        ssh = app_module.get_ssh(server)
        ssh.connect()
        manager = app_module.get_protocol_manager(ssh, conn['protocol'])
        config_text = app_module._manager_call(manager, 'get_client_config', conn['protocol'], conn['client_id'], server['host'], port)
        ssh.disconnect()
        vpn_link = app_module.generate_vpn_link(config_text) if config_text else ''
        qr_content = app_module.generate_qr_content(config_text) if config_text else ''
        return {'config': config_text, 'vpn_link': vpn_link, 'qr_content': qr_content}
    except Exception as e:
        app_module.logger.exception("Error getting my connection config")
        return JSONResponse({'error': str(e)}, status_code=500)
