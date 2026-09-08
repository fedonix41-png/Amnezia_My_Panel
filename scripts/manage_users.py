"""Utility script to list users and reset passwords in the database.

Usage:
    # List all users
    venv/bin/python scripts/manage_users.py list

    # Reset admin password to a new value
    venv/bin/python scripts/manage_users.py reset-password <username> <new_password>

    # Reset admin password (interactive prompt for new password)
    venv/bin/python scripts/manage_users.py reset-password <username>
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import config
from database.session import engine, async_session
from database.models import Base, User
from database.repositories import UserRepository, hash_password
from sqlalchemy import select


async def list_users():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with async_session() as session:
        stmt = select(User).order_by(User.created_at.desc())
        res = await session.execute(stmt)
        users = list(res.scalars().all())

        if not users:
            print("No users found in the database.")
            return

        print(f"\n{'ID':<36} {'Username':<20} {'Role':<10} {'Active':<6} {'Created At':<25} Password Hash")
        print("-" * 120)
        for u in users:
            password_hash_display = u.password_hash[:60] + "..." if len(u.password_hash) > 60 else u.password_hash
            print(
                f"{str(u.id):<36} {u.username:<20} {u.role:<10} "
                f"{str(u.enabled):<6} {u.created_at.strftime('%Y-%m-%d %H:%M:%S'):<25} {password_hash_display}"
            )
        print(f"\nTotal: {len(users)} user(s)")


async def reset_password(username: str, new_password: str):
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with async_session() as session:
        user = await UserRepository.get_by_username(session, username)
        if not user:
            print(f"User '{username}' not found in the database.")
            return False

        new_hash = hash_password(new_password)
        user.password_hash = new_hash
        await session.commit()

        print(f"Password for user '{username}' (id={user.id}, role={user.role}) has been reset successfully.")
        return True


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    command = sys.argv[1]

    if command == "list":
        asyncio.run(list_users())

    elif command == "reset-password":
        if len(sys.argv) < 3:
            print("Usage: venv/bin/python scripts/manage_users.py reset-password <username> [new_password]")
            sys.exit(1)

        username = sys.argv[2]
        if len(sys.argv) >= 4:
            new_password = sys.argv[3]
        else:
            import getpass

            new_password = getpass.getpass(f"Enter new password for '{username}': ")
            if not new_password:
                print("Password cannot be empty.")
                sys.exit(1)

        success = asyncio.run(reset_password(username, new_password))
        if not success:
            sys.exit(1)

    else:
        print(f"Unknown command: {command}")
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    main()
