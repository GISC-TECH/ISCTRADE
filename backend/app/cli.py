"""Operator CLI (run inside the backend container): ``python -m app.cli <command>``.

    create-admin  --email E [--name N] [--password-stdin] [--promote]
        Bootstraps the platform super-admin (replaces "first signup becomes admin"). The password comes from
        BTP_ADMIN_PASSWORD, stdin (--password-stdin) or an interactive prompt; never from the command line.
        --promote turns an existing user into super-admin instead.
    create-invite [--email E] [--hours H] [--note T]
        Prints a single-use registration token (REGISTRATION_MODE=invite).
    registration-status
        Shows the effective registration mode and whether a super-admin exists.

Example: docker compose exec backend python -m app.cli create-admin --email voce@exemplo.com
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import os
import sys

from sqlalchemy import func, select

from app.core.config import settings
from app.core.database import AsyncSessionLocal, engine
from app.core.security import Role
from app.models import User
from app.services.audit import log_event
from app.services.registration import create_invite, create_user

MIN_PASSWORD = 8


def _read_password(from_stdin: bool) -> str:
    if env := os.environ.get("BTP_ADMIN_PASSWORD"):
        return env
    if from_stdin:
        return sys.stdin.readline().rstrip("\n")
    first = getpass.getpass("Senha do administrador: ")
    if getpass.getpass("Repita a senha: ") != first:
        raise SystemExit("As senhas não conferem")
    return first


async def create_admin(email: str, name: str | None, password: str | None, promote: bool) -> str:
    email = email.strip().lower()
    async with AsyncSessionLocal() as db:
        user = await db.scalar(select(User).where(User.email == email))
        if user is not None:
            if not promote:
                raise SystemExit(f"Usuário {email} já existe (use --promote para torná-lo super-admin)")
            user.role = Role.ADMIN
            user.is_active = True
            await log_event(db, "cli.user_promoted", user_id=user.id, email=email)
            await db.commit()
            return f"{email} agora é super-admin"
        if promote:
            raise SystemExit(f"Usuário {email} não existe")
        if password is None or len(password) < MIN_PASSWORD:
            raise SystemExit(f"Senha deve ter pelo menos {MIN_PASSWORD} caracteres")
        user = await create_user(db, email=email, password=password, full_name=name, role=Role.ADMIN)
        await log_event(db, "cli.admin_created", user_id=user.id, workspace_id=user.active_workspace_id, email=email)
        await db.commit()
        return f"Super-admin {email} criado"


async def new_invite(email: str | None, hours: int | None, note: str | None) -> str:
    async with AsyncSessionLocal() as db:
        invite, token = await create_invite(db, email=email, hours=hours, note=note)
        await log_event(db, "cli.invite_created", invite_id=str(invite.id), email=invite.email)
        await db.commit()
        warn = "" if settings.registration_mode == "invite" else (
            f"\n(atenção: REGISTRATION_MODE efetivo é '{settings.registration_mode}', não 'invite')"
        )  # fmt: skip
        return f"Convite (uso único, expira em {invite.expires_at:%Y-%m-%d %H:%M} UTC):\n{token}{warn}"


async def registration_status() -> str:
    async with AsyncSessionLocal() as db:
        admins = await db.scalar(select(func.count()).select_from(User).where(User.role == Role.ADMIN))
        users = await db.scalar(select(func.count()).select_from(User))
    return f"modo de cadastro: {settings.registration_mode}; usuários: {users}; super-admins: {admins}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.cli", description="ISCTRADE operator CLI")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("create-admin", help="cria (ou promove) o super-admin da plataforma")
    p.add_argument("--email", required=True)
    p.add_argument("--name")
    p.add_argument("--password-stdin", action="store_true")
    p.add_argument("--promote", action="store_true")
    p = sub.add_parser("create-invite", help="gera um convite de cadastro de uso único")
    p.add_argument("--email")
    p.add_argument("--hours", type=int)
    p.add_argument("--note")
    sub.add_parser("registration-status", help="mostra o modo de cadastro e se há super-admin")
    args = parser.parse_args(argv)

    def run(coro) -> str:
        async def _with_dispose():
            try:
                return await coro
            finally:
                await engine.dispose()

        return asyncio.run(_with_dispose())

    if args.command == "create-admin":
        password = None if args.promote else _read_password(args.password_stdin)
        out = run(create_admin(args.email, args.name, password, args.promote))
    elif args.command == "create-invite":
        out = run(new_invite(args.email, args.hours, args.note))
    else:
        out = run(registration_status())
    print(out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
