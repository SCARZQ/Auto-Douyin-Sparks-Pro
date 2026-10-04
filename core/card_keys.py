"""授权卡密：用户兑换后在现有到期时间上叠加天数。

卡密格式（24 位字符，去掉连字符后的长度）：
    前缀 = 天数编码（1 位 或 2 位），其余为随机字符，便于肉眼识别面额。
      天卡    -> 1XXXXXXXXXXXXXXXXXXXXXXX
      3天卡   -> 3XXXXXXXXXXXXXXXXXXXXXXX
      7天卡   -> 7XXXXXXXXXXXXXXXXXXXXXXX
      15天卡  -> 15XXXXXXXXXXXXXXXXXXXXXX
      30天卡  -> 30XXXXXXXXXXXXXXXXXXXXXX
    分组显示为 XXXX-XXXX-XXXX-XXXX-XXXX-XXXX（6 组 × 4 位）。

天数编码规则：
    1 / 3 / 7 / 9  -> 直接用 1 位数字（1,3,7,9 天）
    10 及以上      -> 用 2 位数字（10,15,30,90,180,365...）
"""

from __future__ import annotations

import os
import json
import secrets
import string
import threading
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
KEYS_PATH = DATA_DIR / "card_keys.json"

KEY_LEN = 24          # 卡密总长度（不含连字符）
GROUP = 4             # 分组长度

_lock = threading.Lock()

# 随机部分使用的字符集：去掉容易混淆的 0/O/1/I
_ALPHABET = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"


def _atomic_write_text(path, text: str) -> None:
    """原子写文件：先写同目录临时文件，再 os.replace 覆盖。

    直接 write_text 时若进程被中断（重启/断电/OOM），会留下半截 JSON，
    下次读取解析失败就会被当成空数据，导致用户/卡密/授权被整体清空。
    """
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)
    except Exception:
        try:
            if tmp.exists():
                tmp.unlink()
        except Exception:
            pass
        raise


def _backup_corrupt(path) -> None:
    """文件解析失败时先备份，避免随后一次写入把好数据也覆盖掉。"""
    try:
        if path.exists() and path.stat().st_size > 0:
            bak = path.with_suffix(path.suffix + ".corrupt")
            path.replace(bak)
    except Exception:
        pass


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _load() -> dict:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if not KEYS_PATH.exists():
        return {}
    try:
        data = json.loads(KEYS_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save(data: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    _atomic_write_text(KEYS_PATH, json.dumps(data, ensure_ascii=False, indent=2))


def day_prefix(days: int) -> str:
    """天数 -> 卡密前缀。1/3/7/9 用 1 位，其余用 2 位。"""
    d = int(days)
    if d in (1, 3, 7, 9):
        return str(d)
    return f"{d:02d}" if d < 100 else str(d)


def _format_key(raw: str) -> str:
    return "-".join(raw[i:i + GROUP] for i in range(0, len(raw), GROUP))


def _build_key(days: int) -> str:
    prefix = day_prefix(days)
    body_len = KEY_LEN - len(prefix)
    if body_len < 4:
        raise ValueError("天数过大，无法生成卡密")
    body = "".join(secrets.choice(_ALPHABET) for _ in range(body_len))
    return _format_key(prefix + body)


def query_card_key(key: str) -> dict:
    """按卡密查询状态（/kami 公开查询用）。

    返回 {found, used, days, used_at, created_at, note}。
    不返回使用者信息，避免被扫库。
    """
    key = (key or "").strip().upper()
    if not key:
        return {"found": False}

    try:
        info = find_card_key(key)
    except Exception:
        info = None
    if not info:
        return {"found": False}

    days = info.get("days")
    if not days:
        try:
            days = parse_days(info.get("key") or key)
        except Exception:
            days = 0

    def _fmt(v):
        """把时间字段统一成字符串。"""
        if not v:
            return ""
        if isinstance(v, (int, float)):
            try:
                return datetime.fromtimestamp(v).strftime("%Y-%m-%d %H:%M:%S")
            except Exception:
                return ""
        return str(v)[:19]

    return {
        "found": True,
        "used": bool(info.get("used")),
        "days": int(days or 0),
        "used_at": _fmt(info.get("used_at") or info.get("used_time")),
        "created_at": _fmt(info.get("created_at")),
        "note": str(info.get("note") or "")[:60],
    }


def parse_days(key: str) -> int | None:
    """从卡密前缀粗略反推天数（仅用于展示，权威值以存储的 days 字段为准）。

    逆推 day_prefix 的生成规则：
      1 位前缀：1 / 3 / 7 / 9      -> 首位非 2/4/5/6/8，且不是两位数面额的开头
      2 位前缀：10 / 15 / 30 / 60 / 90
      3 位前缀：180 / 365
    采用「最长已知前缀优先」匹配，避免 "15" 被误读为 "1"。
    """
    raw = "".join(c for c in str(key or "").upper() if c.isalnum())
    if not raw or not raw[0].isdigit():
        return None

    known = [1, 3, 7, 9, 10, 15, 30, 60, 90, 180, 365]
    # 前缀越长的面额越先匹配
    for d in sorted(known, key=lambda x: len(str(x)), reverse=True):
        if raw.startswith(str(d)):
            return d
    return None


def create_card_key(days: int, note: str = "", created_by: str = "") -> dict:
    days = int(days)
    if days <= 0 or days > 3650:
        raise ValueError("天数需在 1～3650 之间")

    with _lock:
        data = _load()
        # 避免极端情况下的重复
        for _ in range(50):
            key = _build_key(days)
            if key not in data:
                break
        info = {
            "key": key,
            "days": days,
            "note": (note or "").strip()[:80],
            "created_by": created_by or "",
            "created_at": _now(),
            "used": False,
            "used_by": None,
            "used_account": None,
            "used_at": None,
        }
        data[key] = info
        _save(data)
    return dict(info)


def list_card_keys() -> list[dict]:
    data = _load()
    items = list(data.values())
    items.sort(key=lambda x: x.get("created_at") or "", reverse=True)
    return items


def delete_card_key(key: str) -> None:
    key = (key or "").strip().upper()
    with _lock:
        data = _load()
        if key not in data:
            # 兼容无连字符
            plain = key.replace("-", "")
            for k in list(data.keys()):
                if k.replace("-", "") == plain:
                    key = k
                    break
            else:
                raise ValueError("卡密不存在")
        del data[key]
        _save(data)


def find_card_key(key: str) -> dict | None:
    """按卡密查找（忽略大小写与连字符），用于核销前预览。"""
    raw = (key or "").strip().upper()
    if not raw:
        return None
    plain = raw.replace("-", "")

    data = _load()
    info = data.get(raw)
    if not info:
        for k, v in data.items():
            if k.replace("-", "").upper() == plain:
                info = v
                break
    if not info:
        return None

    out = dict(info)
    # 面额兜底：老卡密若没有 days 字段，用前缀反推
    try:
        if not out.get("days"):
            d = parse_days(out.get("key") or raw)
            if d:
                out["days"] = d
    except Exception:
        pass
    return out


def redeem_card_key(key: str, username: str, account: str) -> dict:
    """
    兑换卡密：返回 days，调用方负责叠加到账号授权时间。
    卡密使用后标记 used。
    """
    raw = (key or "").strip().upper()
    if not raw:
        raise ValueError("请输入卡密")
    username = (username or "").strip()
    account = (account or "").strip()
    if not account:
        raise ValueError("请指定要授权的账号")

    with _lock:
        data = _load()
        info = data.get(raw)
        if not info:
            plain = raw.replace("-", "")
            for k, v in data.items():
                if k.replace("-", "") == plain:
                    info = v
                    raw = k
                    break
        if not info:
            raise ValueError("卡密无效")
        if info.get("used"):
            raise ValueError("卡密已被使用")
        days = int(info.get("days") or 0)
        if days <= 0:
            raise ValueError("卡密天数无效")
        data[raw] = {
            **info,
            "used": True,
            "used_by": username,
            "used_account": account,
            "used_at": _now(),
        }
        _save(data)
        return {
            "key": raw,
            "days": days,
            "account": account,
            "used_by": username,
        }


def create_card_keys_batch(days: int, count: int = 1, note: str = "", created_by: str = "") -> list:
    count = max(1, min(int(count or 1), 100))
    return [create_card_key(days, note=note, created_by=created_by) for _ in range(count)]
