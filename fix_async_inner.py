import re

with open('telegram_bot.py', 'r') as f:
    content = f.read()

# Fix _admin_toggle_protocol
old_toggle = """    def _toggle():
        data = await _build_data_dict(db)"""
new_toggle = """    data = await _build_data_dict(db)
    def _toggle(data=data):"""
content = content.replace(old_toggle, new_toggle)

# Fix _admin_do_install_protocol
old_install = """    def _install():
        data = await _build_data_dict(db)"""
new_install = """    data = await _build_data_dict(db)
    def _install(data=data):"""
content = content.replace(old_install, new_install)

# Fix _admin_do_uninstall_protocol
old_uninstall = """    def _uninstall():
        data = await _build_data_dict(db)"""
new_uninstall = """    data = await _build_data_dict(db)
    def _uninstall(data=data):"""
content = content.replace(old_uninstall, new_uninstall)

# Fix _admin_create_client
old_create = """    def _create():
        data = await _build_data_dict(db)"""
new_create = """    data = await _build_data_dict(db)
    def _create(data=data):"""
content = content.replace(old_create, new_create)

# Fix _admin_remove_client
old_remove = """    def _remove():
        data = await _build_data_dict(db)"""
new_remove = """    data = await _build_data_dict(db)
    def _remove(data=data):"""
content = content.replace(old_remove, new_remove)


with open('telegram_bot.py', 'w') as f:
    f.write(content)

print("Done fixing")
