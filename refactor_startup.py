import re

with open('app.py', 'r') as f:
    content = f.read()

new_startup = """async def _startup():
    from database.session import async_session
    from database import models
    from sqlalchemy import select
    import uuid
    from datetime import datetime
    import secrets

    async with async_session() as db:
        # Default admin
        stmt = select(models.User).where(models.User.username == 'admin')
        res = await db.execute(stmt)
        if not res.scalars().first():
            db.add(models.User(
                id=str(uuid.uuid4()),
                username='admin',
                password_hash=hash_password('admin'),
                role='admin',
                enabled=True,
                created_at=datetime.now()
            ))
            logger.info("Default admin created (admin / admin)")
        
        # SSL settings
        stmt = select(models.Setting).where(models.Setting.key == 'settings')
        res = await db.execute(stmt)
        setting = res.scalars().first()
        if not setting:
            setting = models.Setting(key='settings', value={})
            db.add(setting)
        
        settings_dict = setting.value or {}
        ssl_conf = settings_dict.get('ssl')
        if not ssl_conf:
            settings_dict['ssl'] = {
                'enabled': False,
                'domain': '',
                'cert_path': '',
                'key_path': '',
                'cert_text': '',
                'key_text': '',
                'panel_port': 5000
            }
            setting.value = settings_dict
            from sqlalchemy.orm.attributes import flag_modified
            flag_modified(setting, "value")
            logger.info("Migrated SSL settings")

        await db.commit()

        # Telegram
        tg_cfg = settings_dict.get('telegram', {})
        if tg_cfg.get('enabled') and tg_cfg.get('token'):
            logger.info("Starting Telegram bot from saved settings...")
            tg_bot.launch_bot(tg_cfg['token'], _amnezia_generate_vpn_link)

    global _bg_traffic, _bg_backup
    _bg_traffic = asyncio.create_task(periodic_background_tasks())
    _bg_backup = asyncio.create_task(_background_backup())"""

# Replace _startup
start_idx = content.find("async def _startup():")
end_idx = content.find("def _scrape_server_traffic(", start_idx)
if start_idx != -1 and end_idx != -1:
    content = content[:start_idx] + new_startup + "\n\n\n" + content[end_idx:]


new_bg = """async def periodic_background_tasks():
    from database.session import async_session
    from database import models
    from sqlalchemy import select
    from sqlalchemy.orm import selectinload
    while True:
        try:
            await asyncio.sleep(60)
            logger.info("Starting background traffic sync...")
            
            async with async_session() as db:
                stmt = select(models.Server).options(selectinload(models.Server.protocols))
                res = await db.execute(stmt)
                servers = res.scalars().all()
                
                stmt = select(models.UserConnection)
                res = await db.execute(stmt)
                all_conns = res.scalars().all()
                conns_by_server = {}
                for uc in all_conns:
                    conns_by_server.setdefault(uc.server_id, []).append(uc)
                    
                updates = []
                for srv in servers:
                    if srv.id not in conns_by_server: continue
                    # Transform server and conns to dicts for _scrape_server_traffic
                    srv_dict = {
                        "host": srv.host, "ssh_port": srv.ssh_port, "username": srv.username,
                        "password": srv.password, "private_key": srv.private_key,
                        "protocols": {p.protocol_type: {} for p in srv.protocols}
                    }
                    conns_dict = [{"id": uc.id, "protocol": uc.protocol, "client_id": uc.client_id, "last_bytes": uc.last_bytes} for uc in conns_by_server[srv.id]]
                    server_updates = await asyncio.to_thread(_scrape_server_traffic, srv_dict, srv.id, conns_dict)
                    if server_updates:
                        updates.extend(server_updates)
                        
                to_disable_uids = []
                if updates:
                    now = datetime.now()
                    for uc_id, delta, curr_bytes in updates:
                        uc = await db.get(models.UserConnection, uc_id)
                        if uc:
                            uc.last_bytes = curr_bytes
                            uid = uc.user_id
                            u = await db.get(models.User, uid)
                            if u:
                                strategy = u.traffic_reset_strategy or 'never'
                                last_reset_iso = u.last_reset_at
                                reset_needed = False
                                if strategy != 'never' and last_reset_iso:
                                    try:
                                        if strategy == 'daily':
                                            reset_needed = now.date() > last_reset_iso.date()
                                        elif strategy == 'weekly':
                                            reset_needed = now.isocalendar()[1] != last_reset_iso.isocalendar()[1] or now.year != last_reset_iso.year
                                        elif strategy == 'monthly':
                                            reset_needed = now.month != last_reset_iso.month or now.year != last_reset_iso.year
                                    except: pass
                                    
                                if reset_needed:
                                    u.traffic_used = 0
                                    u.last_reset_at = now
                                    
                                u.traffic_used = (u.traffic_used or 0) + delta
                                u.traffic_total = (u.traffic_total or 0) + delta
                                
                                limit = u.traffic_limit or 0
                                if limit > 0 and u.traffic_used >= limit and u.enabled:
                                    if uid not in to_disable_uids:
                                        to_disable_uids.append(uid)
                                        
                                if u.expiration_date and u.enabled:
                                    try:
                                        if now > u.expiration_date:
                                            if uid not in to_disable_uids:
                                                to_disable_uids.append(uid)
                                    except: pass
                    await db.commit()
                    
                if to_disable_uids:
                    await perform_mass_operations(db, toggle_uids=[(uid, False) for uid in to_disable_uids])
                    
                logger.info("Starting background Remnawave sync...")
                count, msg = await sync_users_with_remnawave(db)
                logger.info(f"Background Remnawave sync finished: {count} users updated. {msg}")

        except Exception as e:
            logger.error(f"Error in periodic_background_tasks: {e}")
            
        await asyncio.sleep(600)"""

start_idx = content.find("async def periodic_background_tasks():")
end_idx = content.find("# ======================== ROUTERS REGISTRATION ========================", start_idx)
if start_idx != -1 and end_idx != -1:
    content = content[:start_idx] + new_bg + "\n\n\n" + content[end_idx:]


# Also remove _maybe_migrate_legacy_data_file since we don't use it anymore
content = re.sub(r'def _maybe_migrate_legacy_data_file\(\):.*?async def _startup', 'async def _startup', content, flags=re.DOTALL)

with open('app.py', 'w') as f:
    f.write(content)
print("Done")
