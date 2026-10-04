"""多用户认证：管理员 + 注册密钥 + 普通用户绑定抖音账号。"""

from __future__ import annotations

import os
import hashlib
import re
import json
import secrets
import threading
from datetime import datetime, timedelta
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
USERS_PATH = DATA_DIR / "users.json"
KEYS_PATH = DATA_DIR / "invite_keys.json"
SESSIONS_PATH = DATA_DIR / "sessions.json"
RESET_CODES_PATH = DATA_DIR / "reset_codes.json"
# 兼容旧版单管理员文件
OLD_ADMIN_PATH = DATA_DIR / "admin.json"

SESSION_DAYS = 30
# 密码重置验证码：10 分钟有效，同邮箱 60 秒内只发一次，每天最多 10 条，验证错 5 次作废
RESET_CODE_TTL_SECONDS = 600
RESET_CODE_SEND_COOLDOWN = 60
RESET_CODE_MAX_PER_DAY = 10
RESET_CODE_MAX_ATTEMPTS = 5
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


def _ensure_data_dir() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _valid_email(email: str) -> str:
    email = (email or "").strip()
    if not email:
        return ""
    if len(email) > 120 or "@" not in email or "." not in email.split("@")[-1]:
        raise ValueError("邮箱格式不正确")
    return email


def _hash_password(password: str, salt: str) -> str:
    raw = f"{salt}:{password}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _load_json(path: Path) -> dict:
    _ensure_data_dir()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        # 解析失败：先备份坏文件，避免后续写入把可恢复的数据覆盖掉
        _backup_corrupt(path)
        return {}


def _save_json(path: Path, data: dict) -> None:
    _ensure_data_dir()
    _atomic_write_text(
        path,
        json.dumps(data, ensure_ascii=False, indent=2),
    )


# ------------------------------------------------------------
# 用户
# ------------------------------------------------------------

def _load_users() -> dict:
    return _load_json(USERS_PATH)


def _save_users(data: dict) -> None:
    _save_json(USERS_PATH, data)


def ensure_default_admin() -> None:
    """首次启动创建默认管理员 admin / admin；并迁移旧 admin.json。"""
    with _lock:
        users = _load_users()
        if any(u.get("role") == "admin" for u in users.values()):
            return

        # 迁移旧版单管理员
        if OLD_ADMIN_PATH.exists():
            try:
                old = json.loads(OLD_ADMIN_PATH.read_text(encoding="utf-8"))
                if old.get("username") and old.get("password_hash"):
                    uname = str(old["username"])
                    users[uname] = {
                        "username": uname,
                        "role": "admin",
                        "salt": old.get("salt") or secrets.token_hex(16),
                        "password_hash": old["password_hash"],
                        "password_plain": "",  # 旧数据无明文
                        "bound_account": None,
                        "created_at": old.get("created_at") or _now(),
                        "updated_at": _now(),
                    }
                    _save_users(users)
                    return
            except Exception:
                pass

        salt = secrets.token_hex(16)
        users["admin"] = {
            "username": "admin",
            "role": "admin",
            "salt": salt,
            "password_hash": _hash_password("admin", salt),
            "password_plain": "admin",
            "bound_account": None,
            "created_at": _now(),
            "updated_at": _now(),
        }
        _save_users(users)


def get_user(username: str) -> dict | None:
    users = _load_users()
    u = users.get((username or "").strip())
    return dict(u) if u else None


def is_admin(username: str) -> bool:
    u = get_user(username)
    return bool(u and u.get("role") == "admin")


def get_admin_username() -> str:
    users = _load_users()
    for u in users.values():
        if u.get("role") == "admin":
            return str(u.get("username") or "admin")
    return "admin"


def verify_login(username: str, password: str) -> bool:
    u = get_user(username)
    if not u:
        return False
    salt = u.get("salt") or ""
    expected = u.get("password_hash") or ""
    if not salt or not expected:
        return False
    return secrets.compare_digest(_hash_password(password, salt), expected)


def _make_user(
    username: str,
    password: str,
    role: str = "user",
    bound_account: str | None = None,
    email: str = "",
    qq: str = "",
    phone: str = "",
) -> dict:
    username = (username or "").strip()
    if not username:
        raise ValueError("用户名不能为空")
    if len(username) < 2 or len(username) > 32:
        raise ValueError("用户名长度需在 2～32 之间")
    if any(c in username for c in "/\\ \t\n"):
        raise ValueError("用户名不能包含空格或斜杠")
    password = password or ""
    if len(password) < 4:
        raise ValueError("密码至少 4 位")
    if len(password) > 64:
        raise ValueError("密码不能超过 64 位")
    salt = secrets.token_hex(16)
    return {
        "username": username,
        "role": role if role in ("admin", "user") else "user",
        "salt": salt,
        "password_hash": _hash_password(password, salt),
        "password_plain": password,  # 管理员可查看（按需求）
        "bound_account": bound_account,
        "email": _valid_email(email),
        "qq": str(qq or "").strip()[:32],
        "phone": str(phone or "").strip()[:32],
        "created_at": _now(),
        "updated_at": _now(),
    }


def register_with_key(
    username: str,
    password: str,
    invite_key: str,
    email: str = "",
    qq: str = "",
    phone: str = "",
) -> dict:
    """用户通过注册密钥自行注册。"""
    key = (invite_key or "").strip()
    if not key:
        raise ValueError("请填写注册码")
    username = (username or "").strip()
    email = (email or "").strip()
    qq = (qq or "").strip()
    phone = (phone or "").strip()
    if not email:
        raise ValueError("邮箱必填，用于接收任务日志与通知")
    if "@" not in email or "." not in email.split("@")[-1]:
        raise ValueError("邮箱格式不正确")
    if not qq and not phone:
        raise ValueError("QQ 与手机号请至少填写一项")

    with _lock:
        keys = _load_json(KEYS_PATH)
        info = keys.get(key)
        if not info:
            raise ValueError("注册码无效")
        if info.get("used"):
            raise ValueError("注册码已被使用")

        users = _load_users()
        if username in users:
            raise ValueError("用户名已存在")
        # 邮箱唯一
        for u, ud in users.items():
            if (ud.get("email") or "").lower() == email.lower():
                raise ValueError("该邮箱已被注册")

        user = _make_user(
            username, password, role="user", email=email, qq=qq, phone=phone
        )
        users[username] = user
        keys[key] = {
            **info,
            "used": True,
            "used_by": username,
            "used_at": _now(),
        }
        _save_users(users)
        _save_json(KEYS_PATH, keys)
        result = {
            "username": username,
            "role": "user",
            "email": user.get("email") or "",
            "qq": user.get("qq") or "",
            "phone": user.get("phone") or "",
            "default_auth_days": 0,
        }

    # 注册码默认授权天数：由站点设置控制（在锁外读配置，避免循环依赖）
    # 说明：注册时由 app.py 直接发放到账号，这里只负责回传天数，不写 pending，
    #       避免用户再创建账号时重复赠送。
    try:
        from . import site_settings
        days = int((site_settings.load().get("invite_settings") or {}).get("default_auth_days") or 0)
        result["default_auth_days"] = max(0, days)
    except Exception:
        pass
    return result


def _revoke_user_sessions(username: str) -> None:
    """吊销某用户的全部会话（改密/重置密码后调用）。"""
    try:
        sessions = _load_json(SESSIONS_PATH)
        cleaned = {
            t: info
            for t, info in sessions.items()
            if info.get("username") != username
        }
        if len(cleaned) != len(sessions):
            _save_json(SESSIONS_PATH, cleaned)
    except Exception:
        pass


def _hash_reset_code(email: str, code: str) -> str:
    """验证码只存哈希，reset_codes.json 泄露也不能直接重置密码。"""
    return hashlib.sha256(
        f"reset:{(email or '').lower()}:{code}".encode("utf-8")
    ).hexdigest()


def _cleanup_reset_codes(data: dict) -> dict:
    """清掉已过期的验证码记录（惰性 GC）。"""
    now = datetime.now()
    cleaned = {}
    for email_key, entry in data.items():
        try:
            exp = datetime.fromisoformat(str(entry.get("expires") or ""))
            if exp > now:
                cleaned[email_key] = entry
        except Exception:
            pass
    return cleaned


def create_reset_code(email: str) -> None:
    """生成密码重置验证码并发送到用户邮箱（需管理员已配置 SMTP）。

    此前忘记密码仅凭「邮箱 + 新密码」即可重置，等于知道邮箱就能接管
    任意账号；现在必须持有该邮箱的验证码。
    """
    email = (email or "").strip()
    if not email:
        raise ValueError("请填写邮箱")

    from . import email_util  # 延迟导入，避免循环依赖

    cfg = email_util.load_email_config()
    if not cfg.get("enabled") or not (cfg.get("smtp_host") or "").strip():
        raise ValueError(
            "管理员未配置邮件服务，无法发送验证码，请联系管理员重置密码"
        )

    # 邮箱必须对应用户（与旧版提示一致）
    users = _load_users()
    matched = [
        u for u, ud in users.items()
        if (ud.get("email") or "").lower() == email.lower()
    ]
    if not matched:
        raise ValueError("未找到该邮箱对应的账号")

    code = f"{secrets.randbelow(1000000):06d}"
    now = datetime.now()
    today = now.strftime("%Y-%m-%d")

    with _lock:
        data = _cleanup_reset_codes(_load_json(RESET_CODES_PATH))
        entry = data.get(email.lower()) or {}

        last_raw = str(entry.get("last_sent") or "")
        if last_raw:
            try:
                last = datetime.fromisoformat(last_raw)
            except Exception:
                last = None
            if last and (now - last).total_seconds() < RESET_CODE_SEND_COOLDOWN:
                raise ValueError("验证码发送过于频繁，请 1 分钟后再试")

        sent_count = int(entry.get("sent_count") or 0) if entry.get("sent_date") == today else 0
        if sent_count >= RESET_CODE_MAX_PER_DAY:
            raise ValueError("今日验证码发送次数已达上限，请明天再试或联系管理员")

        data[email.lower()] = {
            "code_hash": _hash_reset_code(email, code),
            "expires": (
                now + timedelta(seconds=RESET_CODE_TTL_SECONDS)
            ).isoformat(timespec="seconds"),
            "attempts": 0,
            "last_sent": now.isoformat(timespec="seconds"),
            "sent_date": today,
            "sent_count": sent_count + 1,
        }
        _save_json(RESET_CODES_PATH, data)

    # 邮件在锁外发送（SMTP 较慢）；发送失败则作废验证码，避免留下半可用状态
    plain = "\n".join([
        "你好！",
        "",
        "你正在重置「云逸续火花助手」的登录密码。",
        f"验证码：{code}",
        f"有效期 {RESET_CODE_TTL_SECONDS // 60} 分钟；若非本人操作，请忽略本邮件。",
        "",
        f"发送时间：{now.strftime('%Y-%m-%d %H:%M:%S')}",
    ])
    try:
        email_util.send_email(
            "[云逸续火花助手] 密码重置验证码",
            plain,
            to_addrs=[email],
            cfg=cfg,
        )
    except Exception:
        with _lock:
            data = _load_json(RESET_CODES_PATH)
            if data.pop(email.lower(), None) is not None:
                _save_json(RESET_CODES_PATH, data)
        raise


def reset_password_by_email(
    email: str,
    new_password: str,
    username: str | None = None,
    code: str = "",
) -> None:
    """通过邮箱验证码重置密码（登录页忘记密码）。"""
    email = (email or "").strip()
    if not email:
        raise ValueError("请填写邮箱")
    new_password = new_password or ""
    if len(new_password) < 6 or len(new_password) > 18:
        raise ValueError("新密码长度需为 6～18 位")
    if not re.fullmatch(r"[A-Za-z0-9]+", new_password):
        raise ValueError("新密码只能包含英文和数字")
    code = (code or "").strip()
    if not code:
        raise ValueError("请先点击「获取验证码」，再填写邮件中的验证码")

    with _lock:
        # ===== 验证码校验（一次性，错太多次作废） =====
        data = _cleanup_reset_codes(_load_json(RESET_CODES_PATH))
        entry = data.get(email.lower())
        if not entry:
            raise ValueError("验证码无效或已过期，请重新获取")
        try:
            if datetime.fromisoformat(str(entry.get("expires") or "")) <= datetime.now():
                data.pop(email.lower(), None)
                _save_json(RESET_CODES_PATH, data)
                raise ValueError("验证码已过期，请重新获取")
        except ValueError:
            raise
        except Exception:
            data.pop(email.lower(), None)
            _save_json(RESET_CODES_PATH, data)
            raise ValueError("验证码无效，请重新获取")
        if int(entry.get("attempts") or 0) >= RESET_CODE_MAX_ATTEMPTS:
            data.pop(email.lower(), None)
            _save_json(RESET_CODES_PATH, data)
            raise ValueError("验证码错误次数过多，请重新获取")
        if not secrets.compare_digest(
            str(entry.get("code_hash") or ""), _hash_reset_code(email, code)
        ):
            entry["attempts"] = int(entry.get("attempts") or 0) + 1
            data[email.lower()] = entry
            _save_json(RESET_CODES_PATH, data)
            raise ValueError("验证码错误")
        # 验证通过即作废（一次性）
        data.pop(email.lower(), None)
        _save_json(RESET_CODES_PATH, data)

        # ===== 匹配用户 =====
        users = _load_users()
        matched = []
        for uname, ud in users.items():
            if (ud.get("email") or "").lower() == email.lower():
                matched.append(uname)
        if not matched:
            raise ValueError("未找到该邮箱对应的账号")
        if username:
            username = username.strip()
            if username not in matched:
                raise ValueError("账号与邮箱不匹配")
            target = username
        else:
            if len(matched) > 1:
                raise ValueError("该邮箱绑定多个账号，请同时填写账号")
            target = matched[0]
        salt = secrets.token_hex(16)
        users[target]["salt"] = salt
        users[target]["password_hash"] = _hash_password(new_password, salt)
        # 同步明文字段，避免管理后台显示过期旧密码
        users[target]["password_plain"] = new_password
        users[target]["updated_at"] = _now()
        _save_users(users)
        # 重置密码后吊销该用户全部旧会话，
        # 否则盗号者手里的 token 还能继续用 30 天。
        _revoke_user_sessions(target)


def admin_create_user(
    username: str,
    password: str,
    role: str = "user",
    email: str = "",
) -> dict:
    """管理员直接创建用户（无需密钥）。"""
    username = (username or "").strip()
    with _lock:
        users = _load_users()
        if username in users:
            raise ValueError("用户名已存在")
        if role == "admin" and any(u.get("role") == "admin" for u in users.values()):
            # 允许多管理员，不限制
            pass
        user = _make_user(username, password, role=role or "user", email=email)
        users[username] = user
        _save_users(users)
        return {
            "username": username,
            "role": user["role"],
            "email": user.get("email") or "",
        }


def change_password(username: str, old_password: str, new_password: str) -> None:
    new_password = (new_password or "").strip()
    if len(new_password) < 4:
        raise ValueError("新密码至少 4 位")
    if len(new_password) > 64:
        raise ValueError("新密码不能超过 64 位")

    with _lock:
        users = _load_users()
        u = users.get(username)
        if not u:
            raise ValueError("用户不存在")
        salt = u.get("salt") or ""
        expected = u.get("password_hash") or ""
        if not secrets.compare_digest(_hash_password(old_password, salt), expected):
            raise ValueError("原密码错误")

        new_salt = secrets.token_hex(16)
        u["salt"] = new_salt
        u["password_hash"] = _hash_password(new_password, new_salt)
        u["password_plain"] = new_password
        u["updated_at"] = _now()
        users[username] = u
        _save_users(users)

        # 清除该用户所有会话
        sessions = _load_json(SESSIONS_PATH)
        cleaned = {
            t: info
            for t, info in sessions.items()
            if info.get("username") != username
        }
        _save_json(SESSIONS_PATH, cleaned)


def set_pending_auth_days(username: str, days: int) -> int:
    """记录用户注册码赠送的待发放授权天数（等他创建抖音账号后生效）。"""
    try:
        days = int(days or 0)
    except (TypeError, ValueError):
        days = 0
    days = max(0, min(days, 3650))
    with _lock:
        users = _load_users()
        u = users.get((username or "").strip())
        if not u:
            return 0
        u["pending_auth_days"] = days
        u["updated_at"] = _now()
        users[u.get("username") or username] = u
        _save_users(users)
    return days


def take_pending_auth_days(username: str) -> int:
    """取出并清空待发放天数（只发一次，避免重复赠送）。"""
    with _lock:
        users = _load_users()
        u = users.get((username or "").strip())
        if not u:
            return 0
        try:
            days = int(u.get("pending_auth_days") or 0)
        except (TypeError, ValueError):
            days = 0
        if days <= 0:
            return 0
        u["pending_auth_days"] = 0
        u["updated_at"] = _now()
        users[u.get("username") or username] = u
        _save_users(users)
    return max(0, min(days, 3650))


def change_username(username: str, new_username: str) -> dict:
    """用户自己修改登录用户名。

    会一并迁移用户记录、会话以及抖音账号绑定关系，
    保证改名后仍然保持登录且绑定不丢失。
    """
    username = (username or "").strip()
    new_username = (new_username or "").strip()

    if not new_username:
        raise ValueError("新用户名不能为空")
    if len(new_username) < 2 or len(new_username) > 32:
        raise ValueError("用户名长度需在 2～32 之间")
    if any(c in new_username for c in "/\\ \t\n"):
        raise ValueError("用户名不能包含空格或斜杠")
    if not re.fullmatch(r"[A-Za-z0-9_\u4e00-\u9fa5]+", new_username):
        raise ValueError("用户名只能包含中英文、数字与下划线")
    if new_username == username:
        raise ValueError("新用户名与当前用户名相同")

    with _lock:
        users = _load_users()
        u = users.get(username)
        if not u:
            raise ValueError("用户不存在")
        if new_username in users:
            raise ValueError("该用户名已被占用")

        # 迁移用户记录
        u["username"] = new_username
        u["updated_at"] = _now()
        users.pop(username, None)
        users[new_username] = u
        _save_users(users)

        # 迁移该用户的会话，保持登录状态
        sessions = _load_json(SESSIONS_PATH)
        changed = False
        for _t, info in sessions.items():
            if info.get("username") == username:
                info["username"] = new_username
                changed = True
        if changed:
            _save_json(SESSIONS_PATH, sessions)

        # 迁移注册密钥里的使用记录
        try:
            keys = _load_json(KEYS_PATH)
            k_changed = False
            for _k, info in keys.items():
                if info.get("used_by") == username:
                    info["used_by"] = new_username
                    k_changed = True
            if k_changed:
                _save_json(KEYS_PATH, keys)
        except Exception:
            pass

    return {
        "username": new_username,
        "old_username": username,
    }


def admin_set_password(username: str, new_password: str) -> None:
    """管理员重置某用户密码。"""
    new_password = (new_password or "").strip()
    if len(new_password) < 4:
        raise ValueError("新密码至少 4 位")
    with _lock:
        users = _load_users()
        u = users.get(username)
        if not u:
            raise ValueError("用户不存在")
        new_salt = secrets.token_hex(16)
        u["salt"] = new_salt
        u["password_hash"] = _hash_password(new_password, new_salt)
        u["password_plain"] = new_password
        u["updated_at"] = _now()
        users[username] = u
        _save_users(users)
        # 与自助改密/忘记密码保持一致：管理员重置后必须吊销旧会话，
        # 否则盗号场景下管理员「救场」后旧 token 仍有效 30 天。
        _revoke_user_sessions(username)


def list_users_for_admin() -> list[dict]:
    """管理员查看所有用户（含明文密码与完整资料）。"""
    users = _load_users()
    result = []
    for u in users.values():
        accounts = _normalize_bound(u)
        result.append({
            "username": u.get("username"),
            "role": u.get("role") or "user",
            "password": u.get("password_plain") or "（旧数据无明文）",
            "email": u.get("email") or "",
            "qq": u.get("qq") or "",
            "phone": u.get("phone") or "",
            "bound_account": accounts[0] if accounts else None,
            "bound_accounts": accounts,
            "pending_auth_days": u.get("pending_auth_days") or 0,
            "created_at": u.get("created_at"),
            "updated_at": u.get("updated_at"),
            "last_login_at": u.get("last_login_at"),
            "last_login_ip": u.get("last_login_ip") or "",
        })
    result.sort(key=lambda x: (0 if x["role"] == "admin" else 1, x["username"] or ""))
    return result


def delete_user(username: str) -> None:
    username = (username or "").strip()
    with _lock:
        users = _load_users()
        u = users.get(username)
        if not u:
            raise ValueError("用户不存在")
        if u.get("role") == "admin":
            admins = [x for x in users.values() if x.get("role") == "admin"]
            if len(admins) <= 1:
                raise ValueError("不能删除唯一的管理员")
        users.pop(username, None)
        _save_users(users)
        sessions = _load_json(SESSIONS_PATH)
        cleaned = {
            t: info
            for t, info in sessions.items()
            if info.get("username") != username
        }
        _save_json(SESSIONS_PATH, cleaned)


def _normalize_bound(u: dict) -> list[str]:
    """统一返回绑定账号列表（兼容旧字段 bound_account）。"""
    accounts: list[str] = []
    raw_list = u.get("bound_accounts")
    if isinstance(raw_list, list):
        for a in raw_list:
            s = str(a or "").strip()
            if s and s not in accounts:
                accounts.append(s)
    single = u.get("bound_account")
    if single:
        s = str(single).strip()
        if s and s not in accounts:
            accounts.insert(0, s)
    return accounts


def bind_account(username: str, account: str | None) -> None:
    """绑定或解绑单个抖音账号（写入列表；兼容旧逻辑）。"""
    with _lock:
        users = _load_users()
        u = users.get(username)
        if not u:
            raise ValueError("用户不存在")
        if account:
            u["bound_account"] = account
            u["bound_accounts"] = [account]
        else:
            u["bound_account"] = None
            u["bound_accounts"] = []
        u["updated_at"] = _now()
        users[username] = u
        _save_users(users)


def set_bound_accounts(username: str, accounts: list[str]) -> list[str]:
    """管理员设置用户绑定的抖音账号列表。"""
    cleaned: list[str] = []
    for a in accounts or []:
        s = str(a or "").strip()
        if s and s not in cleaned:
            cleaned.append(s)
    with _lock:
        users = _load_users()
        u = users.get(username)
        if not u:
            raise ValueError("用户不存在")
        u["bound_accounts"] = cleaned
        u["bound_account"] = cleaned[0] if cleaned else None
        u["updated_at"] = _now()
        users[username] = u
        _save_users(users)
    return cleaned


def get_bound_account(username: str) -> str | None:
    accounts = accounts_owned_by(username)
    return accounts[0] if accounts else None


def accounts_owned_by(username: str) -> list[str]:
    """普通用户可见的账号列表。"""
    u = get_user(username)
    if not u:
        return []
    return _normalize_bound(u)


# ------------------------------------------------------------
# 注册密钥
# ------------------------------------------------------------

def create_invite_key(created_by: str, note: str = "") -> dict:
    key = secrets.token_urlsafe(12)
    with _lock:
        keys = _load_json(KEYS_PATH)
        info = {
            "key": key,
            "note": (note or "").strip()[:100],
            "used": False,
            "used_by": None,
            "used_at": None,
            "created_by": created_by,
            "created_at": _now(),
        }
        keys[key] = info
        _save_json(KEYS_PATH, keys)
        return dict(info)


def list_invite_keys() -> list[dict]:
    keys = _load_json(KEYS_PATH)
    result = list(keys.values())
    result.sort(key=lambda x: x.get("created_at") or "", reverse=True)
    return result


def delete_invite_key(key: str) -> None:
    key = (key or "").strip()
    with _lock:
        keys = _load_json(KEYS_PATH)
        if key not in keys:
            raise ValueError("密钥不存在")
        keys.pop(key, None)
        _save_json(KEYS_PATH, keys)


# ------------------------------------------------------------
# 会话
# ------------------------------------------------------------

def create_session(username: str) -> str:
    token = secrets.token_urlsafe(32)
    expires = (datetime.now() + timedelta(days=SESSION_DAYS)).isoformat(timespec="seconds")
    with _lock:
        sessions = _load_json(SESSIONS_PATH)
        now = datetime.now()
        cleaned = {}
        for t, info in sessions.items():
            try:
                exp = datetime.fromisoformat(info.get("expires", ""))
                if exp > now:
                    cleaned[t] = info
            except Exception:
                pass
        cleaned[token] = {
            "username": username,
            "created_at": _now(),
            "expires": expires,
        }
        _save_json(SESSIONS_PATH, cleaned)
    return token


def validate_session(token: str) -> str | None:
    if not token:
        return None
    with _lock:
        sessions = _load_json(SESSIONS_PATH)
        info = sessions.get(token)
        if not info:
            return None
        try:
            exp = datetime.fromisoformat(info.get("expires", ""))
            if exp <= datetime.now():
                sessions.pop(token, None)
                _save_json(SESSIONS_PATH, sessions)
                return None
        except Exception:
            return None
        return info.get("username")


def revoke_session(token: str) -> None:
    if not token:
        return
    with _lock:
        sessions = _load_json(SESSIONS_PATH)
        if token in sessions:
            sessions.pop(token, None)
            _save_json(SESSIONS_PATH, sessions)


def revoke_all_sessions() -> None:
    with _lock:
        _save_json(SESSIONS_PATH, {})


def update_user_email(username: str, email: str) -> str:
    """用户自己修改收件邮箱。"""
    email = _valid_email(email)
    with _lock:
        users = _load_users()
        u = users.get(username)
        if not u:
            raise ValueError("用户不存在")
        u["email"] = email
        u["updated_at"] = _now()
        users[username] = u
        _save_users(users)
    return email


def record_login(username: str, ip: str = "") -> None:
    """记录最近一次登录的时间与 IP（个人中心展示用）。"""
    with _lock:
        users = _load_users()
        u = users.get((username or "").strip())
        if not u:
            return
        u["last_login_at"] = _now()
        u["last_login_ip"] = str(ip or "").strip()[:64]
        u["updated_at"] = _now()
        users[u.get("username") or username] = u
        _save_users(users)


def find_users_for_account(account: str) -> list[dict]:
    """找出绑定了某抖音账号的用户（用于发日志邮件）。"""
    account = (account or "").strip()
    result = []
    for u in _load_users().values():
        accounts = _normalize_bound(u)
        if account in accounts:
            result.append({
                "username": u.get("username"),
                "email": (u.get("email") or "").strip(),
                "role": u.get("role") or "user",
            })
    return result


def create_invite_keys_batch(created_by: str, count: int = 1, note: str = "") -> list:
    count = max(1, min(int(count or 1), 100))
    return [create_invite_key(created_by, note=note) for _ in range(count)]
