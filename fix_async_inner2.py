import re

with open('telegram_bot.py', 'r') as f:
    content = f.read()

# Fix _load_clients in _admin_clients
old_load_clients = """    def _load_clients():
        data = await _build_data_dict(db)"""
new_load_clients = """    data = await _build_data_dict(db)
    def _load_clients(data=data):"""
content = content.replace(old_load_clients, new_load_clients)

# Fix _load_client in _admin_client_detail
old_load_client = """    def _load_client():
        data = await _build_data_dict(db)"""
new_load_client = """    data = await _build_data_dict(db)
    def _load_client(data=data):"""
content = content.replace(old_load_client, new_load_client)

with open('telegram_bot.py', 'w') as f:
    f.write(content)

print("Done fixing 2")
