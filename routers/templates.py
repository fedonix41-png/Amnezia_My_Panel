from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

import app as app_module

router = APIRouter(tags=["System Templates"])


@router.get('/login', response_class=HTMLResponse)
async def login_page(request: Request):
    if app_module.get_current_user(request):
        return RedirectResponse(url='/', status_code=302)
    return app_module.tpl(request, 'login.html')


@router.get("/set_lang/{lang}")
async def set_lang(lang: str, request: Request):
    ref = request.headers.get("referer", "/")
    response = RedirectResponse(url=ref)
    response.set_cookie(key="lang", value=lang, max_age=31536000)
    return response


@router.get('/logout')
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse(url='/login', status_code=302)


@router.get('/', response_class=HTMLResponse)
async def index(request: Request):
    user = app_module.get_current_user(request)
    if not user:
        return RedirectResponse(url='/login', status_code=302)
    if user['role'] == 'user':
        return RedirectResponse(url='/my', status_code=302)
    data = app_module.load_data()
    return app_module.tpl(request, 'index.html', servers=data['servers'])


@router.get('/server/{server_id}', response_class=HTMLResponse)
async def server_detail(request: Request, server_id: int):
    user = app_module.get_current_user(request)
    if not user:
        return RedirectResponse(url='/login', status_code=302)
    if user['role'] not in ('admin', 'support'):
        return RedirectResponse(url='/my', status_code=302)
    data = app_module.load_data()
    if server_id >= len(data['servers']):
        return RedirectResponse(url='/')
    server = data['servers'][server_id]
    users_list = data.get('users', [])
    return app_module.tpl(request, 'server.html', server=server, server_id=server_id, users=users_list)


@router.get('/users', response_class=HTMLResponse)
async def users_page(request: Request):
    user = app_module.get_current_user(request)
    if not user:
        return RedirectResponse(url='/login', status_code=302)
    if user['role'] not in ('admin', 'support'):
        return RedirectResponse(url='/my', status_code=302)
    data = app_module.load_data()
    users_list = data.get('users', [])
    # Count connections per user
    conns = data.get('user_connections', [])
    for u in users_list:
        u['connections_count'] = sum(1 for c in conns if c['user_id'] == u['id'])
    servers = data['servers']
    return app_module.tpl(request, 'users.html', users=users_list, servers=servers)


@router.get('/my', response_class=HTMLResponse)
async def my_connections_page(request: Request):
    user = app_module.get_current_user(request)
    if not user:
        return RedirectResponse(url='/login', status_code=302)
    data = app_module.load_data()
    conns = [c for c in data.get('user_connections', []) if c['user_id'] == user['id']]
    # Enrich with server names
    for c in conns:
        sid = c.get('server_id', 0)
        if sid < len(data['servers']):
            c['server_name'] = data['servers'][sid].get('name', data['servers'][sid].get('host', ''))
        else:
            c['server_name'] = 'Unknown'
    return app_module.tpl(request, 'my_connections.html', connections=conns)
