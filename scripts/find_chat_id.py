#!/usr/bin/env python3
"""Find the chat id of every chat your bot can see.

Run it locally. The token is asked for interactively, so it never lands in
your shell history, in a file, or in the repository.

    python3 scripts/find_chat_id.py

Optionally send a test message once you know the id:

    python3 scripts/find_chat_id.py --test -1001234567890

Standard library only.
"""

import getpass
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

API = "https://api.telegram.org/bot{token}/{method}"

KIND = {
    "private": "личный чат",
    "group": "группа",
    "supergroup": "супергруппа",
    "channel": "канал",
}


def call(token, method, params=None):
    url = API.format(token=token, method=method)
    if params:
        url += "?" + urllib.parse.urlencode(params)
    try:
        with urllib.request.urlopen(url, timeout=25) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read().decode("utf-8"))
        except ValueError:
            return {"ok": False, "description": f"HTTP {e.code}"}
    except (urllib.error.URLError, OSError) as e:
        return {"ok": False, "description": f"нет связи с Telegram: {e}"}


def get_token():
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if token:
        return token.strip()
    # Prompting only works with a real terminal. Run from a click-to-run button
    # there is nowhere to show the prompt, so say so instead of hanging.
    if not sys.stdin.isatty():
        print("""
Запустите этот скрипт в приложении «Терминал» — здесь ему негде спросить токен.

    cd ~/thai-tourism-dashboard
    python3 scripts/find_chat_id.py
""".strip(), file=sys.stderr)
        return ""
    print("Вставьте токен от @BotFather (при вводе он не отображается):")
    return getpass.getpass("токен: ").strip()


def collect_chats(updates):
    """Pull every distinct chat out of whatever update types came back."""
    chats, seen = [], set()
    for upd in updates:
        for key in ("message", "edited_message", "channel_post",
                    "edited_channel_post", "my_chat_member", "chat_member"):
            payload = upd.get(key)
            if not payload:
                continue
            chat = payload.get("chat")
            if chat and chat["id"] not in seen:
                seen.add(chat["id"])
                chats.append(chat)
    return chats


def main():
    args = sys.argv[1:]
    token = get_token()
    if not token:
        print("Токен не введён.", file=sys.stderr)
        return 1

    me = call(token, "getMe")
    if not me.get("ok"):
        print(f"Токен не принят: {me.get('description')}", file=sys.stderr)
        return 1
    bot = me["result"]
    print(f"\nБот: @{bot.get('username')} ({bot.get('first_name')})")

    if "--test" in args:
        i = args.index("--test")
        if i + 1 >= len(args):
            print("Укажите id после --test.", file=sys.stderr)
            return 1
        chat_id = args[i + 1]
        res = call(token, "sendMessage", {
            "chat_id": chat_id,
            "text": "Проверка связи. Если вы это видите — бот настроен верно.",
        })
        if res.get("ok"):
            print(f"Тестовое сообщение отправлено в {chat_id}.")
            return 0
        print(f"Не получилось: {res.get('description')}", file=sys.stderr)
        if "chat not found" in str(res.get("description", "")).lower():
            print("Проверьте знак минуса и то, что бот всё ещё в этом чате.",
                  file=sys.stderr)
        return 1

    upd = call(token, "getUpdates", {"limit": 100, "timeout": 0})
    if not upd.get("ok"):
        print(f"Ошибка: {upd.get('description')}", file=sys.stderr)
        return 1

    chats = collect_chats(upd.get("result", []))
    if not chats:
        print("""
Telegram пока не показал ни одного чата.

Обычно помогает одно из двух:

  1. Напишите любое сообщение в группе, где бот админ, и запустите скрипт снова.
  2. Если бот не админ — сделайте его админом. Бот без прав администратора
     видит в группе только команды вида /start@имя_бота.

Личный чат появится здесь после того, как вы нажмёте Start у самого бота.
""".strip(), file=sys.stderr)
        return 1

    print(f"\nНайдено чатов: {len(chats)}\n")
    for c in chats:
        name = c.get("title") or " ".join(
            filter(None, [c.get("first_name"), c.get("last_name")])
        ) or c.get("username") or "без названия"
        print(f"  {KIND.get(c['type'], c['type']):<14} {name}")
        print(f"  TELEGRAM_CHAT_ID = {c['id']}\n")

    print("Скопируйте нужный id в секрет TELEGRAM_CHAT_ID на GitHub.")
    print("Проверить связь: python3 scripts/find_chat_id.py --test <id>")
    return 0


if __name__ == "__main__":
    sys.exit(main())
