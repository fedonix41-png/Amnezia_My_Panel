from fastapi import APIRouter, Request, Depends
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from sqlalchemy.orm import selectinload

import app as app_module
from database.session import get_db
from database import models

router = APIRouter(tags=["System Templates"])


@router.get('/login', response_class=HTMLResponse)
async def login_page(request: Request, db: AsyncSession = Depends(get_db)):
    if await app_module.get_current_user(db, request):
        return RedirectResponse(url='/', status_code=302)
    return await app_module.tpl(db, request, 'login.html')


@router.get("/set_lang/{lang}")
async def set_lang(lang: str, request: Request):
    from urllib.parse import urlparse
    ref = request.headers.get("referer", "/")
    parsed = urlparse(ref)
    path = parsed.path
    if not path.startswith("/") or path.startswith("//"):
        path = "/"
    if parsed.query:
        path += "?" + parsed.query
        
    response = RedirectResponse(url=path)
    response.set_cookie(key="lang", value=lang, max_age=31536000)
    return response


@router.get('/logout')
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse(url='/login', status_code=302)


@router.get('/', response_class=HTMLResponse)
async def index(request: Request, db: AsyncSession = Depends(get_db)):
    user = await app_module.get_current_user(db, request)
    if not user:
        return RedirectResponse(url='/login', status_code=302)
    if user.role == 'user':
        return RedirectResponse(url='/my', status_code=302)
        
    stmt_servers = select(models.Server).options(selectinload(models.Server.protocols)).order_by(models.Server.display_order.asc(), models.Server.id.asc())
    res_servers = await db.execute(stmt_servers)
    
    servers = []
    for s in res_servers.scalars().all():
        s_dict = s.to_dict()
        s_dict['protocols'] = {p.protocol_type: p.to_dict() for p in s.protocols}
        servers.append(s_dict)
    
    return await app_module.tpl(db, request, 'index.html', servers=servers)


@router.get('/server/{server_id}', response_class=HTMLResponse)
async def server_detail(request: Request, server_id: int, db: AsyncSession = Depends(get_db)):
    user = await app_module.get_current_user(db, request)
    if not user:
        return RedirectResponse(url='/login', status_code=302)
    if user.role not in ('admin', 'support'):
        return RedirectResponse(url='/my', status_code=302)
        
    server = await db.get(models.Server, server_id)
    if not server:
        return RedirectResponse(url='/')
        
    stmt_users = select(models.User)
    res_users = await db.execute(stmt_users)
    users_list = [u.to_dict() for u in res_users.scalars().all()]
    
    return await app_module.tpl(db, request, 'server.html', server=server.to_dict(), server_id=server_id, users=users_list)


@router.get('/users', response_class=HTMLResponse)
async def users_page(request: Request, db: AsyncSession = Depends(get_db)):
    user = await app_module.get_current_user(db, request)
    if not user:
        return RedirectResponse(url='/login', status_code=302)
    if user.role not in ('admin', 'support'):
        return RedirectResponse(url='/my', status_code=302)
        
    stmt_users = select(models.User).options(selectinload(models.User.connections))
    res_users = await db.execute(stmt_users)
    users_list = []
    for u in res_users.scalars().all():
        u_dict = u.to_dict().copy()
        u_dict['connections_count'] = len(u.connections)
        users_list.append(u_dict)
        
    stmt_servers = select(models.Server).options(selectinload(models.Server.protocols)).order_by(models.Server.display_order.asc(), models.Server.id.asc())
    res_servers = await db.execute(stmt_servers)
    servers = []
    for s in res_servers.scalars().all():
        s_dict = s.to_dict()
        s_dict['protocols'] = {p.protocol_type: p.to_dict() for p in s.protocols}
        servers.append(s_dict)
    
    return await app_module.tpl(db, request, 'users.html', users=users_list, servers=servers)


@router.get('/my', response_class=HTMLResponse)
async def my_connections_page(request: Request, db: AsyncSession = Depends(get_db)):
    user = await app_module.get_current_user(db, request)
    if not user:
        return RedirectResponse(url='/login', status_code=302)
        
    stmt_conns = select(models.Connection).where(models.Connection.user_id == user.id).options(selectinload(models.Connection.server))
    res_conns = await db.execute(stmt_conns)
    
    conns = []
    for c in res_conns.scalars().all():
        conns.append({
            'id': c.id,
            'user_id': c.user_id,
            'server_id': c.server_id,
            'protocol': c.protocol,
            'client_id': c.client_id,
            'name': c.name,
            'created_at': str(c.created_at) if c.created_at else '',
            'server_name': c.server.name if c.server else 'Unknown'
        })
        
    return await app_module.tpl(db, request, 'my_connections.html', connections=conns)
