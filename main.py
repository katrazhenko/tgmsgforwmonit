import json
import asyncio
import os
import sys
import re
import logging
import atexit
from pathlib import Path
from typing import Optional
from telethon import TelegramClient, events
from telethon.tl.functions.channels import JoinChannelRequest, LeaveChannelRequest
from telethon.errors import FloodWaitError
from telethon.tl.types import InputChannel
from dotenv import set_key
# --- Логування ---
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("monitor.log", encoding="utf-8"),
    ],
)
log = logging.getLogger(__name__)

try:
    from openai import OpenAI
    OPENAI_AVAILABLE = True
except ImportError:
    OpenAI = None
    OPENAI_AVAILABLE = False
    log.warning("OpenAI не встановлено. pip install openai")

# ──────────────────────────────────────────────────────────────
# Захист від подвійного запуску
# ──────────────────────────────────────────────────────────────
LOCK_FILE = Path("monitor.lock")
EFP = Path(".env")
if LOCK_FILE.exists():
    log.error("Скрипт вже запущено! Зупини попередній або видали monitor.lock")
    sys.exit(1)

LOCK_FILE.write_text(str(os.getpid()))


@atexit.register
def _remove_lock():
    LOCK_FILE.unlink(missing_ok=True)


# ──────────────────────────────────────────────────────────────
# Налаштування — спочатку .env / env-змінні, потім fallback
# ──────────────────────────────────────────────────────────────
def _load_env():
    """Завантажує .env якщо він є (без залежності від python-dotenv)."""
    env_path = Path(".env")
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_env()

API_ID = int(os.environ.get("TG_API_ID", "0"))
API_HASH = os.environ.get("TG_API_HASH", "")
PHONE = os.environ.get("TG_PHONE", "")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")

if not all([API_ID, API_HASH, PHONE]):
    log.error(
        "Задай TG_API_ID, TG_API_HASH, TG_PHONE у файлі .env або змінних оточення"
    )
    sys.exit(1)

CONFIG_FILE = Path("config.json")

# ──────────────────────────────────────────────────────────────
# Кешований конфіг + lock
# ──────────────────────────────────────────────────────────────
_config_cache: Optional[dict] = None
_config_lock = asyncio.Lock()


def load_config() -> dict:
    """Завжди читає з диска (синхронно). Використовуй всередині lock."""
    if not CONFIG_FILE.exists():
        return {}
    with CONFIG_FILE.open(encoding="utf-8") as f:
        return json.load(f)


def save_config(config: dict) -> None:
    """Атомарне збереження через тимчасовий файл."""
    tmp = CONFIG_FILE.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(config, f, ensure_ascii=False, indent=2)
    tmp.replace(CONFIG_FILE)


async def get_config() -> dict:
    """Повертає кешований конфіг; при першому виклику читає з диска."""
    global _config_cache
    async with _config_lock:
        if _config_cache is None:
            _config_cache = load_config()
        return dict(_config_cache)  # shallow copy


async def update_config(config: dict) -> None:
    """Зберігає конфіг та оновлює кеш."""
    global _config_cache
    async with _config_lock:
        save_config(config)
        _config_cache = config


def invalidate_config_cache() -> None:
    global _config_cache
    _config_cache = None


# ──────────────────────────────────────────────────────────────
# OpenAI синглтон
# ──────────────────────────────────────────────────────────────
_openai_client: Optional["OpenAI"] = None
_openai_key_used: str = ""


def get_openai_client(api_key: str) -> Optional["OpenAI"]:
    global _openai_client, _openai_key_used
    if not OPENAI_AVAILABLE:
        return None
    if _openai_client is None or _openai_key_used != api_key:
        _openai_client = OpenAI(api_key=api_key)
        _openai_key_used = api_key
    return _openai_client


# ──────────────────────────────────────────────────────────────
# Статистика AI
# ──────────────────────────────────────────────────────────────
ai_stats = {"checked": 0, "passed": 0, "filtered": 0}

# ──────────────────────────────────────────────────────────────
# Черга пересилки
# ──────────────────────────────────────────────────────────────
pending_messages: asyncio.Queue = asyncio.Queue()

# ──────────────────────────────────────────────────────────────
# Telethon клієнт
# ──────────────────────────────────────────────────────────────
client = TelegramClient(PHONE.replace("+", ""), API_ID, API_HASH)


# ──────────────────────────────────────────────────────────────
# Утиліти: очищення minus_words
# ──────────────────────────────────────────────────────────────
def clean_minus_words(minus_words: list[str], skip_words: list[str], keywords: list[str]) -> list[str]:
    """
    Видаляє зі списку мінус-слів ті слова, що є в skip_words або keywords.
    Повертає новий (очищений) список. НЕ мутує оригінал.
    """
    skip_set = {w.lower() for w in skip_words}
    kw_set = {w.lower() for w in keywords}
    forbidden = skip_set | kw_set

    result: list[str] = []
    seen: set[str] = set()

    for minus in minus_words:
        cleaned = minus
        for token in re.findall(r'\b\w+\b', minus):
            if token.lower() in forbidden:
                cleaned = re.sub(
                    r'\b' + re.escape(token) + r'\b',
                    '',
                    cleaned,
                    flags=re.IGNORECASE,
                )
        cleaned = re.sub(r'\s+', ' ', cleaned).strip()
        if cleaned and cleaned.lower() not in seen:
            result.append(cleaned)
            seen.add(cleaned.lower())

    return result


def has_minus_word(text: str, minus_words: list[str]) -> bool:
    """True якщо текст містить будь-яке мінус-слово."""
    text_lower = text.lower()
    for phrase in minus_words:
        # Фраза цілком (без word-boundary для multi-word)
        if phrase.lower() in text_lower:
            return True
    return False


def find_keyword(text: str, keywords: list[str]) -> Optional[str]:
    """
    Повертає перше знайдене ключове слово або None.
    Виправлено: break обох циклів після знахідки.
    """
    text_lower = text.lower()
    for keyword in keywords:
        # Перевіряємо кожне слово ключової фрази
        tokens = re.findall(r'\b\w+\b', keyword)
        if all(
                re.search(r'\b' + re.escape(tok) + r'\b', text_lower, re.IGNORECASE)
                for tok in tokens
        ):
            return keyword
    return None


# ──────────────────────────────────────────────────────────────
# AI фільтрація
# ──────────────────────────────────────────────────────────────
async def ai_filter_message(text: str, keyword: str, chat_name: str) -> bool:
    """True = цільове, False = спам/реклама."""
    config = await get_config()

    if not config.get("ai_filter_enabled", False):
        return True

    if not OPENAI_AVAILABLE:
        log.warning("OpenAI недоступний — пропускаю без фільтрації")
        return True

    if not OPENAI_API_KEY or OPENAI_API_KEY == "YOUR_OPENAI_API_KEY":
        log.warning("OpenAI API ключ не налаштовано — пропускаю без фільтрації")
        return True

    oai = get_openai_client(OPENAI_API_KEY)
    if oai is None:
        return True

    try:
        keywords_str = ", ".join(config.get("keywords", [])[:50])
        minus_words_str = ", ".join(config.get("minus_words", [])[:20])
        ai_main_filter_role = config.get("ai_main_filter_role", "")
        ai_tagret_filter_criteria = config.get("ai_tagret_filter_criteria", "")
        ai_spam_filter_criteria = config.get("ai_spam_filter_criteria", "")
        instructions = (
            f'{ai_main_filter_role}\n'
            f'Визнач: ЦІЛЬОВЕ повідомлення (справжній запит/питання) чи РЕКЛАМА/СПАМ.\n\n'
            f'Контекст:\n'
            f'- Знайдено ключове слово: "{keyword}"\n'
            f'- Група: {chat_name}\n'
            f'- Бажані теми: {keywords_str}\n'
            f'- Стоп-слова (спам якщо відповідає): {minus_words_str}\n\n'
            f'Критерії ЦІЛЬОВОГО:\n{ai_tagret_filter_criteria}\n'
            f'Критерії СПАМУ (БЛОКУВАТИ):\n{ai_spam_filter_criteria}\n'
            f'Відповідай ТІЛЬКИ: TARGET або SPAM'
        )

        response = oai.responses.create(
            model=config.get("openai_model", "gpt-4o-mini"),
            instructions=instructions,
            input=f"Повідомлення:\n{text}"
        )

        result = response.output_text.upper()
        ai_stats["checked"] += 1

        if "TARGET" in result:
            ai_stats["passed"] += 1
            log.info(f"🤖 AI ПРОПУСТИВ: {text[:60]}…")
            return True
        else:
            ai_stats["filtered"] += 1
            log.info(f"🤖 AI ЗАБЛОКУВАВ: {text[:60]}…")
            return False

    except Exception as exc:
        log.error(f"Помилка AI фільтрації: {exc}")
        return True  # при помилці — пропускаємо


# ──────────────────────────────────────────────────────────────
# Безпечна відправка
# ──────────────────────────────────────────────────────────────
async def safe_send(destination: str, text: str, max_retries: int = 5) -> None:
    """Надсилає з автоматичним FloodWait retry."""
    for attempt in range(max_retries):
        try:
            await client.send_message(destination, text)
            return
        except FloodWaitError as exc:
            wait = exc.seconds + 5
            log.warning(f"FloodWait: чекаю {wait}с… (спроба {attempt + 1}/{max_retries})")
            await asyncio.sleep(wait)
        except Exception as exc:
            log.error(f"Помилка відправки в {destination}: {exc}")
            return
    log.error(f"safe_send: не вдалося після {max_retries} спроб у {destination}")


async def send_long_message(destination: str, text: str, max_length: int = 4000) -> None:
    """Розбиває довге повідомлення на частини."""
    if len(text) <= max_length:
        await safe_send(destination, text)
        return

    parts: list[str] = []
    current = ""
    for line in text.split('\n'):
        chunk = line + '\n'
        if len(current) + len(chunk) <= max_length:
            current += chunk
        else:
            if current:
                parts.append(current)
            current = chunk
    if current:
        parts.append(current)

    for i, part in enumerate(parts, 1):
        header = f"📄 Частина {i}/{len(parts)}\n\n" if len(parts) > 1 else ""
        await safe_send(destination, header + part)
        await asyncio.sleep(1)


# ──────────────────────────────────────────────────────────────
# Фонова пересилка
# ──────────────────────────────────────────────────────────────
async def background_forwarder() -> None:
    log.info("🔄 Запущено фонову пересилку повідомлень")
    while True:
        try:
            msg_data = await pending_messages.get()
            config = await get_config()
            fwd_ch = config.get("forward_channel")

            if not fwd_ch:
                log.warning("Канал для пересилки не налаштовано!")
                pending_messages.task_done()
                continue

            forward_text = (
                f"🔔 Знайдено: **{msg_data['keyword']}**\n"
                f"📢 Чат: {msg_data['chat']}\n"
                f"👤 Від: {msg_data['sender']}\n\n"
                f"💬 {msg_data['text']}"
            )

            await safe_send(fwd_ch, forward_text)
            log.info(f"✅ Переслано в {fwd_ch} з {msg_data['chat']}")
            await asyncio.sleep(3)
            pending_messages.task_done()

        except Exception as exc:
            log.error(f"Помилка в фоновій пересилці: {exc}")
            try:
                pending_messages.task_done()
            except ValueError:
                pass
            await asyncio.sleep(5)


# ──────────────────────────────────────────────────────────────
# Вступ у групи (фон)
# ──────────────────────────────────────────────────────────────
async def join_all_background(queue: list[str], config: dict) -> None:
    success, failed = [], []
    admins = config.get("admins", [])

    for i, group in enumerate(queue, 1):
        try:
            await client(JoinChannelRequest(group))
            success.append(group)
            for admin in admins:
                await safe_send(admin, f"✅ [{i}/{len(queue)}] Вступив: {group}")
        except Exception as exc:
            failed.append(f"{group} — {exc}")
            for admin in admins:
                await safe_send(admin, f"❌ [{i}/{len(queue)}] Помилка: {group}\n{exc}")

        await asyncio.sleep(15)

    fresh = load_config()
    fresh["join_queue"] = [g for g in fresh.get("join_queue", []) if g not in success]
    await update_config(fresh)

    msg = f"🏁 **Готово!**\n✅ Вступив: {len(success)}\n❌ Помилок: {len(failed)}"
    if failed:
        msg += "\n\n❌ Не вдалось:\n" + "\n".join(f"  • {f}" for f in failed)
    for admin in admins:
        await safe_send(admin, msg)


# ──────────────────────────────────────────────────────────────
# Допоміжна: форматування відправника
# ──────────────────────────────────────────────────────────────
def format_sender(sender) -> str:
    """Повертає читабельний рядок з іменем/username відправника."""
    first = getattr(sender, "first_name", "") or ""
    last = getattr(sender, "last_name", "") or ""
    # ВИПРАВЛЕНО: було " username" (з пробілом) — атрибут ніколи не знаходився
    uname = getattr(sender, "username", None)
    uid = getattr(sender, "id", None)

    name = f"{first} {last}".strip()
    if uname:
        tag = f"[ @{uname} ]"
    elif uid:
        tag = f"[ {uid} ]"
    else:
        tag = ""
    return f"{name} {tag}".strip()


def format_chat(chat) -> str:
    title = getattr(chat, "title", None) or ""
    username = getattr(chat, "username", None)
    suffix = f" [ @{username} ]" if username else ""
    return f"{title}{suffix}"


# ──────────────────────────────────────────────────────────────
# Перевірка прав адміна
# ──────────────────────────────────────────────────────────────
def is_admin(chat_username: str, admins: list[str]) -> bool:
    return ("@" + chat_username.lower()) in {a.lower() for a in admins}


# ──────────────────────────────────────────────────────────────
# Моніторинг повідомлень
# ──────────────────────────────────────────────────────────────
@client.on(events.NewMessage(incoming=True))
async def monitor(event):
    text = event.message.text
    if not text:
        return

    config = await get_config()

    # Виключити чати з адмінами зі списку моніторингу
    chat = await event.get_chat()
    chat_username = getattr(chat, "username", "") or ""
    if chat_username and is_admin(chat_username, config.get("admins", [])):
        return

    # Перевірка мінус-слів
    if has_minus_word(text, config.get("minus_words", [])):
        return

    # Пошук ключового слова (виправлено: правильний break)
    found_keyword = find_keyword(text, config.get("keywords", []))
    if not found_keyword:
        return

    sender = await event.get_sender()
    chat_name = format_chat(chat)
    sender_name = format_sender(sender)

    # AI фільтрація
    if not await ai_filter_message(text, found_keyword, chat_name):
        log.info(f"🚫 AI відфільтрував повідомлення з {chat_name}")
        return

    display_text = text if len(text) <= 1000 else text[:1000] + "…"
    await pending_messages.put({
        "keyword": found_keyword,
        "chat": chat_name,
        "sender": sender_name,
        "text": display_text,
    })
    log.info(f"📥 Додано в чергу з {chat_name} (черга: {pending_messages.qsize()})")


# ──────────────────────────────────────────────────────────────
# Команди адміністратора
# ──────────────────────────────────────────────────────────────
@client.on(events.NewMessage(outgoing=False, pattern=r'^/'))
async def commands(event):
    config = await get_config()
    chat = await event.get_chat()
    chat_username = getattr(chat, "username", "") or ""

    if not is_admin(chat_username, config.get("admins", [])):
        return

    text = event.message.text.strip()
    parts = text.split(maxsplit=1)
    cmd = parts[0].lower()
    arg = parts[1].strip() if len(parts) > 1 else ""

    # === AI ===
    if cmd == "/ai_enable":
        if not OPENAI_AVAILABLE:
            await event.reply("❌ OpenAI не встановлено: pip install openai")
            return
        if not OPENAI_API_KEY or OPENAI_API_KEY == "YOUR_OPENAI_API_KEY":
            await event.reply("❌ Спочатку задай ключ: /ai_set_key sk-…")
            return
        config["ai_filter_enabled"] = True
        await update_config(config)
        await event.reply("✅ AI фільтрація УВІМКНЕНА")

    elif cmd == "/ai_disable":
        config["ai_filter_enabled"] = False
        await update_config(config)
        await event.reply("🔴 AI фільтрація ВИМКНЕНА")

    elif cmd == "/ai_set_key":
        if not arg:
            await event.reply("❌ /ai_set_key sk-proj-…")
            return
        set_key(dotenv_path=EFP, key_to_set="OPENAI_API_KEY", value_to_set=arg)
        os.putenv("OPENAI_API_KEY", arg)
        await event.reply(f"✅ Ключ збережено: {arg[:10]}…{arg[-4:]}\nВикористай /ai_enable")

    elif cmd == "/ai_set_model":
        if not arg:
            await event.reply(
                "❌ Вкажи модель:\n"
                "/ai_set_model gpt-4o-mini (дешево)\n"
                "/ai_set_model gpt-4o (точніше)"
            )
            return
        config["openai_model"] = arg
        await update_config(config)
        await event.reply(f"✅ Модель: {arg}")

    elif cmd == "/ai_status":
        enabled = config.get("ai_filter_enabled", False)
        key_ok = bool(config.get("openai_api_key")) and config.get("openai_api_key") != "YOUR_OPENAI_API_KEY"
        await event.reply(
            f"🤖 **AI фільтрація:**\n"
            f"{'🟢 УВІМКНЕНА' if enabled else '🔴 ВИМКНЕНА'}\n"
            f"🔑 Ключ: {'✅' if key_ok else '❌ не налаштовано'}\n"
            f"🧠 Модель: {config.get('openai_model', 'gpt-4o-mini')}\n"
            f"🎭 Роль: {'✅' if config.get('ai_main_filter_role') else '❌ не задано'}\n"
            f"🎯 Критерії цільового: {'✅' if config.get('ai_tagret_filter_criteria') else '❌ не задано'}\n"
            f"� Критерії спаму: {'✅' if config.get('ai_spam_filter_criteria') else '❌ не задано'}\n"
            f"�📊 Перевірено: {ai_stats['checked']} | "
            f"Пропущено: {ai_stats['passed']} | "
            f"Відфільтровано: {ai_stats['filtered']}"
        )

    elif cmd == "/ai_test":
        if not arg:
            await event.reply("❌ /ai_test <текст>")
            return
        await event.reply("🤖 Тестую…")
        result = await ai_filter_message(arg, "ситу", "test_chat")
        await event.reply("✅ AI ПРОПУСТИВ (цільове)" if result else "🚫 AI ЗАБЛОКУВАВ (спам)")

    # === AI ролі та критерії ===
    elif cmd == "/ai_set_role":
        if not arg:
            await event.reply("❌ /ai_set_role <текст ролі AI>")
            return
        config["ai_main_filter_role"] = arg
        await update_config(config)
        await event.reply(f"✅ AI роль встановлено:\n{arg[:200]}")

    elif cmd == "/ai_get_role":
        role = config.get("ai_main_filter_role", "")
        await event.reply(f"🎭 **AI роль:**\n{role}" if role else "❌ AI роль не налаштовано")

    elif cmd == "/ai_set_target":
        if not arg:
            await event.reply("❌ /ai_set_target <критерії цільового повідомлення>")
            return
        config["ai_tagret_filter_criteria"] = arg
        await update_config(config)
        await event.reply(f"✅ Критерії ЦІЛЬОВОГО встановлено:\n{arg[:200]}")

    elif cmd == "/ai_get_target":
        criteria = config.get("ai_tagret_filter_criteria", "")
        await event.reply(f"🎯 **Критерії ЦІЛЬОВОГО:**\n{criteria}" if criteria else "❌ Критерії цільового не налаштовано")

    elif cmd == "/ai_set_spam":
        if not arg:
            await event.reply("❌ /ai_set_spam <критерії спаму>")
            return
        config["ai_spam_filter_criteria"] = arg
        await update_config(config)
        await event.reply(f"✅ Критерії СПАМУ встановлено:\n{arg[:200]}")

    elif cmd == "/ai_get_spam":
        criteria = config.get("ai_spam_filter_criteria", "")
        await event.reply(f"🚫 **Критерії СПАМУ:**\n{criteria}" if criteria else "❌ Критерії спаму не налаштовано")

    # === Канал ===
    elif cmd == "/set_channel":
        if not arg:
            await event.reply("❌ /set_channel @канал")
            return
        try:
            entity = await client.get_entity(arg)
            config["forward_channel"] = arg
            await update_config(config)
            await event.reply(
                f"✅ Канал: **{arg}**\n"
                f"Назва: {getattr(entity, 'title', '?')}\n"
                f"⚠️ Переконайся що акаунт є адміном каналу!"
            )
        except Exception as exc:
            await event.reply(f"❌ Помилка доступу до каналу: {exc}")

    elif cmd == "/get_channel":
        ch = config.get("forward_channel")
        await event.reply(f"📢 Канал: **{ch}**" if ch else "❌ Канал не налаштовано")

    # === Адміни ===
    elif cmd == "/add_admin":
        if not arg:
            await event.reply("❌ /add_admin @username")
            return
        admins = config.get("admins", [])
        if arg.lower() in {a.lower() for a in admins}:
            await event.reply("⚠️ Адмін вже є")
        else:
            admins.append(arg)
            config["admins"] = admins
            await update_config(config)
            await event.reply(f"✅ Додано адміна: **{arg}**")

    elif cmd == "/del_admin":
        if "@" + chat_username.lower() == arg.lower():
            await event.reply("❌ Не можна видалити себе")
            return
        admins = config.get("admins", [])
        new_admins = [a for a in admins if a.lower() != arg.lower()]
        if len(new_admins) < len(admins):
            config["admins"] = new_admins
            await update_config(config)
            await event.reply(f"🗑 Видалено: **{arg}**")
        else:
            await event.reply("❌ Адміна не знайдено")

    # === Ключові слова ===
    elif cmd == "/add_word":
        if not arg:
            await event.reply("❌ /add_word <слово>")
            return
        kw = config.get("keywords", [])
        if arg.lower() in {w.lower() for w in kw}:
            await event.reply("⚠️ Вже є")
        else:
            kw.append(arg)
            config["keywords"] = kw
            await update_config(config)
            await event.reply(f"✅ Додано: **{arg}**")

    elif cmd == "/del_word":
        kw = config.get("keywords", [])
        new_kw = [w for w in kw if w.lower() != arg.lower()]
        if len(new_kw) < len(kw):
            config["keywords"] = new_kw
            await update_config(config)
            await event.reply(f"🗑 Видалено: **{arg}**")
        else:
            await event.reply("❌ Не знайдено")

    # === Мінус-слова ===
    elif cmd == "/add_minus":
        if not arg:
            await event.reply("❌ /add_minus <слово>")
            return
        mw = config.get("minus_words", [])
        if arg.lower() in {w.lower() for w in mw}:
            await event.reply("⚠️ Вже є")
        else:
            mw.append(arg)
            config["minus_words"] = mw
            await update_config(config)
            await event.reply(f"✅ Додано мінус-слово: **{arg}**")

    elif cmd == "/del_minus":
        mw = config.get("minus_words", [])
        new_mw = [w for w in mw if w.lower() != arg.lower()]
        if len(new_mw) < len(mw):
            config["minus_words"] = new_mw
            await update_config(config)
            await event.reply(f"🗑 Видалено: **{arg}**")
        else:
            await event.reply("❌ Не знайдено")

    # === Skip-слова ===
    elif cmd == "/add_skip":
        if not arg:
            await event.reply("❌ /add_skip <слово>")
            return
        sw = config.get("skip_words", [])
        if arg.lower() in {w.lower() for w in sw}:
            await event.reply("⚠️ Вже є")
        else:
            sw.append(arg)
            config["skip_words"] = sw
            await update_config(config)
            await event.reply(f"✅ Додано skip: **{arg}**")

    elif cmd == "/del_skip":
        if not arg:
            await event.reply("❌ /del_skip <слово>")
            return
        sw = config.get("skip_words", [])
        new_sw = [w for w in sw if w.lower() != arg.lower()]
        if len(new_sw) < len(sw):
            config["skip_words"] = new_sw
            await update_config(config)
            await event.reply(f"🗑 Видалено: **{arg}**")
        else:
            await event.reply("❌ Не знайдено")

    # === Статус черги ===
    elif cmd == "/queue_status":
        await event.reply(
            f"📊 **Черга пересилки:**\n"
            f"📥 У черзі: {pending_messages.qsize()} повідомлень\n"
            f"📢 Канал: {config.get('forward_channel', 'не встановлено')}\n"
            f"⏱ Затримка: 3 сек"
        )

    # === Очищення minus_words ===
    elif cmd == "/clean_minus":
        old = config.get("minus_words", [])
        new = clean_minus_words(old, config.get("skip_words", []), config.get("keywords", []))
        diff = len(old) - len(new)
        config["minus_words"] = new
        await update_config(config)
        await event.reply(
            f"🧹 Очищено minus_words\n"
            f"Було: {len(old)} | Стало: {len(new)} | Видалено: {diff}"
        )

    # === Список налаштувань ===
    elif cmd == "/list":
        kw = "\n".join(f"  • {w}" for w in config.get("keywords", [])) or "  (пусто)"
        mw = "\n".join(f"  • {w}" for w in config.get("minus_words", [])) or "  (пусто)"
        sw = "\n".join(f"  • {w}" for w in config.get("skip_words", [])) or "  (пусто)"
        jq = "\n".join(f"  • {g}" for g in config.get("join_queue", [])) or "  (пусто)"
        adm = "\n".join(f"  • {a}" for a in config.get("admins", [])) or "  (пусто)"
        ch = config.get("forward_channel", "не встановлено")
        ai_st = "🟢 УВІМКНЕНА" if config.get("ai_filter_enabled") else "🔴 ВИМКНЕНА"

        text_out = (
            f"📋 **Поточні налаштування:**\n\n"
            f"👤 Адміни:\n{adm}\n\n"
            f"📢 Канал пересилки: {ch}\n\n"
            f"🤖 AI фільтрація: {ai_st}\n\n"
            f"🔍 Ключові слова:\n{kw}\n\n"
            f"🚫 Мінус-слова:\n{mw}\n\n"
            f"⏭️ Skip-слова:\n{sw}\n\n"
            f"📥 Черга груп:\n{jq}"
        )
        await send_long_message('@' + chat_username, text_out)

    # === Групи ===
    elif cmd == "/join_add":
        if not arg:
            await event.reply("❌ /join_add @г1 @г2 …")
            return
        new_groups = [g.strip() for g in arg.replace("\n", " ").split() if g.startswith("@")]
        if not new_groups:
            await event.reply("❌ Групи мають починатися з @")
            return

        queue = config.get("join_queue", [])
        q_lower = {g.lower() for g in queue}
        added, skipped = [], []
        for g in new_groups:
            if g.lower() not in q_lower:
                queue.append(g)
                q_lower.add(g.lower())
                added.append(g)
            else:
                skipped.append(g)

        config["join_queue"] = queue
        await update_config(config)

        msg = ""
        if added:
            msg += f"✅ Додано ({len(added)}):\n" + "\n".join(f"  • {g}" for g in added)
        if skipped:
            msg += f"\n\n⚠️ Вже були ({len(skipped)}):\n" + "\n".join(f"  • {g}" for g in skipped)
        msg += f"\n\n📥 Всього: {len(queue)} груп. /join_all — вступити у всі"
        await send_long_message('@' + chat_username, msg)

    elif cmd == "/join_del":
        if not arg:
            await event.reply("❌ /join_del @група")
            return
        queue = config.get("join_queue", [])
        new_q = [g for g in queue if g.lower() != arg.lower()]
        if len(new_q) < len(queue):
            config["join_queue"] = new_q
            await update_config(config)
            await event.reply(f"🗑 Видалено: **{arg}**")
        else:
            await event.reply("❌ Не знайдено в черзі")

    elif cmd == "/join_list":
        queue = config.get("join_queue", [])
        if not queue:
            await event.reply("📭 Черга порожня. /join_add @г1 @г2")
            return
        lines = "\n".join(f"  {i + 1}. {g}" for i, g in enumerate(queue))
        await send_long_message('@' + chat_username,
                                f"📥 **Черга ({len(queue)}):**\n\n{lines}\n\n/join_all — вступити у всі"
                                )

    elif cmd == "/join_all":
        queue = config.get("join_queue", [])
        if not queue:
            await event.reply("📭 Черга порожня")
            return
        await event.reply(f"🚀 Вступаю у {len(queue)} груп(и) у фоні…")
        asyncio.create_task(join_all_background(queue, config))

    elif cmd == "/join":
        if not arg:
            await event.reply("❌ /join @група")
            return
        try:
            await client(JoinChannelRequest(arg))
            await event.reply(f"✅ Вступив: **{arg}**")
        except Exception as exc:
            await event.reply(f"❌ Помилка: {exc}")

    elif cmd == "/leave":
        if not arg:
            await event.reply("❌ /leave @група")
            return
        try:
            await client(LeaveChannelRequest(arg))
            await event.reply(f"✅ Вийшов: **{arg}**")
        except Exception as exc:
            await event.reply(f"❌ Помилка: {exc}")

    elif cmd == "/groups":
        dialogs = await client.get_dialogs()
        groups = [d for d in dialogs if d.is_group or d.is_channel]
        if not groups:
            await event.reply("📭 Немає груп/каналів")
            return
        lines = "\n".join(
            f"  • {g.title} (@{g.entity.username})" if getattr(g.entity, "username", None)
            else f"  • {g.title}"
            for g in groups
        )
        await send_long_message('@' + chat_username, f"📋 **Групи ({len(groups)}):**\n\n{lines}")

    elif cmd == "/help":
        help_text = (
            "📖 **Команди управління:**\n\n"
            "🤖 **AI Фільтрація:**\n"
            "/ai_enable — увімкнути\n"
            "/ai_disable — вимкнути\n"
            "/ai_set_key [ключ] — OpenAI ключ\n"
            "/ai_set_model [модель] — модель GPT\n"
            "/ai_set_role [текст] — задати роль AI\n"
            "/ai_get_role — поточна роль AI\n"
            "/ai_set_target [текст] — критерії цільового\n"
            "/ai_get_target — поточні критерії цільового\n"
            "/ai_set_spam [текст] — критерії спаму\n"
            "/ai_get_spam — поточні критерії спаму\n"
            "/ai_status — статус і статистика\n"
            "/ai_test [текст] — протестувати\n\n"
            "👤 **Адміни:**\n"
            "/add_admin @user — додати\n"
            "/del_admin @user — видалити\n\n"
            "📢 **Канал:**\n"
            "/set_channel @к — встановити\n"
            "/get_channel — поточний\n"
            "/queue_status — статус черги\n\n"
            "🔍 **Ключові слова:**\n"
            "/add_word [слово] — додати\n"
            "/del_word [слово] — видалити\n\n"
            "🚫 **Мінус-слова:**\n"
            "/add_minus [слово] — додати\n"
            "/del_minus [слово] — видалити\n"
            "/clean_minus — очистити дублі/skip\n\n"
            "⏭️ **Skip-слова:**\n"
            "/add_skip [слово] — додати\n"
            "/del_skip [слово] — видалити\n\n"
            "📥 **Групи:**\n"
            "/join_add @г1 @г2 — додати в чергу\n"
            "/join_del @г — видалити з черги\n"
            "/join_list — показати чергу\n"
            "/join_all — вступити у всі\n"
            "/join @г — вступити в одну\n"
            "/leave @г — вийти\n\n"
            "⚙️ **Інше:**\n"
            "/groups — всі групи\n"
            "/list — всі налаштування\n"
            "/help — ця довідка"
        )
        await send_long_message('@' + chat_username, help_text)


# ──────────────────────────────────────────────────────────────
# Точка входу
# ──────────────────────────────────────────────────────────────
async def main():
    await client.start()
    log.info("✅ Бот запущено")
    log.info(f"🤖 OpenAI: {'доступний' if OPENAI_AVAILABLE else 'не встановлено'}")
    asyncio.create_task(background_forwarder())
    await client.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())
