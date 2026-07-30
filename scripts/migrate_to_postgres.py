import os
import sys
import json
import asyncio
from datetime import datetime


# Add the project root to the python path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from database.session import engine, async_session
from database import models
from database.models import Base

DATA_FILE = os.path.join(os.path.dirname(__file__), '..', 'data.json')

def parse_dt(dt_str):
    if not dt_str:
        return None
    try:
        from datetime import datetime, timezone
        # Handle simple parsing for standard ISO string with or without Z
        dt = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None

async def migrate():
    print("Initializing database schema...")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    if not os.path.exists(DATA_FILE):
        print(f"File {DATA_FILE} does not exist. Nothing to migrate.")
        return

    print(f"Reading data from {DATA_FILE}...")
    with open(DATA_FILE, 'r') as f:
        data = json.load(f)

    async with async_session() as db:
        print("Migrating Settings...")
        for key in ["settings", "amnezia_settings"]:
            if key in data:
                setting = await db.get(models.Setting, key)
                if not setting:
                    setting = models.Setting(key=key, value=data[key])
                    db.add(setting)
                else:
                    setting.value = data[key]
        
        print("Migrating Users...")
        user_ids = set()
        for u in data.get("users", []):
            user_id = u.get("id")
            if not user_id: continue
            
            user = await db.get(models.User, user_id)
            if not user:
                user = models.User(id=user_id)
                db.add(user)
            
            user.username = u.get("username", "Unknown")
            user.password_hash = u.get("password_hash", "")
            user.role = u.get("role", "user")
            user.enabled = u.get("enabled", True)
            user.created_at = parse_dt(u.get("createdAt")) or parse_dt(u.get("created_at")) or models.utcnow()
            
            user.share_enabled = u.get("share_enabled", False)
            user.share_token = u.get("share_token")
            user.share_password_hash = u.get("share_password_hash")
            
            user.traffic_reset_strategy = u.get("traffic_reset_strategy", "never")
            user.traffic_used = u.get("traffic_used", 0)
            user.traffic_total = u.get("traffic_total", 0)
            user.last_reset_at = parse_dt(u.get("last_reset_at"))
            user.expiration_date = parse_dt(u.get("expiration_date"))
            user.traffic_limit = u.get("traffic_limit")
            
            user.telegramId = int(str(u.get("telegramId")).lstrip("@") or 0) if str(u.get("telegramId")).lstrip("@").isdigit() else None
            user.email = u.get("email")
            user.description = u.get("description")
            user.remnawave_uuid = u.get("remnawave_uuid")
            user_ids.add(user_id)

        print("Migrating API Tokens...")
        for t in data.get("api_tokens", []):
            token_id = t.get("id")
            if not token_id: continue
            if t.get("user_id") not in user_ids: continue
            
            token = await db.get(models.ApiToken, token_id)
            if not token:
                token = models.ApiToken(id=token_id)
                db.add(token)
            
            token.name = t.get("name", "Unknown")
            token.token_hash = t.get("token_hash", "")
            token.token_prefix = t.get("token_prefix", "")
            token.user_id = t.get("user_id")
            token.created_at = parse_dt(t.get("created_at")) or models.utcnow()
            token.last_used_at = parse_dt(t.get("last_used_at"))

        print("Migrating Servers and Protocols...")
        for idx, s in enumerate(data.get("servers", [])):
            server_id = idx
            server = await db.get(models.Server, server_id)
            if not server:
                server = models.Server(id=server_id)
                db.add(server)
            
            server.display_order = server_id
            server.name = s.get("name", s.get("host", f"Server {server_id}"))
            server.host = s.get("host", "")
            server.ssh_port = s.get("ssh_port", 22)
            server.username = s.get("username", "root")
            server.password = s.get("password")
            server.private_key = s.get("private_key")
            server.server_info = s.get("server_info")
            server.host_fingerprint = s.get("host_fingerprint")
            
            # Protocols
            protocols_dict = s.get("protocols", {})
            for proto_type, p_info in protocols_dict.items():
                from sqlalchemy import select
                stmt = select(models.ServerProtocol).where(
                    models.ServerProtocol.server_id == server_id,
                    models.ServerProtocol.protocol_type == proto_type
                )
                res = await db.execute(stmt)
                proto_record = res.scalars().first()
                if not proto_record:
                    proto_record = models.ServerProtocol(server_id=server_id, protocol_type=proto_type)
                    db.add(proto_record)
                
                def safe_int(v):
                    try:
                        return int(v) if v is not None and str(v).strip() else None
                    except ValueError:
                        return None
                
                proto_record.installed = p_info.get("installed", True)
                proto_record.port = safe_int(p_info.get("port"))
                proto_record.base_protocol = p_info.get("base_protocol")
                proto_record.instance = safe_int(p_info.get("instance"))
                proto_record.display_name = p_info.get("display_name")
                proto_record.container_name = p_info.get("container_name")
                
                # Copy extra params
                extra = {}
                for k, v in p_info.items():
                    if k not in ("installed", "port", "base_protocol", "instance", "display_name", "container_name"):
                        extra[k] = v
                proto_record.extra_params = extra

        print("Migrating User Connections...")
        for c in data.get("user_connections", []):
            conn_id = c.get("id")
            if not conn_id: continue
            if c.get("user_id") not in user_ids: continue
            
            conn = await db.get(models.UserConnection, conn_id)
            if not conn:
                conn = models.UserConnection(id=conn_id)
                db.add(conn)
            
            conn.user_id = c.get("user_id")
            conn.server_id = c.get("server_id", 0)
            conn.protocol = c.get("protocol", "awg")
            conn.client_id = c.get("client_id", "")
            conn.name = c.get("name", "Unknown")
            conn.created_at = parse_dt(c.get("created_at")) or models.utcnow()
            conn.last_bytes = c.get("last_bytes")
            
        await db.commit()
        print("Migration complete!")

if __name__ == "__main__":
    asyncio.run(migrate())
