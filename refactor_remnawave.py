import re

with open('app.py', 'r') as f:
    content = f.read()

start_sig = "async def sync_users_with_remnawave(data: dict):"
start_idx = content.find(start_sig)
if start_idx != -1:
    end_idx = content.find("def get_current_user(", start_idx)
    old_func = content[start_idx:end_idx]
    
    new_func = """async def sync_users_with_remnawave(db):
    from database import models
    from sqlalchemy import select
    
    settings = {}
    stmt = select(models.Setting).where(models.Setting.key == 'settings')
    res = await db.execute(stmt)
    setting_obj = res.scalars().first()
    if setting_obj and setting_obj.value:
        settings = setting_obj.value.get('sync', {})
        
    if not settings.get('remnawave_sync_users'):
        return 0, "Synchronization is disabled in settings"
    
    url = settings.get('remnawave_url')
    api_key = settings.get('remnawave_api_key')
    if not url or not api_key:
        return 0, "Remnawave URL or API Key not configured"
    
    api_url = url.rstrip('/') + '/api/users'
    headers = {"Authorization": f"Bearer {api_key}"}
    
    try:
        rw_users = []
        async with httpx.AsyncClient(timeout=30.0) as client:
            page_size = 50
            current_start = 0
            while True:
                resp = await client.get(f"{api_url}?size={page_size}&start={current_start}", headers=headers)
                if resp.status_code != 200:
                    return 0, f"Remnawave API error: {resp.status_code} {resp.text}"
                
                page_data = resp.json()
                response_obj = page_data.get('response', {})
                page_users = response_obj.get('users', [])
                total_count = response_obj.get('total', 0)
                
                if not page_users: break
                
                rw_users.extend(page_users)
                if len(rw_users) >= total_count or len(page_users) == 0: break
                current_start += len(page_users)

            rw_uuids = {u['uuid'] for u in rw_users}
            
            stmt = select(models.User)
            res = await db.execute(stmt)
            all_users = res.scalars().all()
            
            # 1. Handle deletion
            to_delete_ids = []
            for u in all_users:
                if u.remnawave_uuid and u.remnawave_uuid not in rw_uuids:
                    to_delete_ids.append(u.id)
            
            if to_delete_ids:
                logger.info(f"Removing {len(to_delete_ids)} users deleted in Remnawave")
                await perform_mass_operations(db, delete_uids=to_delete_ids)

            # 2. Sync / Create users
            synced_count = 0
            to_toggle = []
            to_create_conns = []
            
            for rw_u in rw_users:
                stmt = select(models.User).where(models.User.remnawave_uuid == rw_u['uuid'])
                res = await db.execute(stmt)
                local_u = res.scalars().first()
                if not local_u:
                    stmt = select(models.User).where(models.User.username == rw_u['username'])
                    res = await db.execute(stmt)
                    local_u = res.scalars().first()

                is_active = (rw_u.get('status') == 'ACTIVE')
                
                if local_u:
                    local_u.username = rw_u['username']
                    local_u.telegramId = rw_u.get('telegramId')
                    local_u.email = rw_u.get('email')
                    local_u.description = rw_u.get('description')
                    local_u.remnawave_uuid = rw_u['uuid']
                    
                    if getattr(local_u, 'enabled', True) != is_active:
                        to_toggle.append((local_u.id, is_active))
                        local_u.enabled = is_active
                        
                    await db.commit()
                    synced_count += 1
                else:
                    new_id = str(uuid.uuid4())
                    new_user = models.User(
                        id=new_id,
                        username=rw_u['username'],
                        password_hash='',
                        role='user',
                        telegramId=rw_u.get('telegramId'),
                        email=rw_u.get('email'),
                        description=rw_u.get('description'),
                        enabled=is_active,
                        remnawave_uuid=rw_u['uuid'],
                        share_enabled=False,
                        share_token=secrets.token_urlsafe(16),
                    )
                    db.add(new_user)
                    await db.commit()
                    
                    if settings.get('remnawave_create_conns'):
                        sid = settings.get('remnawave_server_id')
                        if sid is not None:
                            to_create_conns.append({
                                'user_id': new_id,
                                'server_id': sid,
                                'protocol': settings.get('remnawave_protocol', 'awg'),
                                'name': f"{rw_u['username']}_vpn"
                            })
                    synced_count += 1
            
            if to_toggle or to_create_conns:
                logger.info(f"Executing mass ops for Remnawave sync: toggle={len(to_toggle)}, create={len(to_create_conns)}")
                await perform_mass_operations(db, toggle_uids=to_toggle, create_conns=to_create_conns)
            
            return synced_count, "Successfully synchronized with Remnawave"
            
    except Exception as e:
        logger.exception("Synchronization error")
        return 0, f"Error: {str(e)}"

"""
    content = content.replace(old_func, new_func)
    with open('app.py', 'w') as f:
        f.write(content)
    print("Done")
else:
    print("Not found")
