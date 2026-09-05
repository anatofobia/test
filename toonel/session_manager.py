"""
Pyrogram session management.
Handles first-time interactive login and SESSION_STRING persistence.
"""
import logging
from urllib.parse import parse_qs, unquote
from pyrogram import Client
from config import API_ID, API_HASH, PHONE_NUMBER, SESSION_STRING, ENV_PATH

logger = logging.getLogger(__name__)


def _save_session_string(session_str: str) -> None:
    """Write/update SESSION_STRING line in .env file."""
    lines: list[str] = []
    found = False

    if ENV_PATH.exists():
        with open(ENV_PATH, encoding="utf-8") as f:
            lines = f.readlines()
        for i, line in enumerate(lines):
            if line.startswith("SESSION_STRING="):
                lines[i] = f"SESSION_STRING={session_str}\n"
                found = True
                break

    if not found:
        lines.append(f"SESSION_STRING={session_str}\n")

    with open(ENV_PATH, "w", encoding="utf-8") as f:
        f.writelines(lines)

    logger.info("SESSION_STRING сохранён в .env")


def build_client() -> Client:
    """
    Build a Pyrogram Client.

    - If SESSION_STRING is set → use it (no phone/code needed).
    - Otherwise → interactive login via PHONE_NUMBER (or prompt).
    """
    if SESSION_STRING:
        logger.info("Используем SESSION_STRING из .env")
        return Client(
            name="tonnel",
            api_id=API_ID,
            api_hash=API_HASH,
            session_string=SESSION_STRING,
        )
    else:
        logger.info("SESSION_STRING не найден — будет интерактивный вход")
        kwargs: dict = {
            "name": "tonnel",
            "api_id": API_ID,
            "api_hash": API_HASH,
        }
        if PHONE_NUMBER:
            kwargs["phone_number"] = PHONE_NUMBER
        return Client(**kwargs)


async def init_session(app: Client) -> str:
    """
    Called INSIDE 'async with app:' block.
    Exports session string if we don't have one yet and saves it.
    Returns the session string.
    """
    me = await app.get_me()
    logger.info(f"Авторизован: {me.first_name} (ID: {me.id}, @{me.username})")

    if not SESSION_STRING:
        sess = await app.export_session_string()
        _save_session_string(sess)
        print(f"\n✅ SESSION_STRING сохранён в .env")
        print(f"   Скопируй его для переноса на другие серверы.\n")
        return sess

    return SESSION_STRING


async def fetch_tonnel_init_data(app: Client) -> str:
    """
    Open Tonnel Mini App via RequestWebView (MTProto) and extract
    tgWebAppData — the same authData that market.tonnel.network
    passes to Tonnel API.  No manual copy-paste needed.
    """
    from pyrogram.raw.functions.messages import RequestWebView

    bot_peer = await app.resolve_peer("Tonnel_Network_bot")

    result = await app.invoke(
        RequestWebView(
            peer=bot_peer,
            bot=bot_peer,
            url="https://market.tonnel.network",
            from_bot_menu=False,
            platform="android",
        )
    )

    # result.url looks like:
    # https://market.tonnel.network/#tgWebAppData=query_id%3D...&tgWebAppVersion=...
    url: str = result.url
    fragment = url.split("#", 1)[1] if "#" in url else ""
    params = parse_qs(fragment)

    raw = params.get("tgWebAppData", [None])[0]
    if not raw:
        raise RuntimeError(
            "Не удалось получить tgWebAppData из Mini App URL. "
            "Убедись, что @Tonnel_Network_bot подключён как Business Bot."
        )

    init_data = unquote(raw)
    logger.info("tgWebAppData (initData) получен из Tonnel Mini App")
    return init_data
