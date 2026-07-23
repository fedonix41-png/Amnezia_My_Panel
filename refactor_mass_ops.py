import re

with open('app.py', 'r') as f:
    content = f.read()

# Replace perform_mass_operations
start_sig = "async def perform_mass_operations(delete_uids: List[str] = None, toggle_uids: List[tuple] = None, create_conns: List[dict] = None):"
start_idx = content.find(start_sig)
if start_idx != -1:
    end_idx = content.find("async def sync_users_with_remnawave(", start_idx)
    old_func = content[start_idx:end_idx]
    
    new_func = """async def perform_mass_operations(db, delete_uids: List[str] = None, toggle_uids: List[tuple] = None, create_conns: List[dict] = None):
    \"\"\"Executes multiple SSH operations efficiently.\"\"\"
    from database import models
    from sqlalchemy import select
    from sqlalchemy.orm import selectinload

    server_ops = {}
    def get_ops(sid):
        if sid not in server_ops:
            server_ops[sid] = {'delete': [], 'toggle': [], 'create': []}
        return server_ops[sid]

    # Collect operations
    if delete_uids:
        stmt = select(models.Connection).where(models.Connection.user_id.in_(delete_uids))
        res = await db.execute(stmt)
        for c in res.scalars().all():
            get_ops(c.server_id)['delete'].append(c)

    if toggle_uids:
        for uid, enabled in toggle_uids:
            stmt = select(models.Connection).where(models.Connection.user_id == uid)
            res = await db.execute(stmt)
            for c in res.scalars().all():
                get_ops(c.server_id)['toggle'].append((c, enabled))

    if create_conns:
        for req in create_conns:
            get_ops(req['server_id'])['create'].append(req)

    async def run_server_ops(srv_id, ops):
        srv = await db.get(models.Server, srv_id)
        if not srv: return
        
        try:
            ssh = get_ssh(srv)
            await asyncio.to_thread(ssh.connect)
            
            # 1. Deletes
            for c in ops['delete']:
                manager = get_protocol_manager(ssh, c.protocol)
                await asyncio.to_thread(_manager_call, manager, 'remove_client', c.protocol, c.client_id)
                await db.delete(c)
                await db.commit()
            
            # 2. Toggles
            for c, enabled in ops['toggle']:
                manager = get_protocol_manager(ssh, c.protocol)
                await asyncio.to_thread(_manager_call, manager, 'toggle_client', c.protocol, c.client_id, enabled)
            
            # 3. Creates
            for c_req in ops['create']:
                stmt = select(models.ServerProtocol).where(models.ServerProtocol.server_id == srv_id, models.ServerProtocol.protocol_type == c_req['protocol'])
                res = await db.execute(stmt)
                proto_record = res.scalars().first()
                port = proto_record.port if proto_record and proto_record.port else 55424
                
                manager = get_protocol_manager(ssh, c_req['protocol'])
                if c_req['protocol'] == 'wireguard':
                    res_add = await asyncio.to_thread(manager.add_client, c_req['name'], srv.host)
                else:
                    res_add = await asyncio.to_thread(_manager_call, manager, 'add_client', c_req['protocol'], c_req['name'], srv.host, port)
                
                if res_add.get('client_id'):
                    new_conn = models.Connection(
                        id=str(uuid.uuid4()),
                        user_id=c_req['user_id'],
                        server_id=srv_id,
                        protocol=c_req['protocol'],
                        client_id=res_add['client_id'],
                        name=c_req['name'],
                        created_at=datetime.now(),
                    )
                    db.add(new_conn)
                    await db.commit()
            
            await asyncio.to_thread(ssh.disconnect)
        except Exception as e:
            logger.error(f"Mass ops failed for server {srv_id}: {e}")

    tasks = [run_server_ops(sid, ops) for sid, ops in server_ops.items()]
    if tasks:
        await asyncio.gather(*tasks)

    # Final user-level cleanup
    if delete_uids:
        stmt = select(models.User).where(models.User.id.in_(delete_uids))
        res = await db.execute(stmt)
        for u in res.scalars().all():
            await db.delete(u)
        await db.commit()

    if toggle_uids:
        for uid, enabled in toggle_uids:
            u = await db.get(models.User, uid)
            if u:
                u.enabled = enabled
        await db.commit()

    return True

"""
    content = content.replace(old_func, new_func)
    
    with open('app.py', 'w') as f:
        f.write(content)
    print("Done")
else:
    print("Not found")
