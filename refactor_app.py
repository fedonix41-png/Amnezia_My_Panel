import re

with open('app.py', 'r') as f:
    content = f.read()

new_get_current_user = """async def get_current_user(db, request: Request):
    user_id = request.session.get('user_id')
    if not user_id:
        return None
    from database import models
    return await db.get(models.User, user_id)"""

old_get_current_user = """def get_current_user(request: Request):
    user_id = request.session.get('user_id')
    if not user_id:
        return None
    data = load_data()
    for u in data.get('users', []):
        if u['id'] == user_id:
            return u
    return None"""

content = content.replace(old_get_current_user, new_get_current_user)

old_check_admin = """def _check_admin(request):
    \"\"\"Authorize an admin/support action via session cookie OR Bearer token.

    Tokens are admin-equivalent and inherit the role of the user who created
    them — if that user is later disabled or demoted, the token stops working.
    \"\"\"
    user = get_current_user(request)
    if user and user['role'] in ('admin', 'support'):
        return user

    auth_header = request.headers.get('Authorization', '')
    if auth_header.lower().startswith('bearer '):
        raw_token = auth_header[7:].strip()
        data = load_data()
        resolved = _resolve_api_token(data, raw_token)
        if resolved:
            entry, token_user = resolved
            try:
                if _touch_api_token(entry):
                    save_data(data)
            except Exception as e:
                logger.warning(f"Failed to touch API token last_used_at: {e}")
            return token_user

    return None"""

new_check_admin = """async def _check_admin(db, request: Request):
    user = await get_current_user(db, request)
    if user and user.role in ('admin', 'support'):
        return user
    auth_header = request.headers.get('Authorization', '')
    if auth_header.lower().startswith('bearer '):
        raw_token = auth_header[7:].strip()
        from sqlalchemy import select
        from sqlalchemy.orm import selectinload
        import hashlib
        from database import models
        token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
        stmt = select(models.ApiToken).where(models.ApiToken.token_hash == token_hash).options(selectinload(models.ApiToken.user))
        res = await db.execute(stmt)
        token = res.scalars().first()
        if token and token.user and token.user.role in ('admin', 'support') and token.user.enabled:
            token.last_used_at = models.utcnow()
            await db.commit()
            return token.user
    return None"""

content = content.replace(old_check_admin, new_check_admin)

old_tpl = """def tpl(request, template, **kwargs):
    data = load_data()
    lang = request.cookies.get('lang', 'en')
    ctx = {
        'request': request,
        'current_user': get_current_user(request),
        'site_settings': data.get('settings', {}).get('appearance', {}),
        'captcha_settings': data.get('settings', {}).get('captcha', {}),
        'telegram_settings': data.get('settings', {}).get('telegram', {}),
        'bot_running': tg_bot.is_running(),
        'lang': lang,
        '_': lambda text_id: _t(text_id, lang),
    }
    ctx.update(kwargs)
    return templates.TemplateResponse(template, ctx)"""

new_tpl = """def tpl(request, template, **kwargs):
    # Actually, we need to pass settings as kwargs to tpl from the caller.
    # Since callers are async, let's just use defaults for now, or fetch dynamically if needed.
    # We will let the callers inject what they need.
    lang = request.cookies.get('lang', 'en')
    ctx = {
        'request': request,
        'bot_running': tg_bot.is_running(),
        'lang': lang,
        '_': lambda text_id: _t(text_id, lang),
    }
    ctx.update(kwargs)
    return templates.TemplateResponse(template, ctx)"""

content = content.replace(old_tpl, new_tpl)

with open('app.py', 'w') as f:
    f.write(content)

