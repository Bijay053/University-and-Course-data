import asyncio
import os
import sys

import bcrypt
import click

# Ensure Python can see the 'app' directory when running this script directly
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from app.database import AsyncSessionLocal
from app.models import User


# Helper to hash passwords using bcrypt
def hash_password(password: str) -> str:
    salt = bcrypt.gensalt()
    return bcrypt.hashpw(password.encode("utf-8"), salt).decode("utf-8")


@click.command()
@click.option("--email", prompt="Enter email address", help="The email for the new user.")
@click.option("--full-name", prompt="Enter full name", help="The full name of the user.")
@click.option(
    "--password",
    prompt="Enter password",
    hide_input=True,
    confirmation_prompt=True,
    help="The password for the new user.",
)
@click.option(
    "--super-admin",
    is_flag=True,
    help="Give this user full super admin permissions.",
)
def create_user(email, full_name, password, super_admin):
    """Simple CLI script to create a user in the database."""

    async def _create():
        async with AsyncSessionLocal() as session:
            # Hash the password cleanly using bcrypt
            hashed_pw = hash_password(password)

            # Instantiating the user based exactly on your SQLAlchemy schema
            new_user = User(
                email=email,
                password_hash=hashed_pw,  # Fixed argument name
                full_name=full_name,
                is_active=True,
                is_super_admin=super_admin,
            )

            try:
                session.add(new_user)
                await session.commit()
                click.echo(f"🚀 Successfully created user: {email}")
                if super_admin:
                    click.echo("👑 User granted Super Admin privileges.")
            except Exception as e:
                await session.rollback()
                click.echo(f"❌ Error creating user: {e}", err=True)

    # Run the async execution block
    asyncio.run(_create())


if __name__ == "__main__":
    create_user()
