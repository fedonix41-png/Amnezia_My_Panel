import os

routers = ['routers/connections.py', 'routers/sharing.py', 'routers/users.py']
for path in routers:
    with open(path, 'rb') as f:
        content = f.read()
    if b"B8AC" in content:
        print(f"FOUND HARDCODED IN {path}")
    else:
        print(f"NO HARDCODED IN {path}")
