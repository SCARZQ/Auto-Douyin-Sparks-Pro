"""运行状态与日志 - 添加模式状态"""

from __future__ import annotations

import os
import json
import re
import logging
import threading
from collections import deque
from datetime import datetime
from typing import Any

from .config import runtime_path, logs_dir, get_mode_status, load_config


# 用 RLock：update_runtime 需要把「读-改-写」整体放进锁内，
# 而内部的 _save 也会取同一把锁，普通 Lock 会死锁。
_lock = threading.RLock()
_rings: dict[str, deque[str]] = {}


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


def _default() -> dict:
    return {
        "session_status": "unknown",
        "running": False,
        "mode": "independent",
        "enabled": True,
        "authorized_until": None,
        "mode_active": True,
        "mode_status_text": "已开启",
        "last_run": None,
        "last_run_at": None,      # 上一次运行时间（供后台列表显示）
        "next_run": None,         # 下一次计划运行时间（调度器同步写入）
        "history_count": 0,       # 历史运行总次数
        "history": [],
        "contacts": [],
        "contacts_at": None,
        "contacts_error": None,
        "last_error": None,
        "rate_limited": False,
        "updated_at": None,
        "_progress": None,
        "_stop_progress": None,
        "_stopped_at": None,
        # ====== 用户信息 ======
        "douyin_name": "",      # 抖音昵称
        "douyin_id": "",        # 抖音号
        "sec_uid": "",          # 用户安全ID
        "douyin_avatar": "",    # 头像 URL
        "_force_stop": False,   # 强制停止标志
    }


def drop_ring(account: str) -> None:
    """删除账号后释放其内存中的环形日志缓冲。"""
    _rings.pop(account, None)


def _ring(account: str) -> deque[str]:
    if account not in _rings:
        # 300 行足够前端展示与排障，内存占用减半
        _rings[account] = deque(maxlen=300)
    return _rings[account]


def load_runtime(account: str) -> dict:
    rt = _default()
    path = runtime_path(account)
    if not path.exists():
        return rt
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            rt.update(data)
    except Exception:
        pass

    # ========================================================
    # 防止 runtime 里的旧「授权」信息与 config.json 打架
    # （config.json 才是权威来源：详见 get_mode_status）
    # ========================================================
    try:
        cfg = load_config(account)
        cfg_mode = cfg.get("mode")

        # runtime 里的授权字段一律以 config.json 为准，避免显示过期/矛盾的旧值
        rt["mode"] = cfg_mode or rt.get("mode")
        rt["enabled"] = bool(cfg.get("enabled", rt.get("enabled", True)))
        rt["authorized_until"] = (
            None if cfg_mode == "independent" else cfg.get("authorized_until")
        )
    except Exception:
        pass

    return rt


def _save(account: str, rt: dict) -> None:
    with _lock:
        path = runtime_path(account)
        path.parent.mkdir(parents=True, exist_ok=True)
        rt["updated_at"] = _now()
        _atomic_write_text(
            path,
            json.dumps(rt, ensure_ascii=False, indent=2),
        )


def update_runtime(account: str, **fields: Any) -> None:
    # 整个「读-改-写」必须持锁：否则发送循环写 _progress 时，
    # 会用它读到的旧快照覆盖掉 api_stop 刚写入的 _force_stop=True，
    # 导致「强制停止」信号丢失、本轮继续发完。
    with _lock:
        rt = load_runtime(account)
        rt.update(fields)
        _save(account, rt)


def set_running(account: str, value: bool) -> None:
    rt = load_runtime(account)
    rt["running"] = bool(value)
    if value:
        rt["last_error"] = None
    _save(account, rt)


def record_run(account: str, result: dict) -> None:
    rt = load_runtime(account)

    rt["last_run"] = result

    # 上一次运行时间（优先用任务自身记录的时间，回退到当前时间）
    try:
        rt["last_run_at"] = result.get("at") or _now()
    except Exception:
        rt["last_run_at"] = _now()

    history = rt.get("history", [])
    if not isinstance(history, list):
        history = []
    history.insert(0, result)
    rt["history"] = history[:30]

    # 历史运行总次数（累计，不因 history 截断而丢失）
    try:
        prev_total = int(rt.get("history_count") or 0)
    except (TypeError, ValueError):
        prev_total = 0
    rt["history_count"] = prev_total + 1

    rt["running"] = False
    rt["rate_limited"] = bool(result.get("rate_limited", False))

    # ========================================================
    # 登录状态判断逻辑
    # ========================================================

    ok_items = result.get("ok", [])
    failed_items = result.get("failed", [])
    logged_out = result.get("logged_out", False)
    rate_limited = result.get("rate_limited", False)

    if not isinstance(ok_items, list):
        ok_items = []

    if not isinstance(failed_items, list):
        failed_items = []

    # ========================================================
    # 情况1：明确标记为登录失效
    # ========================================================

    if logged_out:

        rt["session_status"] = "expired"
        rt["last_error"] = result.get("stop_reason") or "登录状态已失效"

    # ========================================================
    # 情况2：有失败记录
    # ========================================================

    elif failed_items:

        # 检查失败原因是否包含登录相关关键词
        login_keywords = [
            "扫码登录",
            "登录已过期",
            "登录态已过期",
            "登录状态失效",
            "登录状态已失效",
            "页面已跳转到登录页",
            "请先登录",
            "立即登录",
            "登录后使用",
            "登录后发送",
            "sessionid",
            "未检测到 sessionid",
            "Cookie",
            "storage_state",
        ]

        is_login_error = False

        for item in failed_items:
            if isinstance(item, dict):
                reason = str(item.get("reason", ""))
            else:
                reason = str(item)

            if any(keyword in reason for keyword in login_keywords):
                is_login_error = True
                break

        if is_login_error:
            # 登录相关错误 → 登录失效
            rt["session_status"] = "expired"
            rt["last_error"] = str(failed_items[-1]) if failed_items else "登录失效"

        elif rate_limited:
            # 限流 → 登录正常
            rt["session_status"] = "ok"
            rt["last_error"] = "触发限流"

        else:
            # 其他失败 → 登录失效（安全策略）
            rt["session_status"] = "expired"
            rt["last_error"] = str(failed_items[-1]) if failed_items else "操作失败"

    # ========================================================
    # 情况3：全部成功 → 登录正常
    # ========================================================

    else:

        rt["session_status"] = "ok"
        rt["last_error"] = None

    # --------------------------------------------------------
    # 保存最近错误
    # --------------------------------------------------------

    if rt.get("last_error") is None and failed_items:

        last_failed = failed_items[-1]

        if isinstance(last_failed, dict):
            rt["last_error"] = last_failed.get("reason")
        else:
            rt["last_error"] = str(last_failed)

    _save(account, rt)


def _clean_contact_name(raw: str) -> str:
    s = " ".join(str(raw or "").split()).strip()
    if not s:
        return ""
    s = re.sub(
        r"\s*(刚刚|昨天|今天|前天|星期[一二三四五六日天]|周[一二三四五六日天])\s*(\d{1,2}:\d{2})?\s*$",
        "",
        s,
    )
    s = re.sub(r"\s*\d{1,2}月\d{1,2}日(\s*\d{1,2}:\d{2})?\s*$", "", s)
    s = re.sub(r"\s*\d{4}[-/]\d{1,2}[-/]\d{1,2}(\s*\d{1,2}:\d{2})?\s*$", "", s)
    s = re.sub(r"\s*\d{1,2}:\d{2}\s*$", "", s)
    return s.strip()


def _normalize_contacts(names: list) -> list:
    """清洗昵称时间后缀并按昵称去重。"""
    out = []
    seen = set()
    for item in names:
        if isinstance(item, str):
            name = _clean_contact_name(item)
            if not name or name in seen:
                continue
            seen.add(name)
            out.append({
                "name": name,
                "douyin_name": name,
                "douyin_id": "",
                "streak": "",
            })
            continue
        if not isinstance(item, dict):
            continue
        name = _clean_contact_name(
            item.get("name") or item.get("douyin_name") or ""
        )
        if not name or name in seen:
            continue
        seen.add(name)
        out.append({
            "name": name,
            "douyin_name": name,
            "douyin_id": str(item.get("douyin_id") or "").strip(),
            "streak": str(item.get("streak") or "").strip(),
        })
    return out


def record_contacts(account: str, data: dict) -> None:
    rt = load_runtime(account)
    names = data.get("names", [])
    if not isinstance(names, list):
        names = []
    rt["contacts"] = _normalize_contacts(names)
    rt["contacts_at"] = data.get("at")
    rt["contacts_error"] = data.get("error")
    _save(account, rt)


def sync_mode_status(account: str) -> None:
    """从配置同步模式状态到运行时"""
    status = get_mode_status(account)
    rt = load_runtime(account)
    rt.update({
        "mode": status["mode"],
        "enabled": status["enabled"],
        "authorized_until": status["authorized_until"],
        "mode_active": status["is_active"],
        "mode_status_text": status["status_text"],
    })
    _save(account, rt)


# ============================================================
# 日志相关
# ============================================================

class RingHandler(logging.Handler):
    def __init__(self, account: str) -> None:
        super().__init__()
        self.account = account

    def emit(self, record: logging.LogRecord) -> None:
        try:
            _ring(self.account).append(self.format(record))
        except Exception:
            pass


class DailyFileHandler(logging.Handler):
    """按天写入 logs/YYYY-MM-DD.log，每天一个文件。"""

    def __init__(self, account: str) -> None:
        super().__init__()
        self.account = account
        self._current_date: str | None = None
        self._stream = None
        self._app_stream = None

    def _ensure_stream(self) -> None:
        today = datetime.now().strftime("%Y-%m-%d")
        if self._current_date == today and self._stream is not None:
            return
        if self._stream is not None:
            try:
                self._stream.close()
            except Exception:
                pass
        log_dir = logs_dir(self.account)
        log_dir.mkdir(parents=True, exist_ok=True)
        path = log_dir / f"{today}.log"
        self._stream = open(path, "a", encoding="utf-8")
        self._current_date = today
        try:
            if self._app_stream is None:
                self._app_stream = open(log_dir / "app.log", "a", encoding="utf-8")
        except Exception:
            self._app_stream = None

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self._ensure_stream()
            msg = self.format(record) + "\n"
            self._stream.write(msg)
            self._stream.flush()
            if self._app_stream is not None:
                self._app_stream.write(msg)
                self._app_stream.flush()
        except Exception:
            self.handleError(record)

    def close(self) -> None:
        try:
            if self._stream:
                self._stream.close()
            if self._app_stream:
                self._app_stream.close()
        except Exception:
            pass
        super().close()


def setup_logging(account: str) -> logging.Logger:
    logger = logging.getLogger(f"auto-douyin-sparks-pro.{account}")
    if logger.handlers:
        return logger

    logger.setLevel(logging.INFO)
    logger.propagate = False

    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")

    log_dir = logs_dir(account)
    log_dir.mkdir(parents=True, exist_ok=True)

    fh = DailyFileHandler(account)
    fh.setFormatter(fmt)

    sh = logging.StreamHandler()
    sh.setFormatter(fmt)

    rh = RingHandler(account)
    rh.setFormatter(fmt)

    logger.addHandler(fh)
    logger.addHandler(sh)
    logger.addHandler(rh)

    return logger


def list_log_files(account: str) -> list[dict]:
    """列出账号下按天保存的日志文件。"""
    log_dir = logs_dir(account)
    if not log_dir.exists():
        return []
    result = []
    for p in sorted(log_dir.glob("*.log"), reverse=True):
        if p.name == "app.log":
            continue
        try:
            result.append({
                "name": p.name,
                "date": p.stem,
                "size": p.stat().st_size,
            })
        except Exception:
            pass
    return result


def read_log_file(account: str, date: str | None = None, n: int = 500) -> str:
    """读取某天日志；date 为空则读今天。"""
    log_dir = logs_dir(account)
    if not date:
        date = datetime.now().strftime("%Y-%m-%d")
    date = "".join(c for c in str(date) if c.isdigit() or c == "-")[:16]
    path = log_dir / f"{date}.log"
    if not path.exists():
        path = log_dir / "app.log"
        if not path.exists():
            return ""
    try:
        lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
        n = max(1, min(int(n), 5000))
        return "\n".join(lines[-n:])
    except Exception:
        return ""


def recent_logs(account: str, n: int = 300) -> list[str]:
    n = max(1, min(int(n), 600))
    ring = list(_ring(account))[-n:]
    if ring:
        return ring
    text = read_log_file(account, None, n)
    if not text:
        return []
    return text.splitlines()[-n:]
