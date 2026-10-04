"""配置管理 - 支持独立/授权两种模式"""

from __future__ import annotations

import os
import json
import logging
import shutil
import threading
from pathlib import Path
from datetime import datetime, timedelta

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
ACCOUNTS_DIR = DATA_DIR / "accounts"
# 独立授权文件：存放授权到期时间与等级(VIP)。
# 单独存一个文件，用户清理抖音数据时不会被删除。
LICENSE_PATH = DATA_DIR / "licenses.json"

DEFAULT_CONFIG = {
    "schedule_time": "00:00",
    "jitter_minutes": 5,
    "send_gap_min": 5,
    "send_gap_max": 8,
    "max_friends_per_run": 20,
    "friends": [],
    "messages": [
        "🔥 续火花",
        "晚安，明天见",
        "今天也要开心哦",
    ],
    # ====== AI 续火花文案 ======
    "ai_enabled": False,
    "ai_api_base": "https://api.openai.com/v1",
    "ai_api_key": "",
    "ai_model": "gpt-4o-mini",
    "ai_system_prompt": "你是一个友好的社交助手，请根据好友昵称生成一句简短自然续火花的中文问候，加上当前中国时间自动续火花，用于抖音私信续火花，24字以内，不要用引号，开放一点，头上加上抖音自动续火花",
    # ====== 模式配置 ======
    "mode": "authorized",         # 默认授权模式，需卡密开通
    "enabled": True,              # 独立模式：是否启用
    "authorized_until": None,     # 授权模式：到期时间 (ISO格式)
}

_lock = threading.Lock()


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


def _check_account(account: str) -> str:
    """账号名称校验 - 防止目录穿越"""
    account = str(account).strip()
    if not account:
        raise ValueError("账号名称不能为空")
    if account in {".", ".."}:
        raise ValueError("账号名称无效")
    if len(account) > 100:
        raise ValueError("账号名称不能超过100个字符")
    if "/" in account or "\\" in account:
        raise ValueError("账号名称不能包含 / 或 \\")
    if any(ord(ch) < 32 for ch in account):
        raise ValueError("账号名称不能包含控制字符")
    return account


def account_dir(account: str) -> Path:
    account = _check_account(account)
    path = ACCOUNTS_DIR / account
    path.mkdir(parents=True, exist_ok=True)
    return path


def config_path(account: str) -> Path:
    return account_dir(account) / "config.json"


def state_path(account: str) -> Path:
    return account_dir(account) / "state.json"


def runtime_path(account: str) -> Path:
    return account_dir(account) / "runtime.json"


def profile_path(account: str) -> Path:
    return account_dir(account) / "profile.json"


def logs_dir(account: str) -> Path:
    path = account_dir(account) / "logs"
    path.mkdir(parents=True, exist_ok=True)
    return path


def list_accounts() -> list[str]:
    if not ACCOUNTS_DIR.exists():
        return []
    result = []
    for path in ACCOUNTS_DIR.iterdir():
        if not path.is_dir():
            continue
        try:
            name = _check_account(path.name)
        except ValueError:
            continue
        result.append(name)

    def _sort_key(name: str):
        try:
            p = load_profile(name)
            pri = int(p.get("priority") or 1)
        except Exception:
            pri = 0
        # VIP：数字越大越优先（V8 优先于 V1）
        pri = max(1, min(pri if pri else 1, 8))
        return (-pri, name.casefold())

    return sorted(result, key=_sort_key)


def default_profile(account: str) -> dict:
    return {
        "account": account,
        "name": account,
        "created_at": None,
        "updated_at": None,
        "priority": 1,  # VIP 等级 1~8（V1~V8），数字越大越优先
    }


def load_profile(account: str) -> dict:
    account = _check_account(account)
    profile = default_profile(account)
    path = profile_path(account)
    if not path.exists():
        pass
    else:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                profile.update(data)
        except Exception:
            pass

    # 等级(VIP) 以独立授权文件为准，避免用户清理数据后等级丢失
    lic = _load_license_entry(account)
    if lic and lic.get("priority") is not None:
        try:
            profile["priority"] = max(1, min(int(lic["priority"]), 8))
        except (TypeError, ValueError):
            pass

    profile["account"] = account
    if not profile.get("name"):
        profile["name"] = account
    return profile


# ============================================================
# 独立授权文件（防删除）：授权到期时间 + 等级
# ============================================================

def _load_licenses() -> dict:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if not LICENSE_PATH.exists():
        return {}
    try:
        data = json.loads(LICENSE_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_licenses(data: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    _atomic_write_text(
        LICENSE_PATH,
        json.dumps(data, ensure_ascii=False, indent=2),
    )


def _load_license_entry(account: str) -> dict:
    try:
        account = _check_account(account)
    except ValueError:
        return {}
    return _load_licenses().get(account) or {}


def load_license(account: str, cfg: dict | None = None) -> dict:
    """读取账号授权信息。

    优先使用独立授权文件 licenses.json；若没有记录，
    则回退到 config.json（兼容旧数据）并顺手迁移过来。
    """
    account = _check_account(account)
    entry = _load_license_entry(account)

    if not entry:
        # 旧数据迁移：把 config.json 里的授权字段搬进独立文件
        cfg = cfg or load_config(account)
        entry = {
            "mode": cfg.get("mode", "authorized"),
            "enabled": bool(cfg.get("enabled", True)),
            "authorized_until": cfg.get("authorized_until"),
            "priority": None,
        }
        try:
            _save_license(account, entry)
        except Exception:
            pass

    return {
        "mode": entry.get("mode") or "authorized",
        "enabled": bool(entry.get("enabled", True)),
        "authorized_until": entry.get("authorized_until"),
        "priority": entry.get("priority"),
    }


def _save_license(account: str, patch: dict) -> dict:
    account = _check_account(account)
    with _lock:
        data = _load_licenses()
        cur = data.get(account) or {}
        cur.update({k: v for k, v in (patch or {}).items() if v is not None or k == "authorized_until"})
        cur["account"] = account
        cur["updated_at"] = datetime.now().isoformat(timespec="seconds")
        data[account] = cur
        _save_licenses(data)
    return cur


def save_license(account: str, patch: dict) -> dict:
    """写入授权信息（对外接口）。"""
    return _save_license(account, patch)


def drop_license(account: str) -> None:
    """彻底移除账号授权记录（仅管理员强制删除时调用）。"""
    try:
        account = _check_account(account)
    except ValueError:
        return
    with _lock:
        data = _load_licenses()
        if account in data:
            data.pop(account, None)
            _save_licenses(data)


def save_profile(account: str, profile: dict | None = None) -> dict:
    account = _check_account(account)
    merged = default_profile(account)
    current = load_profile(account)
    merged.update(current)
    if profile:
        merged.update(profile)
    merged["account"] = account

    name = str(merged.get("name", account)).strip()
    if not name:
        name = account
    if len(name) > 100:
        raise ValueError("账号名称不能超过100个字符")
    if "/" in name or "\\" in name:
        raise ValueError("账号名称不能包含 / 或 \\")
    if any(ord(ch) < 32 for ch in name):
        raise ValueError("账号名称不能包含控制字符")
    merged["name"] = name

    path = profile_path(account)
    with _lock:
        path.write_text(
            json.dumps(merged, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    # 等级(VIP) 同步写入独立授权文件，用户清理数据后不会丢失
    try:
        pri = merged.get("priority")
        if pri is not None:
            p = max(1, min(int(pri), 8))
            merged["priority"] = p
            cur = _load_license_entry(account)
            if cur.get("priority") != p:
                _save_license(account, {"priority": p})
    except Exception:
        pass

    return merged


def create_account(account: str) -> dict:
    account = _check_account(account)
    if account in list_accounts():
        raise ValueError(f"账号已存在: {account}")
    account_dir(account)
    save_profile(account, default_profile(account))
    save_config(account, DEFAULT_CONFIG)
    return load_profile(account)


def rename_account(old_account: str, new_account: str) -> dict:
    old_account = _check_account(old_account)
    new_account = _check_account(new_account)
    if old_account == new_account:
        return load_profile(old_account)
    old_path = ACCOUNTS_DIR / old_account
    new_path = ACCOUNTS_DIR / new_account
    if not old_path.exists():
        raise ValueError(f"账号不存在: {old_account}")
    if new_path.exists():
        raise ValueError(f"账号已存在: {new_account}")
    with _lock:
        old_path.rename(new_path)
        # 同步迁移授权记录。否则旧名字的授权变成孤儿，
        # 以后任何新建同名账号都会「免费继承」旧的到期时间与等级。
        try:
            lic = _load_licenses()
            if old_account in lic:
                entry = lic.pop(old_account)
                if isinstance(entry, dict):
                    entry["account"] = new_account
                lic[new_account] = entry
                _save_licenses(lic)
        except Exception:
            logging.exception("迁移授权记录失败：%s -> %s", old_account, new_account)
    profile = load_profile(new_account)
    profile["account"] = new_account
    profile["name"] = new_account
    save_profile(new_account, profile)
    return profile


def delete_account(account: str) -> None:
    account = _check_account(account)
    path = ACCOUNTS_DIR / account
    if not path.exists():
        raise ValueError(f"账号不存在: {account}")
    if not path.is_dir():
        raise ValueError(f"账号路径无效: {account}")
    with _lock:
        shutil.rmtree(path)


def load_config(account: str) -> dict:
    cfg = dict(DEFAULT_CONFIG)
    path = config_path(account)
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                cfg.update(data)
        except Exception:
            pass
    return cfg


def save_config(account: str, cfg: dict | None = None) -> dict:
    merged = dict(DEFAULT_CONFIG)
    if cfg:
        merged.update(cfg)

    # 清理好友列表
    merged["friends"] = [
        str(x).strip() for x in merged.get("friends", []) if str(x).strip()
    ]

    # 清理消息列表
    merged["messages"] = [
        str(x) for x in merged.get("messages", []) if str(x).strip()
    ]
    if not merged["messages"]:
        merged["messages"] = ["🔥"]

    # 验证时间格式
    schedule = str(merged.get("schedule_time", "21:00"))
    try:
        hh, mm = schedule.split(":")
        hh = int(hh)
        mm = int(mm)
        if not (0 <= hh <= 23 and 0 <= mm <= 59):
            raise ValueError
        merged["schedule_time"] = f"{hh:02d}:{mm:02d}"
    except Exception:
        raise ValueError("schedule_time 必须是 HH:MM 格式")

    # 验证数值字段
    for key in ("jitter_minutes", "send_gap_min", "send_gap_max", "max_friends_per_run"):
        try:
            merged[key] = max(0, int(merged.get(key, DEFAULT_CONFIG[key])))
        except (TypeError, ValueError):
            raise ValueError(f"{key} 必须是整数")

    if merged["send_gap_max"] < merged["send_gap_min"]:
        merged["send_gap_max"] = merged["send_gap_min"]

    # ====== 验证模式配置 ======
    mode = merged.get("mode", "independent")
    if mode not in ["independent", "authorized"]:
        merged["mode"] = "independent"

    merged["enabled"] = bool(merged.get("enabled", True))

    # AI 文案配置
    merged["ai_enabled"] = bool(merged.get("ai_enabled", False))
    merged["ai_api_base"] = str(merged.get("ai_api_base") or "https://api.openai.com/v1").strip().rstrip("/")
    merged["ai_api_key"] = str(merged.get("ai_api_key") or "").strip()
    merged["ai_model"] = str(merged.get("ai_model") or "gpt-4o-mini").strip()
    merged["ai_system_prompt"] = str(merged.get("ai_system_prompt") or "").strip()

    authorized_until = merged.get("authorized_until")
    if authorized_until is not None:
        try:
            datetime.fromisoformat(str(authorized_until))
        except Exception:
            merged["authorized_until"] = None

    path = config_path(account)
    with _lock:
        path.write_text(
            json.dumps(merged, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    return merged


def get_mode_status(account: str) -> dict:
    """
    获取账号的模式状态
    返回: {
        "mode": "independent" | "authorized",
        "enabled": bool,
        "authorized_until": str | None,
        "is_active": bool,
        "status_text": str,
        "remaining_days": int | None,
    }
    """
    cfg = load_config(account)
    lic = load_license(account, cfg)
    mode = lic.get("mode") or "authorized"
    enabled = lic.get("enabled", True)
    authorized_until = lic.get("authorized_until")

    result = {
        "mode": mode,
        "enabled": enabled,
        "authorized_until": authorized_until,
        "is_active": False,
        "status_text": "",
        "remaining_days": None,
    }

    if mode == "independent":
        result["is_active"] = enabled
        result["status_text"] = "已开启" if enabled else "已关闭"

    elif mode == "authorized":
        if authorized_until:
            try:
                until = datetime.fromisoformat(str(authorized_until))
                # 与 scheduler 保持一致：折算到上海时区再比较，
                # 避免服务器时区不同导致到期判断错位 / aware-naive 比较抛错
                from zoneinfo import ZoneInfo
                _tz = ZoneInfo("Asia/Shanghai")
                if until.tzinfo is None:
                    until = until.replace(tzinfo=_tz)
                else:
                    until = until.astimezone(_tz)
                now = datetime.now(_tz)
                if now > until:
                    result["is_active"] = False
                    result["status_text"] = "已过期"
                    if enabled:
                        # 只有在真正过期时才关闭，且写回独立授权文件
                        save_license(account, {"enabled": False})
                        result["enabled"] = False
                else:
                    result["is_active"] = enabled
                    # 格式化显示，避免把原始 ISO 串（含 T 和微秒）漏到界面
                    until_text = (
                        f"{until.year}/{until.month}/{until.day} "
                        f"{until.hour:02d}:{until.minute:02d}:{until.second:02d}"
                    )
                    result["status_text"] = f"有效期至 {until_text}"
                    days = (until - now).days
                    result["remaining_days"] = days
                    if days <= 1:
                        result["status_text"] += " (即将到期)"
            except Exception:
                result["is_active"] = False
                result["status_text"] = "时间格式错误"
        else:
            result["is_active"] = False
            result["status_text"] = "未设置到期时间"

    return result


# ============================================================
# 快捷授权工具
# ============================================================

def add_authorized_days(account: str, days: int) -> dict:
    """
    为账号添加授权天数：在现有未过期到期时间基础上叠加；
    若已过期或未设置，则从当前时间起算。

    授权信息写入独立文件 licenses.json，用户清理抖音数据不会丢失。
    """
    account = _check_account(account)
    lic = load_license(account)

    now = datetime.now()
    base = now
    raw = lic.get("authorized_until")
    if raw:
        try:
            existing = datetime.fromisoformat(str(raw))
            if existing > now:
                base = existing
        except Exception:
            pass
    until = base + timedelta(days=int(days))

    _save_license(account, {
        "mode": "authorized",
        "enabled": True,
        "authorized_until": until.isoformat(),
    })

    # 同步一份到 config.json，保持旧逻辑/前端兼容
    try:
        cfg = load_config(account)
        cfg["mode"] = "authorized"
        cfg["enabled"] = True
        cfg["authorized_until"] = until.isoformat()
        save_config(account, cfg)
    except Exception:
        pass

    return {
        "account": account,
        "mode": "authorized",
        "enabled": True,
        "authorized_until": until.isoformat(),
        "days": int(days),
        "from": base.isoformat(),
    }
