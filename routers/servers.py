import asyncio
import time
from typing import Optional, List
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

import app as app_module
from managers.ssh_manager import SSHManager
from managers.secrets import encrypt_secret, decrypt_secret
from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from database.session import get_db
from database.repositories import ServerRepository
from database import models
from services.server_stats import collect_host_stats

router = APIRouter(tags=["Servers"])


class AddServerRequest(BaseModel):
    host: str = ''
    ssh_port: int = 22
    username: str = ''
    password: str = ''
    private_key: str = ''
    name: str = ''


class EditServerRequest(BaseModel):
    name: str = ''
    host: str = ''
    ssh_port: int = 22
    username: str = ''
    password: Optional[str] = None
    private_key: Optional[str] = None


class ReorderServersRequest(BaseModel):
    order: List[int]


@router.post('/api/servers/add')
async def api_add_server(request: Request, req: AddServerRequest, db: AsyncSession = Depends(get_db)):
    actor = await app_module._check_admin(db, request)
    if not actor:
        return JSONResponse({'error': 'Forbidden'}, status_code=403)
    try:
        host = req.host.strip()
        username = req.username.strip()
        name = req.name.strip() or host
        if not host or not username:
            return JSONResponse({'error': 'Host and username are required'}, status_code=400)
        if not req.password and not req.private_key:
            return JSONResponse({'error': 'Password or SSH key is required'}, status_code=400)

        ssh = SSHManager(host, req.ssh_port, username, req.password, req.private_key)
        try:
            await asyncio.to_thread(ssh.connect)
            server_info = await asyncio.to_thread(ssh.test_connection)
            host_fingerprint = ssh.host_fingerprint
        except Exception as e:
            return JSONResponse({'error': f'Connection failed: {str(e)}'}, status_code=400)
        finally:
            try:
                await asyncio.to_thread(ssh.disconnect)
            except Exception:
                pass


        # Get the max display_order
        stmt = select(models.Server).order_by(models.Server.display_order.desc()).limit(1)
        res = await db.execute(stmt)
        max_server = res.scalar_one_or_none()
        next_order = (max_server.display_order + 1) if max_server else 0

        server = models.Server(
            name=name, host=host, ssh_port=req.ssh_port,
            username=username, password=encrypt_secret(req.password),
            private_key=encrypt_secret(req.private_key), server_info=server_info,
            host_fingerprint=host_fingerprint, display_order=next_order
        )
        db.add(server)
        await db.commit()
        await db.refresh(server)

        app_module.capture_posthog_event(request, actor, "server_added")
        return {'status': 'success', 'server_id': server.id, 'server_info': server_info}
    except Exception as e:
        app_module.logger.exception("Error adding server")
        return JSONResponse({'error': str(e)}, status_code=500)


@router.post('/api/servers/{server_id}/edit')
async def api_edit_server(request: Request, server_id: int, req: EditServerRequest, db: AsyncSession = Depends(get_db)):
    if not await app_module._check_admin(db, request):
        return JSONResponse({'error': 'Forbidden'}, status_code=403)
    try:
        server = await ServerRepository.get_by_id(db, server_id)
        if not server:
            return JSONResponse({'error': 'Server not found'}, status_code=404)

        new_host = (req.host or '').strip() or server.host
        new_user = (req.username or '').strip() or server.username
        new_port = int(req.ssh_port or server.ssh_port or 22)
        new_name = (req.name or '').strip() or server.name or new_host

        if req.private_key:
            plain_pass, plain_key = '', req.private_key
        elif req.password:
            plain_pass, plain_key = req.password, ''
        else:
            plain_pass = server.plain_password
            plain_key = server.plain_private_key

        if not plain_pass and not plain_key:
            return JSONResponse({'error': 'Password or SSH key is required'}, status_code=400)

        expected_fp = server.host_fingerprint if new_host == server.host else None
        ssh = SSHManager(new_host, new_port, new_user, plain_pass, plain_key,
                         host_fingerprint=expected_fp)
        try:
            await asyncio.to_thread(ssh.connect)
            server_info = await asyncio.to_thread(ssh.test_connection)
            host_fingerprint = ssh.host_fingerprint
        except Exception as e:
            return JSONResponse({'error': f'Connection failed: {e}'}, status_code=400)
        finally:
            try:
                await asyncio.to_thread(ssh.disconnect)
            except Exception:
                pass


        server.name = new_name
        server.host = new_host
        server.ssh_port = new_port
        server.username = new_user
        server.password = encrypt_secret(plain_pass)
        server.private_key = encrypt_secret(plain_key)
        server.host_fingerprint = host_fingerprint
        server.server_info = server_info
        await db.commit()
        return {'status': 'success', 'server_info': server_info}
    except Exception as e:
        app_module.logger.exception("Error editing server")
        return JSONResponse({'error': str(e)}, status_code=500)


@router.get('/api/servers/{server_id}/ping')
async def api_server_ping(request: Request, server_id: int, db: AsyncSession = Depends(get_db)):
    if not await app_module._check_admin(db, request):
        return JSONResponse({'error': 'Forbidden'}, status_code=403)
    server = await ServerRepository.get_by_id(db, server_id)
    if not server:
        return JSONResponse({'error': 'Server not found'}, status_code=404)
    host = server.host
    port = int(server.ssh_port or 22)

    t0 = time.perf_counter()
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), timeout=2.0
        )
        ms = round((time.perf_counter() - t0) * 1000)
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass
        return {'alive': True, 'ms': ms}
    except asyncio.TimeoutError:
        return {'alive': False, 'error': 'timeout', 'ms': None}
    except Exception as e:
        return {'alive': False, 'error': str(e), 'ms': None}


@router.post('/api/servers/{server_id}/reset-fingerprint')
async def api_reset_server_fingerprint(request: Request, server_id: int, db: AsyncSession = Depends(get_db)):
    if not await app_module._check_admin(db, request):
        return JSONResponse({'error': 'Forbidden'}, status_code=403)
    server = await ServerRepository.get_by_id(db, server_id)
    if not server:
        return JSONResponse({'error': 'Server not found'}, status_code=404)
    server.host_fingerprint = None
    await db.commit()
    return {'status': 'success'}


@router.post('/api/servers/reorder')
async def api_reorder_servers(request: Request, req: ReorderServersRequest, db: AsyncSession = Depends(get_db)):
    if not await app_module._check_admin(db, request):
        return JSONResponse({'error': 'Forbidden'}, status_code=403)
    
    servers = await ServerRepository.get_all(db)
    
    n = len(servers)
    order = req.order or []
    if len(order) != n or sorted(order) != list(range(n)):
        return JSONResponse(
            {'error': f'Order must be a permutation of indices 0..{n - 1}'},
            status_code=400,
        )
    
    for new_index, old_index in enumerate(order):
        servers[old_index].display_order = new_index
        
    await db.commit()
    return {'status': 'success'}


@router.post('/api/servers/{server_id}/delete')
async def api_delete_server(request: Request, server_id: int, db: AsyncSession = Depends(get_db)):
    actor = await app_module._check_admin(db, request)
    if not actor:
        return JSONResponse({'error': 'Forbidden'}, status_code=403)
    try:
        server = await ServerRepository.get_by_id(db, server_id)
        if not server:
            return JSONResponse({'error': 'Server not found'}, status_code=404)
            
        await db.delete(server)
        await db.commit()
        app_module.capture_posthog_event(request, actor, "server_deleted")
        return {'status': 'success'}
    except Exception as e:
        return JSONResponse({'error': str(e)}, status_code=500)


@router.post('/api/servers/{server_id}/reboot')
async def api_reboot_server(request: Request, server_id: int, db: AsyncSession = Depends(get_db)):
    actor = await app_module._check_admin(db, request)
    if not actor:
        return JSONResponse({'error': 'Forbidden'}, status_code=403)
    try:
        server = await ServerRepository.get_by_id(db, server_id)
        if not server:
            return JSONResponse({'error': 'Server not found'}, status_code=404)
            
        ssh = app_module.get_ssh(server, db)
        try:
            await asyncio.to_thread(ssh.connect)
            try:
                await asyncio.to_thread(ssh.run_sudo_command, "nohup reboot > /dev/null 2>&1 &")
            except Exception:
                pass
        finally:
            try:
                await asyncio.to_thread(ssh.disconnect)
            except Exception:
                pass
        app_module.capture_posthog_event(request, actor, "server_reboot_requested")
        return {'status': 'success'}
    except Exception as e:
        app_module.logger.exception("Error rebooting server")
        return JSONResponse({'error': str(e)}, status_code=500)


@router.post('/api/servers/{server_id}/clear')
async def api_clear_server(request: Request, server_id: int, db: AsyncSession = Depends(get_db)):
    if not await app_module._check_admin(db, request):
        return JSONResponse({'error': 'Forbidden'}, status_code=403)
    try:
        server = await ServerRepository.get_by_id(db, server_id)
        if not server:
            return JSONResponse({'error': 'Server not found'}, status_code=404)
            
        ssh = app_module.get_ssh(server, db)
        try:
            await asyncio.to_thread(ssh.connect)
            cleanup_script = r"""
for c in $(docker ps -a --format '{{.Names}}' 2>/dev/null | grep -E '^(amnezia-|telemt$)'); do
    docker stop "$c" >/dev/null 2>&1 || true
    docker rm -fv "$c" >/dev/null 2>&1 || true
done

for img in $(docker images --format '{{.Repository}}:{{.Tag}}' 2>/dev/null | grep -E '^(amnezia-|amneziavpn/|telemt:)'); do
    docker rmi -f "$img" >/dev/null 2>&1 || true
done

docker network rm amnezia-dns-net >/dev/null 2>&1 || true
rm -rf /opt/amnezia
"""
            await asyncio.to_thread(ssh.run_sudo_script, cleanup_script, timeout=120)

            # Remove all protocols from DB
            stmt = select(models.ServerProtocol).where(models.ServerProtocol.server_id == server_id)
            res = await db.execute(stmt)
            for p in res.scalars().all():
                await db.delete(p)
                
            await db.commit()
            return {'status': 'success'}
        finally:
            try:
                await asyncio.to_thread(ssh.disconnect)
            except Exception:
                pass

        return {'status': 'success'}
    except Exception as e:
        app_module.logger.exception("Error clearing server")
        return JSONResponse({'error': str(e)}, status_code=500)


@router.post('/api/servers/{server_id}/stats')
async def api_server_stats(request: Request, server_id: int, cached: bool = False, db: AsyncSession = Depends(get_db)):
    if not await app_module._check_admin(db, request):
        return JSONResponse({'error': 'Forbidden'}, status_code=403)
    
    if cached:
        return {'alive': True, 'error': 'cached'}

    try:
        server = await ServerRepository.get_by_id(db, server_id)
        if not server:
            return JSONResponse({'error': 'Server not found'}, status_code=404)
        ssh = app_module.get_ssh(server, db)
        def _fetch_stats(ssh_conn):
            try:
                ssh_conn.connect()
                # One marker-framed batched probe instead of ~5 individual
                # SSH commands; partial results survive a truncated reply.
                return collect_host_stats(ssh_conn)
            finally:
                try:
                    ssh_conn.disconnect()
                except Exception:
                    pass

        return await asyncio.to_thread(_fetch_stats, ssh)
    except Exception as e:
        app_module.logger.exception("Error getting server stats")
        return JSONResponse({'error': str(e)}, status_code=500)


@router.post('/api/servers/{server_id}/check')
async def api_check_server(request: Request, server_id: int, cached: bool = False, db: AsyncSession = Depends(get_db)):
    if not await app_module._check_admin(db, request):
        return JSONResponse({'error': 'Forbidden'}, status_code=403)
    try:
        server = await ServerRepository.get_by_id(db, server_id)
        if not server:
            return JSONResponse({'error': 'Server not found'}, status_code=404)

        stmt = select(models.ServerProtocol).where(models.ServerProtocol.server_id == server_id)
        res = await db.execute(stmt)
        db_protocols = {p.protocol_type: p for p in res.scalars().all()}

        if cached:
            status = {'connection': 'ok', 'docker_installed': True, 'protocols': {}}
            for proto, p in db_protocols.items():
                info = {
                    'protocol': proto,
                    'port': p.port,
                    'base_protocol': p.base_protocol or app_module.protocol_base(proto),
                    'instance': p.instance or app_module.protocol_instance(proto),
                    'display_name': p.display_name or app_module.protocol_display_name(proto),
                    'container_name': p.container_name or app_module.protocol_container_name(proto),
                    'container_exists': True,
                    'container_running': True,
                    'status_preserved': True
                }
                if p.extra_params:
                    if 'awg_params' in p.extra_params:
                        info['awg_params'] = p.extra_params['awg_params']
                    settings = p.extra_params.get('settings', {})
                    for k, v in settings.items():
                        if k == 'expose_web':
                            info['web_exposed'] = v
                        else:
                            info[k] = v
                status['protocols'][proto] = info
            return status

        ssh = app_module.get_ssh(server, db)
        try:
            await asyncio.to_thread(ssh.connect)
            manager = app_module.get_protocol_manager(ssh, 'awg')
            docker_installed = await asyncio.to_thread(manager.check_docker_installed)
            status = {'connection': 'ok', 'docker_installed': docker_installed, 'protocols': {}}
            
            changed = False

            def merge_saved_protocol_status(proto, result=None, error=None):
                db_proto = db_protocols.get(proto)
                merged = dict(result or {})
                merged.setdefault('protocol', proto)
                if error:
                    merged['error'] = error
                if db_proto:
                    if not merged.get('port') and db_proto.port:
                        merged['port'] = db_proto.port
                    extra_params = db_proto.extra_params or {}
                    if extra_params.get('awg_params') and not merged.get('awg_params'):
                        merged['awg_params'] = extra_params['awg_params']
                    # Fallback for a stopped container: version fields were
                    # persisted while it was last seen running.
                    for key in ('protocol_version', 'header_protection'):
                        if merged.get(key) is None and extra_params.get(key) is not None:
                            merged[key] = extra_params[key]
                    merged['base_protocol'] = db_proto.base_protocol or app_module.protocol_base(proto)
                    merged['instance'] = db_proto.instance or app_module.protocol_instance(proto)
                    merged['display_name'] = db_proto.display_name or app_module.protocol_display_name(proto)
                    merged['container_name'] = db_proto.container_name or app_module.protocol_container_name(proto)
                    
                    settings = extra_params.get('settings', {})
                    if app_module.protocol_base(proto) == 'adguard':
                        for key in ('web_port', 'mode', 'internal_ip', 'expose_web'):
                            if settings.get(key) is not None:
                                if key == 'web_port' and merged.get('web_exposed'):
                                    continue
                                merged[key] = settings[key]
                        if 'expose_web' in settings and 'web_exposed' not in merged:
                            merged['web_exposed'] = bool(settings.get('expose_web'))
                    if app_module.protocol_base(proto) == 'nginx':
                        for key in ('domain', 'email', 'site_url'):
                            if settings.get(key) is not None:
                                merged[key] = settings[key]
                else:
                    merged['base_protocol'] = app_module.protocol_base(proto)
                    merged['instance'] = app_module.protocol_instance(proto)
                    merged['display_name'] = app_module.protocol_display_name(proto)
                    merged['container_name'] = app_module.protocol_container_name(proto)
                return merged

            def should_preserve_saved_protocol(proto, result=None, err=None):
                if proto not in db_protocols:
                    return False
                if app_module.protocol_base(proto) in app_module.MULTI_INSTANCE_PROTOCOLS and app_module.protocol_instance(proto) > 1:
                    return True
                if err or (result and result.get('error')):
                    return True
                return False

            def check_proto(proto):
                try:
                    p_manager = app_module.get_protocol_manager(ssh, proto)
                    result = app_module._manager_call(p_manager, 'get_server_status', proto)
                    return proto, merge_saved_protocol_status(proto, result), None
                except Exception as e:
                    return proto, merge_saved_protocol_status(proto, {}, str(e)), str(e)

            protocols_to_check = list(dict.fromkeys(app_module.BASE_PROTOCOLS + list(db_protocols.keys())))
            for proto in protocols_to_check:
                proto, result, err = await asyncio.to_thread(check_proto, proto)
                status['protocols'][proto] = result
                if err:
                    continue
                if not result.get('container_exists') and proto in db_protocols:
                    if should_preserve_saved_protocol(proto, result, err):
                        status['protocols'][proto]['container_exists'] = True
                        status['protocols'][proto].setdefault('container_running', False)
                        status['protocols'][proto]['status_preserved'] = True
                    else:
                        await db.delete(db_protocols[proto])
                        del db_protocols[proto]
                        changed = True
                    continue
                if proto in db_protocols:
                    # Persist new AWG status fields for the stopped-container fallback.
                    if app_module.protocol_base(proto) in ('awg', 'awg2', 'awg3', 'awg_legacy'):
                        new_extra = dict(db_protocols[proto].extra_params or {})
                        extra_changed = False
                        for key in ('protocol_version', 'header_protection'):
                            if result.get(key) is not None and new_extra.get(key) != result[key]:
                                new_extra[key] = result[key]
                                extra_changed = True
                        if extra_changed:
                            db_protocols[proto].extra_params = new_extra
                            changed = True
                    continue
                if result.get('container_exists'):
                    if proto not in db_protocols:
                        new_proto = models.ServerProtocol(
                            server_id=server.id,
                            protocol_type=proto,
                            port=int(result.get('port', 55424)) if result.get('port') else 55424,
                            base_protocol=app_module.protocol_base(proto),
                            instance=app_module.protocol_instance(proto),
                            display_name=app_module.protocol_display_name(proto),
                            container_name=app_module.protocol_container_name(proto)
                        )
                        settings = {}
                        if app_module.protocol_base(proto) == 'adguard':
                            settings.update({
                                'mode': result.get('mode'),
                                'internal_ip': result.get('internal_ip'),
                                'web_port': result.get('web_port'),
                                'expose_web': result.get('web_exposed'),
                            })
                        if app_module.protocol_base(proto) == 'nginx':
                            settings.update({
                                'domain': result.get('domain'),
                                'email': result.get('email'),
                                'site_url': result.get('site_url'),
                            })
                        new_proto.extra_params = {
                            "awg_params": result.get('awg_params', {}),
                            "protocol_version": result.get('protocol_version'),
                            "header_protection": result.get('header_protection'),
                            "settings": settings
                        }
                        db.add(new_proto)
                        db_protocols[proto] = new_proto
                        changed = True
                    
            if changed:
                await db.commit()
                
            return status
        finally:
            try:
                await asyncio.to_thread(ssh.disconnect)
            except Exception:
                pass
    except Exception as e:
        app_module.logger.exception("Error checking server")
        return JSONResponse({'error': str(e), 'connection': 'failed'}, status_code=500)
