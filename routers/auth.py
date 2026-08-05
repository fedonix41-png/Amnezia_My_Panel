import io
from typing import Optional
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel

import config
import app as app_module
from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from database.session import get_db
from database import models
from database.crud import get_setting

try:
    from multicolorcaptcha import CaptchaGenerator
except ImportError:
    CaptchaGenerator = None

router = APIRouter(tags=["Authentication"])


class LoginRequest(BaseModel):
    username: str
    password: str
    captcha: Optional[str] = None


@router.get('/api/auth/captcha')
@app_module.limiter.limit(config.CAPTCHA_RATE_LIMIT)
async def api_captcha(request: Request):
    if not CaptchaGenerator:
        return JSONResponse({"error": "multicolorcaptcha is not installed"}, status_code=500)
    
    # 2 is a multiplier for the image resolution size
    generator = CaptchaGenerator(2)
    captcha = generator.gen_captcha_image(difficult_level=2)
    request.session['captcha_answer'] = captcha.characters
    
    img_bytes = io.BytesIO()
    captcha.image.save(img_bytes, format='PNG')
    img_bytes.seek(0)
    
    return StreamingResponse(img_bytes, media_type="image/png")


@router.post('/api/auth/login')
@app_module.limiter.limit(config.LOGIN_RATE_LIMIT)
async def api_login(request: Request, req: LoginRequest, db: AsyncSession = Depends(get_db)):
    main_settings = await get_setting(db, 'settings', {})
    captcha_settings = main_settings.get('captcha', {})
    if captcha_settings.get('enabled') is True:
        answer = request.session.get('captcha_answer')
        lang = request.cookies.get('lang', 'ru')
        if not answer or not req.captcha or answer.lower() != req.captcha.lower():
            request.session.pop('captcha_answer', None)
            return JSONResponse({'error': app_module._t('invalid_captcha', lang)}, status_code=400)
        request.session.pop('captcha_answer', None)

    stmt = select(models.User).where(models.User.username == req.username)
    result = await db.execute(stmt)
    u = result.scalar_one_or_none()
    
    if u and app_module.verify_password(req.password, u.password_hash):
        lang = request.cookies.get('lang', 'ru')
        if not u.enabled:
            return JSONResponse({'error': app_module._t('account_disabled', lang)}, status_code=403)
        request.session['user_id'] = u.id
        if app_module.posthog_client:
            with app_module.posthog_client.new_context(fresh=True):
                app_module.posthog_client.identify_context(str(u.id))
                app_module.posthog_client.set(
                    distinct_id=str(u.id),
                    properties={
                        "username": u.username,
                        "role": u.role,
                        "email": u.email,
                    },
                )
                app_module.posthog_client.capture(
                    "user_logged_in", properties={"role": u.role}
                )
        return {'status': 'success', 'role': u.role}
    
    lang = request.cookies.get('lang', 'ru')
    return JSONResponse({'error': app_module._t('invalid_login', lang)}, status_code=401)
