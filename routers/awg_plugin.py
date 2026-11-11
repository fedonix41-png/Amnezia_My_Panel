"""Admin API for safe updates of the AmneziaWG components (WP3.2).

Ports the upstream ``app.py`` wiring for ``managers/awg_plugin_updater.py``:
status/changelog/update/cancel/operation-status endpoints. Updates run as
background asyncio tasks with an operation log and step/percent progress; the
registry survives restarts via a JSON file next to ``DATA_FILE`` (the module
itself never touches it — the caller owns persistence and authorization).

No UI is ported; these endpoints are API-only.
"""

import asyncio
import json
import os
import uuid
from datetime import datetime

from fastapi import APIRouter, Request, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

import app as app_module
from database.session import get_db
from database.repositories import ServerRepository
from managers.awg_plugin_updater import (
    COMPONENTS as AWG_PLUGIN_COMPONENTS,
    TOTAL_STEPS as AWG_PLUGIN_TOTAL_STEPS,
    AwgPluginUpdater,
    PluginUpdateError,
    stage_step as awg_plugin_stage_step,
)

router = APIRouter(tags=["AWG Plugin"])

AWG_PLUGIN_OP_TTL = 60 * 60
AWG_PLUGIN_OP_KEEP = 20

_awg_plugin_ops = {}
_awg_plugin_active = {}
_awg_plugin_loaded = {'done': False}
# Strong references to running update tasks: the event loop only keeps weak
# refs, so without this the task can be garbage-collected before it starts.
_awg_plugin_tasks = set()


class AwgPluginUpdateRequest(BaseModel):
    component: str
    target: str | None = None
    allow_interrupt: bool = False


def _awg_plugin_store_path():
    return os.path.join(os.path.dirname(app_module.DATA_FILE), 'awg_plugin_ops.json')


def _awg_plugin_save():
    path = _awg_plugin_store_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w', encoding='utf-8') as handle:
            json.dump(list(_awg_plugin_ops.values()), handle, ensure_ascii=False)
    except OSError:
        app_module.logger.warning("Could not persist AWG plugin operations", exc_info=True)


def _awg_plugin_load():
    if _awg_plugin_loaded['done']:
        return
    _awg_plugin_loaded['done'] = True
    path = _awg_plugin_store_path()
    try:
        with open(path, encoding='utf-8') as handle:
            ops = json.load(handle)
    except (OSError, ValueError):
        return
    for op in ops:
        if not isinstance(op, dict) or 'id' not in op:
            continue
        if op.get('state') == 'running':
            # The task itself did not survive the restart.
            op['state'] = 'failed'
            op['error'] = {'code': 'restart', 'message': 'panel restarted during the update'}
            op.setdefault('finished_ts', datetime.now().timestamp())
        _awg_plugin_ops[op['id']] = op
    _awg_plugin_prune()


async def _awg_plugin_updater(server_id: int, db: AsyncSession) -> AwgPluginUpdater:
    server = await ServerRepository.get_by_id(db, server_id)
    if not server:
        raise PluginUpdateError('server_not_found', 'Server not found')
    return AwgPluginUpdater(app_module.get_ssh(server, db))


def _awg_plugin_error(exc, status_code=400):
    return JSONResponse({'error': exc.message, 'code': exc.code}, status_code=status_code)


def _awg_plugin_prune():
    now = datetime.now().timestamp()
    for op_id, op in list(_awg_plugin_ops.items()):
        if op.get('finished_ts') and now - op['finished_ts'] > AWG_PLUGIN_OP_TTL:
            _awg_plugin_ops.pop(op_id, None)
    if len(_awg_plugin_ops) > AWG_PLUGIN_OP_KEEP:
        finished = sorted(
            (op for op in _awg_plugin_ops.values() if op.get('finished_ts')),
            key=lambda op: op['finished_ts'],
        )
        for op in finished[:len(_awg_plugin_ops) - AWG_PLUGIN_OP_KEEP]:
            _awg_plugin_ops.pop(op['id'], None)


@router.get('/api/servers/{server_id}/awg-plugin/status')
async def api_awg_plugin_status(request: Request, server_id: int, db: AsyncSession = Depends(get_db)):
    """Installed and published versions of the AWG components of this server."""
    if not await app_module._check_admin(db, request):
        return JSONResponse({'error': 'Forbidden'}, status_code=403)
    _awg_plugin_load()
    try:
        updater = await _awg_plugin_updater(server_id, db)
        result = await asyncio.to_thread(updater.status)
        return result
    except PluginUpdateError as e:
        return _awg_plugin_error(e, 502 if e.code == 'server_not_found' else 400)
    except Exception as e:
        app_module.logger.exception("Error reading AWG plugin status")
        return JSONResponse({'error': str(e), 'code': 'internal_error'}, status_code=500)


@router.get('/api/servers/{server_id}/awg-plugin/changelog')
async def api_awg_plugin_changelog(request: Request, server_id: int, component: str, version: str,
                                   db: AsyncSession = Depends(get_db)):
    if not await app_module._check_admin(db, request):
        return JSONResponse({'error': 'Forbidden'}, status_code=403)
    if component not in AWG_PLUGIN_COMPONENTS:
        return JSONResponse({'error': 'Unknown component', 'code': 'unsupported'}, status_code=400)
    try:
        updater = await _awg_plugin_updater(server_id, db)
        entries = await asyncio.to_thread(updater.changelog, component, version)
        return {'component': component, 'version': version, 'entries': entries}
    except PluginUpdateError as e:
        return _awg_plugin_error(e, 502)


@router.post('/api/servers/{server_id}/awg-plugin/update')
async def api_awg_plugin_update(request: Request, server_id: int, req: AwgPluginUpdateRequest,
                                db: AsyncSession = Depends(get_db)):
    if not await app_module._check_admin(db, request):
        return JSONResponse({'error': 'Forbidden'}, status_code=403)
    if req.component not in AWG_PLUGIN_COMPONENTS:
        return JSONResponse({'error': 'Unknown component', 'code': 'unsupported'}, status_code=400)
    _awg_plugin_load()
    _awg_plugin_prune()
    key = (server_id, req.component)
    if key in _awg_plugin_active:
        return JSONResponse(
            {'error': 'An update of this component is already running',
             'code': 'update_in_progress'},
            status_code=409,
        )

    op_id = uuid.uuid4().hex
    op = {
        'id': op_id,
        'server_id': server_id,
        'component': req.component,
        'target': req.target,
        'state': 'running',
        'lines': [],
        'result': None,
        'error': None,
        'cancel_requested': False,
        'step': 0,
        'total': AWG_PLUGIN_TOTAL_STEPS,
        'percent': 0,
        'started_at': datetime.now().isoformat(timespec='seconds'),
    }
    _awg_plugin_ops[op_id] = op
    _awg_plugin_save()
    _awg_plugin_active[key] = op_id
    task = asyncio.create_task(_run_awg_plugin_update(
        op_id, server_id, req.component, req.target, bool(req.allow_interrupt),
    ))
    _awg_plugin_tasks.add(task)
    task.add_done_callback(_awg_plugin_tasks.discard)
    return {'operation_id': op_id}


async def _run_awg_plugin_update(op_id, server_id, component, target, allow_interrupt):
    op = _awg_plugin_ops[op_id]

    def log(stage, message):
        op['lines'].append({
            'ts': datetime.now().isoformat(timespec='seconds'),
            'stage': stage,
            'text': str(message)[-2000:],
        })
        mapped = awg_plugin_stage_step(op['component'], stage)
        if mapped:
            step, total = mapped
            if step > op.get('step', 0):
                op['step'] = step
                op['total'] = total
                op['percent'] = int(round(step * 100.0 / total))
        _awg_plugin_save()

    from database.session import async_session
    try:
        async with async_session() as db:
            updater = await _awg_plugin_updater(server_id, db)
        if not target:
            latest = await asyncio.to_thread(updater.latest_versions)
            target = latest.get(component)
        op['target'] = target
        if not target:
            raise PluginUpdateError('unsupported', 'no published version to update to')
        log('start', 'updating %s to %s' % (component, target))
        result = await asyncio.to_thread(
            updater.update, component, target,
            allow_interrupt=allow_interrupt, log=log,
            should_cancel=lambda: bool(op.get('cancel_requested')),
        )
        op['result'] = result
        op['state'] = 'manual_step_required' if result.get('state') == 'manual_step_required' else 'success'
        log('done', 'state: %s' % op['state'])
    except PluginUpdateError as e:
        op['state'] = 'cancelled' if e.code == 'cancelled' else 'failed'
        op['error'] = {'code': e.code, 'message': e.message}
        log('error', '%s: %s' % (e.code, e.message))
    except Exception as e:
        app_module.logger.exception("AWG plugin update failed")
        op['state'] = 'failed'
        op['error'] = {'code': 'internal_error', 'message': str(e)}
        log('error', str(e))
    finally:
        op['finished_at'] = datetime.now().isoformat(timespec='seconds')
        op['finished_ts'] = datetime.now().timestamp()
        _awg_plugin_active.pop((server_id, component), None)
        _awg_plugin_save()


@router.post('/api/servers/{server_id}/awg-plugin/update/{op_id}/cancel')
async def api_awg_plugin_update_cancel(request: Request, server_id: int, op_id: str,
                                       db: AsyncSession = Depends(get_db)):
    """Ask a running update to stop at the next safe boundary.

    The DKMS build itself cannot be interrupted; the operation aborts before
    the module switch or between containers, leaving the installed version in
    place.
    """
    if not await app_module._check_admin(db, request):
        return JSONResponse({'error': 'Forbidden'}, status_code=403)
    _awg_plugin_load()
    op = _awg_plugin_ops.get(op_id)
    if not op or op['server_id'] != server_id:
        return JSONResponse({'error': 'Operation not found', 'code': 'not_found'}, status_code=404)
    if op['state'] != 'running':
        return JSONResponse({'error': 'Operation is not running', 'code': 'not_running'}, status_code=409)
    op['cancel_requested'] = True
    op['lines'].append({
        'ts': datetime.now().isoformat(timespec='seconds'),
        'stage': 'cancel',
        'text': 'cancellation requested; stopping at the next safe boundary',
    })
    _awg_plugin_save()
    return {'status': 'cancelling'}


@router.get('/api/servers/{server_id}/awg-plugin/update/{op_id}')
async def api_awg_plugin_update_status(request: Request, server_id: int, op_id: str,
                                       db: AsyncSession = Depends(get_db)):
    if not await app_module._check_admin(db, request):
        return JSONResponse({'error': 'Forbidden'}, status_code=403)
    _awg_plugin_load()
    op = _awg_plugin_ops.get(op_id)
    if not op or op['server_id'] != server_id:
        return JSONResponse({'error': 'Operation not found', 'code': 'not_found'}, status_code=404)
    try:
        offset = max(0, int(request.query_params.get('offset', 0)))
    except (TypeError, ValueError):
        offset = 0
    lines = op['lines']
    return {
        'operation_id': op_id,
        'component': op['component'],
        'target': op['target'],
        'state': op['state'],
        'step': op.get('step', 0),
        'total': op.get('total', AWG_PLUGIN_TOTAL_STEPS),
        'percent': op.get('percent', 0),
        'result': op['result'],
        'error': op['error'],
        'started_at': op['started_at'],
        'finished_at': op.get('finished_at'),
        'lines': lines[offset:],
        'next_offset': len(lines),
    }
