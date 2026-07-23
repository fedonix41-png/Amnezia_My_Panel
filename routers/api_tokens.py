import uuid
from datetime import datetime
from typing import Optional
from fastapi import APIRouter, Request, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from sqlalchemy.orm import selectinload

import app as app_module
from database.session import get_db
from database import models

router = APIRouter(tags=["API Tokens"])


class CreateApiTokenRequest(BaseModel):
    name: str


@router.get('/api/settings/tokens', tags=["API Tokens"])
async def api_list_tokens(request: Request, db: AsyncSession = Depends(get_db)):
    """List metadata for every API token. The raw token value is never
    returned by this endpoint — only its prefix and timestamps are visible
    after creation, by design."""
    if not await app_module._check_admin(db, request):
        return JSONResponse({'error': 'Forbidden'}, status_code=403)
        
    stmt = select(models.ApiToken).options(selectinload(models.ApiToken.user))
    res = await db.execute(stmt)
    tokens_models = res.scalars().all()
    
    tokens = []
    for t in tokens_models:
        tokens.append({
            'id': t.id,
            'name': t.name,
            'token_prefix': t.token_prefix,
            'created_at': t.created_at,
            'last_used_at': t.last_used_at,
            'owner': t.user.username if t.user else None,
            'owner_id': t.user_id,
        })
    return {'tokens': tokens}


@router.post('/api/settings/tokens', tags=["API Tokens"])
async def api_create_token(request: Request, req: CreateApiTokenRequest, db: AsyncSession = Depends(get_db)):
    """Issue a new bearer token. The full token value is returned **once** in
    the response and never persisted in plaintext — only its SHA-256 hash is
    stored, so a leaked data.json file alone cannot be used to authenticate.
    Save the value at creation time; if it's lost the token must be recreated.
    """
    cur = await app_module._check_admin(db, request)
    if not cur:
        return JSONResponse({'error': 'Forbidden'}, status_code=403)
    name = (req.name or '').strip()
    if not name:
        return JSONResponse({'error': 'Token name is required'}, status_code=400)

    raw = app_module._generate_api_token()
    token_id = str(uuid.uuid4())
    token_prefix = raw[:len(app_module.API_TOKEN_PREFIX) + 4]

    token_obj = models.ApiToken(
        id=token_id,
        name=name,
        token_hash=app_module._hash_api_token(raw),
        token_prefix=token_prefix,
        user_id=cur.id
    )
    db.add(token_obj)
    await db.commit()

    return {
        'status': 'success',
        'id': token_id,
        'name': name,
        'token': raw,
        'token_prefix': token_prefix,
        'created_at': token_obj.created_at,
    }


@router.delete('/api/settings/tokens/{token_id}', tags=["API Tokens"])
async def api_revoke_token(request: Request, token_id: str, db: AsyncSession = Depends(get_db)):
    """Permanently revoke a token. The associated bearer value can never be
    used again, even if the same name is reissued — every token has its own hash."""
    if not await app_module._check_admin(db, request):
        return JSONResponse({'error': 'Forbidden'}, status_code=403)
        
    token_obj = await db.get(models.ApiToken, token_id)
    if not token_obj:
        return JSONResponse({'error': 'Token not found'}, status_code=404)
        
    await db.delete(token_obj)
    await db.commit()
    
    return {'status': 'success'}
