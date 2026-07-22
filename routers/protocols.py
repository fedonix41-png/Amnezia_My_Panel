import os
import shlex
import tempfile
from typing import Optional
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, FileResponse
from starlette.background import BackgroundTask
from pydantic import BaseModel

import app as app_module
from managers.awg_manager import AWGManager
from managers.backup_manager import BackupManager

router = APIRouter(tags=["Protocols"])


class InstallProtocolRequest(BaseModel):
    protocol: str = 'awg'
    port: str = '55424'
    install_another: Optional[bool] = False
    tls_emulation: Optional[bool] = None
    tls_domain: Optional[str] = None
    max_connections: Optional[int] = None
    socks5_username: Optional[str] = None
    socks5_password: Optional[str] = None
    adguard_mode: Optional[str] = None
    adguard_web_port: Optional[int] = None
    adguard_expose_web: Optional[bool] = None
    adguard_dot_port: Optional[int] = None
    adguard_doh_port: Optional[int] = None
    adguard_expose_dns: Optional[bool] = None
    adguard_expose_dot: Optional[bool] = None
    adguard_expose_doh: Optional[bool] = None
    nginx_domain: Optional[str] = None
    nginx_email: Optional[str] = None


class Socks5SettingsRequest(BaseModel):
    protocol: str = 'socks5'
    port: Optional[int] = None
    username: Optional[str] = None
    password: Optional[str] = None


class ProtocolRequest(BaseModel):
    protocol: str = 'awg'


class ServerConfigSaveRequest(BaseModel):
    protocol: str
    config: str


class NginxSiteSaveRequest(BaseModel):
    protocol: str = 'nginx'
    html: str = ''


class BackupDownloadRequest(BaseModel):
    protocol: str
    filename: str


@router.post('/api/servers/{server_id}/install')
async def api_install_protocol(request: Request, server_id: int, req: InstallProtocolRequest):
    if not app_module._check_admin(request):
        return JSONResponse({'error': 'Forbidden'}, status_code=403)
    try:
        data = app_module.load_data()
        if server_id >= len(data['servers']):
            return JSONResponse({'error': 'Server not found'}, status_code=404)
        if not app_module.is_valid_protocol(req.protocol):
            return JSONResponse({'error': 'Invalid protocol type'}, status_code=400)

        server = data['servers'][server_id]
        if 'protocols' not in server:
            server['protocols'] = {}
        base_protocol = app_module.protocol_base(req.protocol)
        if req.install_another:
            if base_protocol not in app_module.MULTI_INSTANCE_PROTOCOLS:
                return JSONResponse({'error': 'Multiple instances are not supported for this protocol yet'}, status_code=400)
            install_protocol = app_module.next_protocol_key(server.get('protocols', {}), base_protocol)
        else:
            install_protocol = req.protocol
        install_base = app_module.protocol_base(install_protocol)

        ssh = app_module.get_ssh(server)
        ssh.connect()
        docker_install_log = app_module.ensure_docker_installed(ssh)
        manager = app_module.get_protocol_manager(ssh, install_protocol)

        if install_base == 'telemt':
            result = manager.install_protocol(
                protocol_type=install_protocol,
                port=req.port,
                tls_emulation=req.tls_emulation if req.tls_emulation is not None else True,
                tls_domain=req.tls_domain,
                max_connections=req.max_connections if req.max_connections is not None else 0
            )
        elif install_base == 'xray':
            result = manager.install_protocol(port=req.port)
        elif install_base == 'wireguard':
            result = manager.install_protocol(port=req.port)
        elif install_base == 'socks5':
            result = manager.install_protocol(
                protocol_type=install_protocol,
                port=req.port,
                username=req.socks5_username,
                password=req.socks5_password,
            )
        elif install_base == 'adguard':
            result = manager.install_protocol(
                protocol_type='adguard',
                mode=req.adguard_mode or 'sidebyside',
                web_port=req.adguard_web_port,
                expose_web=bool(req.adguard_expose_web),
                dns_port=req.port,
                dot_port=req.adguard_dot_port,
                doh_port=req.adguard_doh_port,
                expose_dns=bool(req.adguard_expose_dns),
                expose_dot=bool(req.adguard_expose_dot),
                expose_doh=bool(req.adguard_expose_doh),
            )
        elif install_base == 'nginx':
            result = manager.install_protocol(
                protocol_type='nginx',
                port=req.port,
                domain=req.nginx_domain,
                email=req.nginx_email,
            )
        else:
            result = manager.install_protocol(install_protocol, port=req.port)

        if not isinstance(result, dict):
            result = {'status': 'success', 'message': str(result)}
        if docker_install_log:
            result.setdefault('log', [])
            result['log'].insert(0, docker_install_log)
        if result.get('status') == 'error' or result.get('error'):
            ssh.disconnect()
            return JSONResponse({'error': result.get('message') or result.get('error') or 'Installation failed'}, status_code=400)

        proto_record = {
            'installed': True,
            'port': req.port,
            'awg_params': result.get('awg_params', {}),
        }
        if install_base == 'adguard':
            proto_record['mode'] = result.get('mode')
            proto_record['internal_ip'] = result.get('internal_ip')
            proto_record['web_port'] = result.get('web_port')
            proto_record['expose_web'] = result.get('expose_web')
        if install_base == 'nginx':
            proto_record['domain'] = result.get('domain')
            proto_record['email'] = result.get('email')
            proto_record['site_url'] = result.get('site_url')
        proto_record['base_protocol'] = install_base
        proto_record['instance'] = app_module.protocol_instance(install_protocol)
        proto_record['display_name'] = app_module.protocol_display_name(install_protocol)
        proto_record['container_name'] = app_module.protocol_container_name(install_protocol)
        server['protocols'][install_protocol] = proto_record
        result['protocol'] = install_protocol
        result['base_protocol'] = install_base
        result['display_name'] = proto_record['display_name']
        result['container_name'] = proto_record['container_name']
        app_module.save_data(data)
        ssh.disconnect()
        return result
    except Exception as e:
        app_module.logger.exception("Error installing protocol")
        return JSONResponse({'error': str(e)}, status_code=500)


@router.get('/api/servers/{server_id}/socks5/credentials')
async def api_socks5_get_credentials(request: Request, server_id: int, protocol: str = 'socks5'):
    if not app_module._check_admin(request):
        return JSONResponse({'error': 'Forbidden'}, status_code=403)
    try:
        data = app_module.load_data()
        if server_id >= len(data['servers']):
            return JSONResponse({'error': 'Server not found'}, status_code=404)
        server = data['servers'][server_id]
        protocol = protocol if app_module.is_valid_protocol(protocol) and app_module.protocol_base(protocol) == 'socks5' else 'socks5'
        ssh = app_module.get_ssh(server)
        ssh.connect()
        manager = app_module.get_protocol_manager(ssh, protocol)
        creds = manager.get_credentials()
        ssh.disconnect()
        return {'status': 'success', **creds}
    except Exception as e:
        app_module.logger.exception("Error reading SOCKS5 credentials")
        return JSONResponse({'error': str(e)}, status_code=500)


@router.post('/api/servers/{server_id}/socks5/credentials')
async def api_socks5_update_credentials(request: Request, server_id: int, req: Socks5SettingsRequest):
    if not app_module._check_admin(request):
        return JSONResponse({'error': 'Forbidden'}, status_code=403)
    try:
        data = app_module.load_data()
        if server_id >= len(data['servers']):
            return JSONResponse({'error': 'Server not found'}, status_code=404)
        server = data['servers'][server_id]
        protocol = req.protocol if app_module.is_valid_protocol(req.protocol) and app_module.protocol_base(req.protocol) == 'socks5' else 'socks5'
        ssh = app_module.get_ssh(server)
        ssh.connect()
        manager = app_module.get_protocol_manager(ssh, protocol)
        result = manager.update_credentials(
            port=req.port, username=req.username, password=req.password
        )
        ssh.disconnect()
        if result.get('status') == 'success' and result.get('port'):
            srv_proto = server.setdefault('protocols', {}).setdefault(protocol, {})
            srv_proto['port'] = str(result['port'])
            srv_proto['installed'] = True
            srv_proto['base_protocol'] = app_module.protocol_base(protocol)
            srv_proto['instance'] = app_module.protocol_instance(protocol)
            srv_proto['display_name'] = app_module.protocol_display_name(protocol)
            srv_proto['container_name'] = app_module.protocol_container_name(protocol)
            app_module.save_data(data)
        return result
    except Exception as e:
        app_module.logger.exception("Error updating SOCKS5 credentials")
        return JSONResponse({'error': str(e)}, status_code=500)


@router.post('/api/servers/{server_id}/uninstall')
async def api_uninstall_protocol(request: Request, server_id: int, req: ProtocolRequest):
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
        base = app_module.protocol_base(req.protocol)
        if base in ('xray', 'wireguard'):
            manager.remove_container()
        else:
            manager.remove_container(req.protocol)
        if req.protocol in server.get('protocols', {}):
            del server['protocols'][req.protocol]
            app_module.save_data(data)
        ssh.disconnect()
        return {'status': 'success'}
    except Exception as e:
        app_module.logger.exception("Error uninstalling protocol")
        return JSONResponse({'error': str(e)}, status_code=500)


@router.post('/api/servers/{server_id}/backups')
async def api_protocol_backups_list(request: Request, server_id: int, req: ProtocolRequest):
    if not app_module._check_admin(request):
        return JSONResponse({'error': 'Forbidden'}, status_code=403)
    if not app_module.is_valid_protocol(req.protocol):
        return JSONResponse({'error': 'Unknown protocol'}, status_code=400)
    ssh = None
    try:
        data = app_module.load_data()
        if server_id >= len(data['servers']):
            return JSONResponse({'error': 'Server not found'}, status_code=404)
        server = data['servers'][server_id]
        ssh = app_module.get_ssh(server)
        ssh.connect()
        result = BackupManager(ssh).list_backups(req.protocol)
        if result.get('status') == 'error':
            return JSONResponse({'error': result.get('message', 'Failed to list backups')}, status_code=500)
        return result
    except Exception as e:
        app_module.logger.exception("Error listing protocol backups")
        return JSONResponse({'error': str(e)}, status_code=500)
    finally:
        if ssh:
            ssh.disconnect()


@router.post('/api/servers/{server_id}/backups/create')
async def api_protocol_backup_create(request: Request, server_id: int, req: ProtocolRequest):
    if not app_module._check_admin(request):
        return JSONResponse({'error': 'Forbidden'}, status_code=403)
    if not app_module.is_valid_protocol(req.protocol):
        return JSONResponse({'error': 'Unknown protocol'}, status_code=400)
    ssh = None
    try:
        data = app_module.load_data()
        if server_id >= len(data['servers']):
            return JSONResponse({'error': 'Server not found'}, status_code=404)
        container = app_module.protocol_container_name(req.protocol)
        if not container:
            return JSONResponse({'error': 'Unknown protocol'}, status_code=400)
        server = data['servers'][server_id]
        ssh = app_module.get_ssh(server)
        ssh.connect()
        result = BackupManager(ssh).create_backup(req.protocol, container)
        if result.get('status') == 'error':
            return JSONResponse({'error': result.get('message', 'Failed to create backup')}, status_code=500)
        return result
    except Exception as e:
        app_module.logger.exception("Error creating protocol backup")
        return JSONResponse({'error': str(e)}, status_code=500)
    finally:
        if ssh:
            ssh.disconnect()


@router.post('/api/servers/{server_id}/backups/download')
async def api_protocol_backup_download(request: Request, server_id: int, req: BackupDownloadRequest):
    if not app_module._check_admin(request):
        return JSONResponse({'error': 'Forbidden'}, status_code=403)
    if not app_module.is_valid_protocol(req.protocol):
        return JSONResponse({'error': 'Unknown protocol'}, status_code=400)
    manager = BackupManager(None)
    safe_proto = manager.safe_protocol(req.protocol)
    filename = manager.safe_filename(req.filename)
    if not filename:
        return JSONResponse({'error': 'Invalid backup filename'}, status_code=400)
    ssh = None
    tmp_path = None
    tmp_remote = f'/tmp/{filename}'
    remote_path = f'{manager.BACKUP_ROOT}/{safe_proto}/{filename}'
    try:
        data = app_module.load_data()
        if server_id >= len(data['servers']):
            return JSONResponse({'error': 'Server not found'}, status_code=404)
        server = data['servers'][server_id]
        ssh = app_module.get_ssh(server)
        ssh.connect()
        quoted_remote = shlex.quote(remote_path)
        quoted_tmp = shlex.quote(tmp_remote)
        _, err, code = ssh.run_sudo_command(
            f"test -f {quoted_remote} && cp {quoted_remote} {quoted_tmp} && chmod 0644 {quoted_tmp}"
        )
        if code != 0:
            return JSONResponse({'error': err or 'Backup not found'}, status_code=404)
        fd, tmp_path = tempfile.mkstemp(prefix='amnezia-backup-', suffix='.tar.gz')
        os.close(fd)
        sftp = ssh.client.open_sftp()
        try:
            sftp.get(tmp_remote, tmp_path)
        finally:
            sftp.close()
            ssh.run_sudo_command(f"rm -f {quoted_tmp}")
            ssh.disconnect()
            ssh = None
        return FileResponse(
            tmp_path,
            media_type='application/gzip',
            filename=filename,
            background=BackgroundTask(lambda p=tmp_path: os.path.exists(p) and os.remove(p)),
        )
    except Exception as e:
        app_module.logger.exception("Error downloading protocol backup")
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except Exception:
                pass
        return JSONResponse({'error': str(e)}, status_code=500)
    finally:
        if ssh:
            ssh.disconnect()


@router.post('/api/servers/{server_id}/container/toggle')
async def api_container_toggle(request: Request, server_id: int, req: ProtocolRequest):
    if not app_module._check_admin(request):
        return JSONResponse({'error': 'Forbidden'}, status_code=403)
    try:
        data = app_module.load_data()
        if server_id >= len(data['servers']):
            return JSONResponse({'error': 'Server not found'}, status_code=404)
        container = app_module.protocol_container_name(req.protocol)
        if not container:
            return JSONResponse({'error': 'Unknown protocol'}, status_code=400)
        server = data['servers'][server_id]
        ssh = app_module.get_ssh(server)
        ssh.connect()
        out, _, _ = ssh.run_sudo_command(
            f"docker inspect -f '{{{{.State.Running}}}}' {container} 2>/dev/null"
        )
        is_running = out.strip().lower() == 'true'
        if is_running:
            ssh.run_sudo_command(f"docker stop {container}")
            action = 'stopped'
        else:
            ssh.run_sudo_command(f"docker start {container}")
            action = 'started'
        ssh.disconnect()
        return {'status': 'success', 'action': action, 'container': container}
    except Exception as e:
        app_module.logger.exception("Error toggling container")
        return JSONResponse({'error': str(e)}, status_code=500)


@router.post('/api/servers/{server_id}/server_config')
async def api_server_config(request: Request, server_id: int, req: ProtocolRequest):
    if not app_module._check_admin(request):
        return JSONResponse({'error': 'Forbidden'}, status_code=403)
    try:
        data = app_module.load_data()
        if server_id >= len(data['servers']):
            return JSONResponse({'error': 'Server not found'}, status_code=404)
        server = data['servers'][server_id]
        ssh = app_module.get_ssh(server)
        ssh.connect()
        if app_module.protocol_base(req.protocol) == 'xray':
            from managers.xray_manager import XrayManager
            mgr = XrayManager(ssh, req.protocol)
            data_json = mgr._get_server_json()
            import json as _json
            config = _json.dumps(data_json, indent=2, ensure_ascii=False) if data_json else ''
        elif app_module.protocol_base(req.protocol) == 'telemt':
            from managers.telemt_manager import TelemtManager
            mgr = TelemtManager(ssh, req.protocol)
            config = mgr._get_server_config()
        elif app_module.protocol_base(req.protocol) == 'wireguard':
            from managers.wireguard_manager import WireGuardManager
            mgr = WireGuardManager(ssh)
            config = mgr._get_server_config()
        elif app_module.protocol_base(req.protocol) == 'nginx':
            from managers.nginx_manager import NginxManager
            mgr = NginxManager(ssh, req.protocol)
            config = mgr._get_server_config(req.protocol)
        else:
            mgr = AWGManager(ssh)
            config = mgr._get_server_config(req.protocol)
        ssh.disconnect()
        return {'config': config}
    except Exception as e:
        app_module.logger.exception("Error getting server config")
        return JSONResponse({'error': str(e)}, status_code=500)


@router.post('/api/servers/{server_id}/server_config/save')
async def api_server_config_save(request: Request, server_id: int, req: ServerConfigSaveRequest):
    if not app_module._check_admin(request):
        return JSONResponse({'error': 'Forbidden'}, status_code=403)
    try:
        data = app_module.load_data()
        if server_id >= len(data['servers']):
            return JSONResponse({'error': 'Server not found'}, status_code=404)
        server = data['servers'][server_id]
        ssh = app_module.get_ssh(server)
        ssh.connect()
        if app_module.protocol_base(req.protocol) == 'xray':
            from managers.xray_manager import XrayManager
            mgr = XrayManager(ssh, req.protocol)
            import json as _json
            try:
                data_json = _json.loads(req.config)
            except Exception as e:
                ssh.disconnect()
                return JSONResponse({'error': f'Invalid JSON format: {str(e)}'}, status_code=400)
            mgr._save_server_json(data_json)
        elif app_module.protocol_base(req.protocol) == 'telemt':
            from managers.telemt_manager import TelemtManager
            mgr = TelemtManager(ssh, req.protocol)
            mgr.save_server_config(req.protocol, req.config)
        elif app_module.protocol_base(req.protocol) == 'wireguard':
            from managers.wireguard_manager import WireGuardManager
            mgr = WireGuardManager(ssh)
            mgr.save_server_config(req.config)
        elif app_module.protocol_base(req.protocol) == 'nginx':
            from managers.nginx_manager import NginxManager
            mgr = NginxManager(ssh, req.protocol)
            mgr.save_server_config(req.protocol, req.config)
        else:
            mgr = AWGManager(ssh)
            mgr.save_server_config(req.protocol, req.config)
        ssh.disconnect()
        return {'status': 'success'}
    except Exception as e:
        app_module.logger.exception("Error saving server config")
        return JSONResponse({'error': str(e)}, status_code=500)


@router.post('/api/servers/{server_id}/nginx/site')
async def api_nginx_site_get(request: Request, server_id: int, req: ProtocolRequest):
    if not app_module._check_admin(request):
        return JSONResponse({'error': 'Forbidden'}, status_code=403)
    try:
        data = app_module.load_data()
        if server_id >= len(data['servers']):
            return JSONResponse({'error': 'Server not found'}, status_code=404)
        if app_module.protocol_base(req.protocol) != 'nginx':
            return JSONResponse({'error': 'Invalid protocol type'}, status_code=400)
        server = data['servers'][server_id]
        ssh = app_module.get_ssh(server)
        ssh.connect()
        from managers.nginx_manager import NginxManager
        mgr = NginxManager(ssh, req.protocol)
        html = mgr.get_site_index(req.protocol)
        ssh.disconnect()
        return {'status': 'success', 'html': html}
    except Exception as e:
        app_module.logger.exception("Error getting NGINX site")
        return JSONResponse({'error': str(e)}, status_code=500)


@router.post('/api/servers/{server_id}/nginx/site/save')
async def api_nginx_site_save(request: Request, server_id: int, req: NginxSiteSaveRequest):
    if not app_module._check_admin(request):
        return JSONResponse({'error': 'Forbidden'}, status_code=403)
    try:
        data = app_module.load_data()
        if server_id >= len(data['servers']):
            return JSONResponse({'error': 'Server not found'}, status_code=404)
        if app_module.protocol_base(req.protocol) != 'nginx':
            return JSONResponse({'error': 'Invalid protocol type'}, status_code=400)
        server = data['servers'][server_id]
        ssh = app_module.get_ssh(server)
        ssh.connect()
        from managers.nginx_manager import NginxManager
        mgr = NginxManager(ssh, req.protocol)
        mgr.save_site_index(req.protocol, req.html)
        ssh.disconnect()
        return {'status': 'success'}
    except Exception as e:
        app_module.logger.exception("Error saving NGINX site")
        return JSONResponse({'error': str(e)}, status_code=500)
