import uuid
from datetime import datetime
from typing import Optional
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

import app as app_module

router = APIRouter(tags=["API Tokens"])


class CreateApiTokenRequest(BaseModel):
    name: str


@router.get('/api/settings/tokens', tags=["API Tokens"])
async def api_list_tokens(request: Request):
    """List metadata for every API token. The raw token value is never
    returned by this endpoint — only its prefix and timestamps are visible
    after creation, by design."""
    if not app_module._check_admin(request):
        return JSONResponse({'error': 'Forbidden'}, status_code=403)
    data = app_module.load_data()
    users_by_id = {u['id']: u for u in data.get('users', [])}
    tokens = []
    for t in data.get('api_tokens', []):
        owner = users_by_id.get(t.get('user_id'))
        tokens.append({
            'id': t.get('id'),
            'name': t.get('name', ''),
            'token_prefix': t.get('token_prefix', ''),
            'created_at': t.get('created_at'),
            'last_used_at': t.get('last_used_at'),
            'owner': owner['username'] if owner else None,
            'owner_id': t.get('user_id'),
        })
    return {'tokens': tokens}


@router.post('/api/settings/tokens', tags=["API Tokens"])
async def api_create_token(request: Request, req: CreateApiTokenRequest):
    """Issue a new bearer token. The full token value is returned **once** in
    the response and never persisted in plaintext — only its SHA-256 hash is
    stored, so a leaked data.json file alone cannot be used to authenticate.
    Save the value at creation time; if it's lost the token must be recreated.
    """
    cur = app_module._check_admin(request)
    if not cur:
        return JSONResponse({'error': 'Forbidden'}, status_code=403)
    name = (req.name or '').strip()
    if not name:
        return JSONResponse({'error': 'Token name is required'}, status_code=400)

    raw = app_module._generate_api_token()
    token_id = str(uuid.uuid4())
    token_prefix = raw[:len(app_module.API_TOKEN_PREFIX) + 4]

    entry = {
        'id': token_id,
        'name': name,
        'token_hash': app_module._hash_api_token(raw),
        'token_prefix': token_prefix,
        'user_id': cur['id'],
        'created_at': datetime.now().isoformat(),
        'last_used_at': None,
    }
    async with app_module.DATA_LOCK:
        data = app_module.load_data()
        data.setdefault('api_tokens', []).append(entry)
        app_module.save_data(data)

    return {
        'status': 'success',
        'id': token_id,
        'name': name,
        'token': raw,
        'token_prefix': token_prefix,
        'created_at': entry['created_at'],
    }


@router.delete('/api/settings/tokens/{token_id}', tags=["API Tokens"])
async def api_revoke_token(request: Request, token_id: str):
    """Permanently revoke a token. The associated bearer value can never be
    used again, even if the same name is reissued — every token has its own hash."""
    if not app_module._check_admin(request):
        return JSONResponse({'error': 'Forbidden'}, status_code=403)
    async with app_module.DATA_LOCK:
        data = app_module.load_data()
        before = len(data.get('api_tokens', []))
        data['api_tokens'] = [t for t in data.get('api_tokens', []) if t.get('id') != token_id]
        if len(data['api_tokens']) == before:
            return JSONResponse({'error': 'Token not found'}, status_code=404)
        app_module.save_data(data)
    return {'status': 'success'}
