#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import os
from pathlib import Path
from typing import Any

from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError
from telethon.sessions import StringSession


async def _run(api_id: int, api_hash: str, phone: str, output: Path, proxy: Any) -> None:
    client = TelegramClient(StringSession(), api_id, api_hash, proxy=proxy)
    await client.connect()
    try:
        sent = await client.send_code_request(phone)
        code = input("Enter Telegram login code: ").strip()
        try:
            await client.sign_in(phone=phone, code=code, phone_code_hash=sent.phone_code_hash)
        except SessionPasswordNeededError:
            password = input("Enter Telegram 2FA password: ").strip()
            await client.sign_in(password=password)

        if not await client.is_user_authorized():
            raise RuntimeError("Login failed: user is not authorized")

        session = client.session.save()
        if not session:
            raise RuntimeError("Failed to save StringSession")

        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(str(session), encoding="utf-8")
        print(f"StringSession saved to {output}")
    finally:
        await client.disconnect()


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate Telegram StringSession from phone login")
    parser.add_argument("--api-id", required=True, type=int)
    parser.add_argument("--api-hash", required=True)
    parser.add_argument("--phone", required=True)
    parser.add_argument("--output", default="data/new_tg_string_session.txt")
    parser.add_argument("--proxy-enabled", default=os.getenv("TG_PROXY_ENABLED", "false"))
    parser.add_argument("--proxy-type", default=os.getenv("TG_PROXY_TYPE", "socks5"))
    parser.add_argument("--proxy-host", default=os.getenv("TG_PROXY_HOST"))
    parser.add_argument("--proxy-port", default=os.getenv("TG_PROXY_PORT"))
    parser.add_argument("--proxy-username", default=os.getenv("TG_PROXY_USERNAME"))
    parser.add_argument("--proxy-password", default=os.getenv("TG_PROXY_PASSWORD"))
    parser.add_argument("--proxy-rdns", default=os.getenv("TG_PROXY_RDNS", "true"))
    args = parser.parse_args()
    proxy = _build_proxy(
        enabled_raw=args.proxy_enabled,
        proxy_type=args.proxy_type,
        host=args.proxy_host,
        port_raw=args.proxy_port,
        username=args.proxy_username,
        password=args.proxy_password,
        rdns_raw=args.proxy_rdns,
    )
    asyncio.run(_run(args.api_id, args.api_hash, args.phone, Path(args.output), proxy))


def _bool_parse(value: str) -> bool:
    normalized = (value or "").strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise RuntimeError("Boolean value expected for proxy flags")


def _build_proxy(
    *,
    enabled_raw: str,
    proxy_type: str,
    host: str | None,
    port_raw: str | None,
    username: str | None,
    password: str | None,
    rdns_raw: str,
):
    enabled = _bool_parse(enabled_raw)
    if not enabled:
        return None
    if not host:
        raise RuntimeError("proxy enabled but proxy host is missing")
    if not port_raw:
        raise RuntimeError("proxy enabled but proxy port is missing")
    try:
        port = int(port_raw)
    except ValueError as exc:
        raise RuntimeError("proxy port must be int") from exc
    if port <= 0:
        raise RuntimeError("proxy port must be > 0")
    ptype = (proxy_type or "").strip().lower()
    if ptype not in {"socks5", "socks4", "http"}:
        raise RuntimeError("proxy type must be one of: socks5, socks4, http")
    rdns = _bool_parse(rdns_raw)
    return (ptype, host, port, rdns, username or None, password or None)


if __name__ == "__main__":
    main()
