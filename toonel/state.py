"""
Persistent state: tracks which gifts have been transferred
and which Tonnel gift IDs have been listed, to avoid duplicates
across restarts.
"""
import json
import logging
import threading
from config import STATE_PATH

logger = logging.getLogger(__name__)

_state_lock = threading.Lock()

_DEFAULT = {
    "transferred_msg_ids": [],   # int: Telegram msg_id already sent to Tonnel
    "listed_gift_ids": [],       # int/str: Tonnel gift_id already listed for sale
}


def _load() -> dict:
    if STATE_PATH.exists():
        try:
            with open(STATE_PATH, encoding="utf-8") as f:
                data = json.load(f)
                # ensure all keys present
                for k, v in _DEFAULT.items():
                    data.setdefault(k, v)
                return data
        except (json.JSONDecodeError, OSError) as e:
            logger.warning(f"state.json повреждён, сбрасываю: {e}")
    return dict(_DEFAULT)


def _save(data: dict) -> None:
    try:
        with open(STATE_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
    except OSError as e:
        logger.error(f"Не удалось сохранить state.json: {e}")


# ── Public API ───────────────────────────────────────────

def is_transferred(msg_id: int) -> bool:
    with _state_lock:
        return msg_id in _load()["transferred_msg_ids"]


def mark_transferred(msg_id: int) -> None:
    with _state_lock:
        data = _load()
        if msg_id not in data["transferred_msg_ids"]:
            data["transferred_msg_ids"].append(msg_id)
            _save(data)


def is_listed(gift_id) -> bool:
    with _state_lock:
        return str(gift_id) in [str(x) for x in _load()["listed_gift_ids"]]


def mark_listed(gift_id) -> None:
    with _state_lock:
        data = _load()
        sid = str(gift_id)
        if sid not in [str(x) for x in data["listed_gift_ids"]]:
            data["listed_gift_ids"].append(sid)
            _save(data)


def get_summary() -> dict:
    data = _load()
    return {
        "transferred": len(data["transferred_msg_ids"]),
        "listed": len(data["listed_gift_ids"]),
    }
