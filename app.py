"""云逸续火花助手：多账号抖音续火花 Web 服务入口。"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import (
    FastAPI,
    File,
    Header,
    HTTPException,
    Request,
    UploadFile,
)
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from playwright.sync_api import sync_playwright

from core import automation, scheduler
from core import auth as auth_mod

from core.config import (
    account_dir,
    list_accounts,
    load_config,
    save_config,
    state_path,
    create_account,
    rename_account,
    delete_account,
    get_mode_status,
    add_authorized_days,
    load_profile,
    save_profile,
    load_license,
    save_license,
    drop_license,
)

from core.runtime import (
    drop_ring,
    load_runtime,
    recent_logs,
    record_contacts,
    record_run,
    set_running,
    setup_logging,
    update_runtime,
    sync_mode_status,
    _normalize_contacts,
    list_log_files,
    read_log_file,
)
from core import email_util
from core import site_settings
from core import stats as stats_mod
from core import card_keys


BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"

# ============================================================
# 服务器访问日志（谁在什么时间、从哪个 IP、访问了什么、多大、多快）
#   · 默认开启；设 ACCESS_LOG=0 可关闭
#   · 按大小轮转：ACCESS_LOG_MAX_MB（默认 10MB）、ACCESS_LOG_BACKUPS（默认 5，即最多约 60MB）
#   · 落到 data/logs/access.log，同时输出到 stdout（journalctl 也能看）
# ============================================================
LOG_DIR = BASE_DIR / "data" / "logs"
ACCESS_LOG_FILE = LOG_DIR / "access.log"
ACCESS_LOG_ENABLED = os.environ.get("ACCESS_LOG", "1") != "0"


def _env_int(name: str, default: int, lo: int = 1, hi: int = 100000) -> int:
    try:
        return max(lo, min(int(os.environ.get(name, default)), hi))
    except (TypeError, ValueError):
        return default


ACCESS_LOG_MAX_BYTES = _env_int("ACCESS_LOG_MAX_MB", 10) * 1024 * 1024
ACCESS_LOG_BACKUPS = _env_int("ACCESS_LOG_BACKUPS", 5)

_access_logger = None
_access_log_guard = threading.Lock()


def _get_access_logger():
    """懒加载访问日志 logger（带按大小轮转）。"""
    global _access_logger
    if _access_logger is not None:
        return _access_logger
    with _access_log_guard:
        if _access_logger is not None:
            return _access_logger
        lg = logging.getLogger("dsh.access")
        lg.setLevel(logging.INFO)
        lg.propagate = False
        if not lg.handlers:
            # 文件：按大小轮转
            try:
                LOG_DIR.mkdir(parents=True, exist_ok=True)
                from logging.handlers import RotatingFileHandler
                fh = RotatingFileHandler(
                    str(ACCESS_LOG_FILE),
                    maxBytes=ACCESS_LOG_MAX_BYTES,
                    backupCount=ACCESS_LOG_BACKUPS,
                    encoding="utf-8",
                )
                fh.setFormatter(logging.Formatter(
                    "%(asctime)s  %(message)s", datefmt="%Y-%m-%d %H:%M:%S"))
                lg.addHandler(fh)
            except Exception:
                logging.exception("初始化访问日志文件失败（不影响服务）")
            # 控制台：方便 journalctl -u 直接看
            try:
                sh = logging.StreamHandler()
                sh.setFormatter(logging.Formatter("%(message)s"))
                lg.addHandler(sh)
            except Exception:
                pass
        _access_logger = lg
        return lg


def _human_size(n: int) -> str:
    """把字节数变成可读大小。"""
    if n is None or n < 0:
        return "-"
    if n < 1024:
        return "%dB" % n
    if n < 1024 * 1024:
        return "%.1fKB" % (n / 1024.0)
    return "%.2fMB" % (n / (1024.0 * 1024.0))


def _resp_size(resp) -> int:
    """尽力取响应体大小；取不到返回 -1。"""
    try:
        cl = resp.headers.get("content-length")
        if cl is not None and str(cl).strip().isdigit():
            return int(cl)
    except Exception:
        pass
    try:
        body = getattr(resp, "body", None)
        if isinstance(body, (bytes, bytearray, str)):
            return len(body.encode("utf-8") if isinstance(body, str) else body)
    except Exception:
        pass
    return -1


# ============================================================
# 环境变量
# ============================================================

def _load_env() -> None:
    env_path = BASE_DIR / ".env"

    if not env_path.exists():
        return

    try:
        lines = env_path.read_text(
            encoding="utf-8"
        ).splitlines()
    except Exception:
        return

    for line in lines:
        line = line.strip()

        if (
            line
            and not line.startswith("#")
            and "=" in line
        ):
            key, value = line.split("=", 1)

            os.environ.setdefault(
                key.strip(),
                value.strip(),
            )


_load_env()


AUTH_TOKEN = os.environ.get(
    "AUTH_TOKEN",
    "",
).strip()


# ============================================================
# 认证（多用户 + 可选 AUTH_TOKEN）
# ============================================================

def _check_auth(token: str) -> None:
    """校验会话令牌或兼容旧的 AUTH_TOKEN。"""
    if not token:
        raise HTTPException(
            status_code=401,
            detail="未登录，请先登录",
        )
    if AUTH_TOKEN and token == AUTH_TOKEN:
        return
    username = auth_mod.validate_session(token)
    if not username:
        raise HTTPException(
            status_code=401,
            detail="登录已过期，请重新登录",
        )


def _current_username(token: str) -> str:
    if AUTH_TOKEN and token == AUTH_TOKEN:
        return auth_mod.get_admin_username()
    username = auth_mod.validate_session(token)
    if not username:
        raise HTTPException(
            status_code=401,
            detail="登录已过期，请重新登录",
        )
    return username



def _login_method_enabled(method: str) -> bool:
    try:
        s = site_settings.load()
        lm = s.get("login_methods") or {}
        return bool(lm.get(method, True))
    except Exception:
        return True


def _require_admin(token: str) -> str:
    username = _current_username(token)
    if not auth_mod.is_admin(username):
        raise HTTPException(
            status_code=403,
            detail="需要管理员权限",
        )
    return username


def _visible_accounts(token: str) -> list[str]:
    """管理员看全部，普通用户只看自己绑定的账号。"""
    username = _current_username(token)
    if auth_mod.is_admin(username):
        return list_accounts()
    return auth_mod.accounts_owned_by(username)


def _assert_account_access(token: str, account: str) -> str:
    username = _current_username(token)
    if auth_mod.is_admin(username):
        return account
    # 必须校验全部绑定账号，而不是只看第一个，
    # 否则管理员给用户绑定多个账号后，用户对第 2 个起的操作会 403。
    owned_list = auth_mod.accounts_owned_by(username)
    if account not in owned_list:
        raise HTTPException(
            status_code=403,
            detail="无权操作该账号",
        )
    return account


# ============================================================
# 账号
# ============================================================

def _get_account(
    account: str | None,
    token: str | None = None,
) -> str:

    if token is not None:
        accounts = _visible_accounts(token)
    else:
        accounts = list_accounts()

    if not accounts:
        raise HTTPException(
            status_code=503,
            detail="当前没有配置任何账号",
        )

    if not account:
        return accounts[0]

    account = account.strip()

    if not account:
        return accounts[0]

    if account not in accounts:
        raise HTTPException(
            status_code=404,
            detail=f"账号不存在: {account}",
        )

    return account


# ============================================================
# 多账号运行锁
# ============================================================

run_locks: dict[str, threading.Lock] = {}

# 全站并行控制（默认 1=串行，可在后台调大）
_global_send_lock = threading.Lock()
_global_running_accounts: set[str] = set()

contacts_status: dict[str, bool] = {}

_locks_guard = threading.Lock()


def _lock(account: str) -> threading.Lock:
    with _locks_guard:
        lock = run_locks.get(account)

        if lock is None:
            lock = threading.Lock()
            run_locks[account] = lock

        return lock


def _max_parallel() -> int:
    try:
        s = site_settings.load()
        return max(1, min(int((s.get("spark_runtime") or {}).get("max_parallel") or 1), 50))
    except Exception:
        return 1


def _try_acquire_global(account: str) -> tuple[bool, str]:
    with _global_send_lock:
        if account in _global_running_accounts:
            return False, f"账号「{account}」已在运行"
        limit = _max_parallel()
        if len(_global_running_accounts) >= limit:
            busy = "、".join(sorted(_global_running_accounts)) or "其他账号"
            return False, f"当前并行已满（上限 {limit}）：{busy}，请稍后再试"
        _global_running_accounts.add(account)
        return True, ""


def _release_global(account: str) -> None:
    with _global_send_lock:
        _global_running_accounts.discard(account)


def _logger(account: str):
    return setup_logging(account)


# ============================================================
# 网页登录（跳转浏览器）
# ============================================================

login_sessions: dict[str, dict] = {}

_login_guard = threading.Lock()

LOGIN_TIMEOUT = 300

LOGIN_URL = "https://www.douyin.com/chat"

# ------------------------------------------------------------
# 网页扫码登录（qr 模式）：服务器无图形界面也可用。
# headless 打开抖音登录页 → 提取页面上的二维码（img data:URL /
# qr URL / canvas）→ 前端轮询展示 → 用户手机扫码 → 轮询 Cookie
# 判定登录（与 check_login 一致），扫码后的「保存登录信息」弹窗
# 自动勾选，安全验证页面截图给用户看。
# 提取逻辑移植自抖音 WebView 登录脚本（douyin_login.js）。
# ------------------------------------------------------------
_QR_EXTRACT_JS = """() => {
  function findQr() {
    var imgs = document.querySelectorAll('img');
    for (var i = 0; i < imgs.length; i++) {
      var im = imgs[i]; var r = im.getBoundingClientRect();
      if (r.width > 80 && r.width < 420 && Math.abs(r.width - r.height) < 30) {
        var s = im.src || '';
        if (s.indexOf('data:image') === 0) return s;
        if (s.indexOf('http') === 0 && (s.toLowerCase().indexOf('qr') >= 0 || s.toLowerCase().indexOf('qrcode') >= 0)) return 'URL:' + s;
      }
    }
    var cvs = document.querySelectorAll('canvas');
    for (var j = 0; j < cvs.length; j++) {
      var c = cvs[j]; var r2 = c.getBoundingClientRect();
      if (r2.width > 80 && r2.width < 420 && Math.abs(r2.width - r2.height) < 30) {
        try { var d = c.toDataURL('image/png'); if (d && d.length > 100) return d; } catch (e) {}
      }
    }
    return null;
  }
  return findQr();
}"""

# 扫码确认后抖音可能弹「保存登录信息」；自动勾选信任开关并点保存
_AUTO_SAVE_LOGIN_JS = """() => {
  try {
    var toggle = document.querySelector('.trust-login-switch-button');
    if (toggle && (toggle.className || '').indexOf('check') < 0) { toggle.click(); return 'toggle'; }
  } catch (e) {}
  try {
    var btns = document.querySelectorAll('button, div[role=button], span[role=button], a[role=button]');
    for (var i = 0; i < btns.length; i++) {
      var b = btns[i];
      var t = (b.textContent || '').trim();
      if ((t === '保存' || t === '保存登录信息') && b.offsetParent) { b.click(); return 'save'; }
    }
  } catch (e) {}
  return null;
}"""


def _click_by_text_any_frame(page, texts: list[str], exact: bool = True) -> bool:
    """跨所有 frame 查找并点击可见元素。

    抖音的「身份验证」弹窗经常渲染在 iframe 里，只搜主文档会
    永远找不到按钮——这就是卡在验证选择页不动的原因。
    """
    for frame in [page.main_frame] + list(page.frames):
        if frame is None:
            continue
        try:
            if _click_by_text(frame, texts, exact=exact):
                return True
        except Exception:
            continue
    return False


def _click_by_text(page, texts: list[str], exact: bool = True) -> bool:
    """按文案匹配依次尝试点击【可见】的元素，点成功才返回 True。

    注意必须逐个校验可见性（bounding_box）：页面里常有隐藏的
    模板/脚本节点包含相同文案，直接 loc.first.click 会点到
    看不见的元素导致超时，状态也会被误判。
    """
    for t in texts:
        try:
            loc = page.get_by_text(t, exact=exact)
            n = loc.count()
        except Exception:
            continue
        for i in range(min(n, 8)):
            el = loc.nth(i)
            try:
                if not el.bounding_box():
                    continue
                el.click(timeout=3000)
                return True
            except Exception:
                continue
    return False


def _fill_sms_code_boxes(page, code: str) -> bool:
    """6 格分位输入框布局（旧版）：逐格填入后点确认。"""
    inputs = page.locator("input:visible")
    try:
        single = 0
        for i in range(min(inputs.count(), 10)):
            if (inputs.nth(i).get_attribute("maxlength") or "") == "1":
                single += 1
        if single >= 4:
            for i, d in enumerate(code):
                box = inputs.nth(i)
                box.click(timeout=2000)
                box.fill(d)
                page.wait_for_timeout(100)
            page.wait_for_timeout(300)
            _click_by_text_any_frame(
                page, ["验证", "确定", "确认", "提交", "下一步", "登录"], exact=True
            )
            return True
    except Exception:
        pass
    return False


def _find_sms_input(page):
    """跨 iframe 查找验证码输入框。

    只要求「可见」——抖音的输入框常带 readonly 属性（防手机键盘
    自动弹出，点击后才解除），按 is_editable 过滤会把它们漏掉。
    优先 placeholder 含「验证码」的元素；兼容 contenteditable 组件。
    """
    selectors = [
        "input[placeholder*='验证码']",
        "[placeholder*='验证码']",
        "input[placeholder*='验证']",
        "input",
        "textarea",
        "[contenteditable='true']",
    ]
    for frame in [page.main_frame] + list(page.frames):
        if frame is None:
            continue
        for sel in selectors:
            try:
                loc = frame.locator(sel + ":visible")
                n = min(loc.count(), 5)
            except Exception:
                continue
            for i in range(n):
                el = loc.nth(i)
                try:
                    if el.bounding_box():
                        return el
                except Exception:
                    continue
    return None


def _sms_debug_dump(page: object, account: str, extra: str = "") -> None:
    """填码失败时把各 frame 的输入框 DOM 信息写到账号目录，便于排查。"""
    try:
        from .config import account_dir
        lines = [f"===== {time.strftime('%Y-%m-%d %H:%M:%S')} {extra} ====="]
        for fr in [page.main_frame] + list(page.frames):
            if fr is None:
                continue
            try:
                lines.append(f"--- frame url={str(fr.url)[:100]}")
            except Exception:
                lines.append("--- frame url=?")
            for sel in ("input", "textarea", "[contenteditable='true']",
                        "[placeholder*='验证码']", "[placeholder]"):
                try:
                    loc = fr.locator(sel)
                    cnt = loc.count()
                    lines.append(f"  {sel}: {cnt}")
                    for i in range(min(cnt, 3)):
                        try:
                            html = loc.nth(i).evaluate(
                                "e => e.outerHTML.slice(0, 400)"
                            )
                            lines.append(f"    [{i}] {html}")
                        except Exception as e:
                            lines.append(f"    [{i}] eval err: {e}")
                except Exception as e:
                    lines.append(f"  {sel} ERR: {e}")
        log_path = account_dir(str(account)) / "sms_debug.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(chr(10).join(lines) + chr(10))
    except Exception:
        pass


def _fill_sms_code(page: object, code: str, account: str = "") -> bool:
    """填入 6 位短信验证码并点击「验证」按钮。

    策略链：
      1) 找到 input/contenteditable 元素 → 双击激活 → 解除 readonly
         → 逐字键入 → 校验值写入（失败 fill 兜底）
      2) 找不到元素时按坐标兜底：定位「重新发送」文字，点击同一行
         左侧的输入区域两次，再用键盘键入
      3) 点击「验证」按钮
    两条策略全失败时写 sms_debug.log 供排查。
    """
    # 校验：必须是 6 位数字才继续
    code = "".join(c for c in (code or "") if c.isdigit())[:6]
    if len(code) != 6:
        return False

    # ---------- 策略 1：元素级 ----------
    target = _find_sms_input(page)
    if target is not None:
        try:
            ml = (target.get_attribute("maxlength") or "").strip()
        except Exception:
            ml = ""
        if ml == "1":
            if _fill_sms_code_boxes(page, code):
                _click_by_text_any_frame(
                    page, ["验证", "确定", "确认", "提交", "下一步", "登录"], exact=True
                )
                return True
            target = None
        else:
            try:
                target.click(timeout=2500)
                page.wait_for_timeout(150)
                target.click(timeout=2500)
                page.wait_for_timeout(200)
                try:
                    target.evaluate(
                        "e => { e.removeAttribute('readonly');"
                        " e.removeAttribute('disabled');"
                        " if (e.focus) e.focus(); }"
                    )
                except Exception:
                    pass
                try:
                    target.click(timeout=1500)
                except Exception:
                    pass
                try:
                    target.type(code, delay=90)
                except Exception:
                    pass
                got = ""
                try:
                    got = target.input_value()
                except Exception:
                    try:
                        got = target.inner_text()
                    except Exception:
                        got = ""
                if code not in (got or ""):
                    try:
                        target.fill(code)
                    except Exception:
                        pass
                # 点击「验证」并视为成功
                page.wait_for_timeout(300)
                _click_by_text_any_frame(
                    page, ["验证", "确定", "确认", "提交", "下一步", "登录"], exact=True
                )
                return True
            except Exception:
                pass

    # ---------- 策略 2：坐标兜底 ----------
    # 「请输入验证码」输入框和「重新发送」在同一行：定位该文字，
    # 点它左侧同一行的输入区域两次，再用键盘键入。
    try:
        for fr in [page.main_frame] + list(page.frames):
            if fr is None:
                continue
            hit = False
            for t in ("后重新发送", "重新发送", "重新获取"):
                try:
                    loc = fr.get_by_text(t, exact=False)
                    n = min(loc.count(), 5)
                except Exception:
                    continue
                for i in range(n):
                    try:
                        box = loc.nth(i).bounding_box()
                    except Exception:
                        box = None
                    if not box or box.get("width", 0) <= 0:
                        continue
                    x = max(box["x"] - 150, 10)
                    y = box["y"] + box["height"] / 2
                    page.mouse.click(x, y)
                    page.wait_for_timeout(150)
                    page.mouse.click(x, y)
                    page.wait_for_timeout(300)
                    page.keyboard.type(code, delay=90)
                    hit = True
                    break
                if hit:
                    break
            if hit:
                page.wait_for_timeout(300)
                _click_by_text_any_frame(
                    page, ["验证", "确定", "确认", "提交", "下一步", "登录"], exact=True
                )
                return True
    except Exception:
        pass

    # ---------- 全部失败：写调试转储 ----------
    _sms_debug_dump(page, account, extra=f"填码失败 code_len={len(code)}")
    return False


def _auto_handle_verification(page, info: dict) -> None:
    """扫码后的「身份验证」自动化（必须点到可见元素才算成功）：

    1. 二维码消失 5 秒后 → 自动点击「接收短信验证码」
    2. 进入验证码输入页 → 自动点击「获取验证码」（可见且点击成功才算），
       然后置 need_sms_code 让网页弹窗显示 6 位输入框
    3. 用户在网页提交验证码后 → 自动填入并点确定
    """
    # 3) 用户已提交验证码：填入并提交（优先级最高）
    if info.get("code_event") and info["code_event"].is_set():
        code = info.get("sms_code") or ""
        info["code_event"].clear()
        if _fill_sms_code(page, code, account=info.get("account") or ""):
            info["message"] = "验证码已自动填入并提交，正在检测登录结果…"
        else:
            info["message"] = "验证码填入失败，请按实时画面手动核对（已记录调试日志）"
        return

    now = time.time()
    qr_gone_at = float(info.get("qr_gone_at") or 0)

    # 1) 验证方式选择页：扫完码 3 秒后全自动点击「接收短信验证码」
    #    （弹窗可能在 iframe 里，跨所有 frame 查找；网页上的倒计时
    #    按钮仍可手动触发，走同一个 sms_click_event）
    if not info.get("sms_option_clicked"):
        if not qr_gone_at:
            return
        user_clicked = info.get("sms_click_event") and info["sms_click_event"].is_set()
        if user_clicked or (now - qr_gone_at >= 3):
            info["sms_click_event"].clear()
            picked = _click_by_text_any_frame(
                page,
                ["接收短信验证码", "使用短信验证码", "短信验证码登录"],
                exact=True,
            ) or _click_by_text_any_frame(
                page,
                ["接收短信验证码", "短信验证码"],
                exact=False,
            )
            if picked:
                info["sms_option_clicked"] = True
                info["waiting_sms_confirm"] = False
                info["sms_clicked_at"] = now
                # 输入框立刻弹出，「获取验证码」由后台自动点击
                info["need_sms_code"] = True
                info["message"] = (
                    "已自动点击「接收短信验证码」并正在自动获取验证码，"
                    "收到短信后请在下方输入 6 位验证码"
                )
            else:
                info["message"] = "正在尝试自动点击「接收短信验证码」…（可点下方按钮重试）"
        else:
            info["waiting_sms_confirm"] = True
            info["message"] = "已扫码完成，即将自动点击「接收短信验证码」…"
        return

    # 2) 短信验证码输入页：自动点「获取验证码」
    if not info.get("sms_send_clicked"):
        # 刚点完选项，等页面跳转
        if now - float(info.get("sms_clicked_at") or 0) < 2:
            return
        sent = _click_by_text_any_frame(
            page,
            ["获取验证码", "获取短信验证码", "点击获取验证码"],
            exact=True,
        )
        if sent:
            info["sms_send_clicked"] = True
            info["need_sms_code"] = True
            info["message"] = (
                "已自动点击「获取验证码」，短信已发送到账号绑定手机，"
                "请在下方输入收到的 6 位验证码"
            )
        else:
            info["message"] = "已选择短信验证，正在等待「获取验证码」按钮出现…"
        return

    # 已发送短信：保持输入框显示，等待用户提交
    info["need_sms_code"] = True
    info["message"] = "请在下方输入手机收到的 6 位短信验证码"


def _fetch_qr_url_as_dataurl(url: str) -> str | None:
    """二维码是远程 URL 时下载转成 data URL，避免前端直连被防盗链拦截。"""
    try:
        import base64
        import requests
        resp = requests.get(
            url,
            timeout=10,
            headers={
                "Referer": "https://www.douyin.com/",
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"
                ),
            },
        )
        ct = (resp.headers.get("Content-Type") or "image/png").split(";")[0].strip()
        if not ct.startswith("image/") or not resp.content:
            return None
        return f"data:{ct};base64,{base64.b64encode(resp.content).decode('ascii')}"
    except Exception:
        return None


def _login_worker(
    account: str,
    stop_event: threading.Event,
) -> None:

    p = None
    browser = None
    context = None
    page = None

    # 登录方式只有一种：headless + 网页二维码（对齐抖音 WebView App 的做法）
    logger = _logger(account)

    try:

        update_runtime(
            account,
            session_status="logging_in",
            login_error=None,
            login_started_at=time.time(),
        )

        state = state_path(account)

        state.parent.mkdir(
            parents=True,
            exist_ok=True,
        )

        p = sync_playwright().start()

        browser = p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                # 内存优化：关闭后台功能，限制 JS 堆
                "--disable-extensions",
                "--disable-background-networking",
                "--disable-default-apps",
                "--disable-sync",
                "--disable-translate",
                "--disable-software-rasterizer",
                "--metrics-recording-only",
                "--no-first-run",
                "--no-zygote",
                "--renderer-process-limit=2",
                "--js-flags=--max-old-space-size=256",
            ],
        )

        browser_args = {
            "viewport": {
                "width": 1366,
                "height": 768,
            },
        }

        if state.exists() and state.stat().st_size > 0:

            try:
                browser_args["storage_state"] = str(
                    state
                )
            except Exception:
                pass

        context = browser.new_context(
            **browser_args
        )

        page = context.new_page()

        logger.info(
            "账号 %s：正在打开抖音登录页面",
            account,
        )

        page.goto(
            LOGIN_URL,
            timeout=90000,
            wait_until="domcontentloaded",
        )

        page.wait_for_timeout(
            5000
        )

        # 线程启动前 login_sessions[account] 已由 start_login 建好
        qr_info = login_sessions.get(account)

        deadline = (
            time.time()
            + LOGIN_TIMEOUT
        )

        while time.time() < deadline:

            # ------------------------------------------------
            # 用户取消登录
            # ------------------------------------------------
            if stop_event.is_set():

                update_runtime(
                    account,
                    session_status="unknown",
                    login_error="用户取消登录",
                    login_started_at=None,
                )

                logger.info(
                    "账号 %s：登录流程已取消",
                    account,
                )

                return

            # ------------------------------------------------
            # 扫码模式：提取二维码 / 自动保存登录信息 / 截图验证页
            # ------------------------------------------------
            if qr_info is not None:

                # 1) 提取页面上的登录二维码
                try:
                    qr_data = page.evaluate(_QR_EXTRACT_JS)
                except Exception:
                    qr_data = None
                if qr_data and str(qr_data).startswith("URL:"):
                    qr_data = _fetch_qr_url_as_dataurl(str(qr_data)[4:])

                if qr_data:
                    qr_info["qr"] = qr_data
                    qr_info["qr_found"] = True
                    qr_info["message"] = "请用手机抖音 App 扫描二维码，并在手机上确认登录"
                    qr_info["shot"] = None
                else:
                    if qr_info.get("qr_found"):
                        # 之前有码、现在没了：已扫码，进入确认/验证阶段
                        qr_info["qr"] = None
                        if not qr_info.get("qr_gone_at"):
                            qr_info["qr_gone_at"] = time.time()
                            # 只在进入验证阶段时提示一次，
                            # 后续消息交给 _auto_handle_verification 管理
                            qr_info["message"] = (
                                "二维码已消失，大概率已在手机上扫码。"
                                "请在手机上确认登录；若出现身份验证会自动处理"
                            )
                    # 2) 自动勾选信任设备并点「保存登录信息」
                    try:
                        page.evaluate(_AUTO_SAVE_LOGIN_JS)
                    except Exception:
                        pass

                    # 2.5) 身份验证自动化：选短信方式 / 点获取验证码 / 填码提交
                    try:
                        _auto_handle_verification(page, qr_info)
                    except Exception:
                        pass
                    # 3) 页面截图持续刷新（每 2 秒一张），验证页面全程可见
                    try:
                        if time.time() - float(qr_info.get("shot_at") or 0) > 2:
                            import base64 as _b64
                            buf = page.screenshot(type="jpeg", quality=55)
                            qr_info["shot"] = _b64.b64encode(buf).decode("ascii")
                            qr_info["shot_at"] = time.time()
                    except Exception:
                        pass

                # 4) 页面出现安全验证 / 验证码提示时更新状态文案
                try:
                    kw = automation.detect_rate_limit(page)
                    if (kw and qr_info.get("qr_found") and not qr_info.get("qr")
                            and not qr_info.get("need_sms_code")
                            and not qr_info.get("sms_option_clicked")):
                        qr_info["message"] = (
                            f"页面出现「{kw}」：请按页面截图提示在手机上完成验证，"
                            "完成后登录会自动检测"
                        )
                except Exception:
                    pass

            # ------------------------------------------------
            # 检查登录状态
            # ------------------------------------------------
            try:

                logged, reason = automation.check_login(
                    page
                )

            except Exception as exc:

                logger.warning(
                    "账号 %s：检查登录状态失败：%s",
                    account,
                    exc,
                )

                logged = False
                reason = str(exc)

            # ------------------------------------------------
            # 登录成功
            # ------------------------------------------------
            if logged:

                logger.info(
                    "账号 %s：检测到网页登录成功",
                    account,
                )

                # 保存 cookies / storage state
                try:

                    context.storage_state(
                        path=str(state)
                    )

                    logger.info(
                        "账号 %s：登录状态已保存",
                        account,
                    )

                except Exception as exc:

                    logger.warning(
                        "账号 %s：保存登录状态失败：%s",
                        account,
                        exc,
                    )

                # ====================================================
                # 提取用户信息（昵称 + 抖音号）
                # 优先页面解析，不足时用 Cookie API 补全
                # ====================================================
                user_info = {"nickname": "", "unique_id": "", "sec_uid": ""}

                try:
                    page.wait_for_timeout(3000)
                    user_info = automation.extract_user_info_from_page(page) or user_info
                    logger.info(
                        "账号 %s：页面提取用户信息 - 昵称=%s, 抖音号=%s",
                        account,
                        user_info.get("nickname", ""),
                        user_info.get("unique_id", ""),
                    )
                except Exception as e:
                    logger.warning("账号 %s：页面提取用户信息失败：%s", account, e)

                # 页面拿不到完整信息时，用 Cookie 再补一次
                if not user_info.get("nickname") or not user_info.get("unique_id"):
                    try:
                        cookie_info = automation.get_user_info_from_cookie(account)
                        if "error" not in cookie_info:
                            user_info["nickname"] = (
                                user_info.get("nickname")
                                or cookie_info.get("nickname")
                                or ""
                            )
                            user_info["unique_id"] = (
                                user_info.get("unique_id")
                                or cookie_info.get("unique_id")
                                or ""
                            )
                            user_info["sec_uid"] = (
                                user_info.get("sec_uid")
                                or cookie_info.get("sec_uid")
                                or ""
                            )
                            logger.info(
                                "账号 %s：Cookie 补全用户信息 - 昵称=%s, 抖音号=%s",
                                account,
                                user_info.get("nickname", ""),
                                user_info.get("unique_id", ""),
                            )
                        else:
                            logger.warning(
                                "账号 %s：Cookie 补全失败：%s",
                                account,
                                cookie_info.get("error"),
                            )
                    except Exception as e:
                        logger.warning("账号 %s：Cookie 补全异常：%s", account, e)

                # 更新运行状态 - 登录成功
                update_runtime(
                    account,
                    douyin_name=user_info.get("nickname") or "",
                    douyin_id=user_info.get("unique_id") or "",
                    sec_uid=user_info.get("sec_uid") or "",
                    douyin_avatar=user_info.get("avatar") or "",
                    session_status="ok",
                    last_login_at=time.strftime(
                        "%Y-%m-%dT%H:%M:%S"
                    ),
                    login_error=None,
                    login_started_at=None,
                )

                logger.info(
                    "账号 %s：网页登录成功，抖音昵称=%s，抖音号=%s",
                    account,
                    user_info.get("nickname") or "未获取到",
                    user_info.get("unique_id") or "未获取到",
                )

                return

            page.wait_for_timeout(
                1500
            )

        # ----------------------------------------------------
        # 登录超时
        # ----------------------------------------------------

        update_runtime(
            account,
            session_status="expired",
            login_error="登录等待超时，请重新点击登录",
            login_started_at=None,
        )

        logger.warning(
            "账号 %s：登录等待超时",
            account,
        )

    except Exception as exc:

        logger.exception(
            "账号 %s：网页登录流程异常",
            account,
        )

        try:

            update_runtime(
                account,
                session_status="failed",
                login_error=str(exc),
                login_started_at=None,
            )

        except Exception:

            logger.exception(
                "账号 %s：写入登录错误状态失败",
                account,
            )

    finally:

        try:
            if page:
                page.close()
        except Exception:
            pass

        try:
            if context:
                context.close()
        except Exception:
            pass

        try:
            if browser:
                browser.close()
        except Exception:
            pass

        try:
            if p:
                p.stop()
        except Exception:
            pass

        with _login_guard:

            login_sessions.pop(
                account,
                None,
            )


def start_login(account: str) -> dict:

    login_mode = "qr"

    with _login_guard:

        existing = login_sessions.get(
            account
        )

        if existing:

            thread = existing.get(
                "thread"
            )

            if thread and thread.is_alive():

                return {
                    "started": False,
                    "already_running": True,
                    "account": account,
                    "mode": login_mode,
                }

        # 并行上限：同时最多 N 个账号在扫码登录（站点管理可设）
        try:
            max_login = int(
                (site_settings.load().get("login_runtime") or {}).get("max_parallel") or 2
            )
        except Exception:
            max_login = 2
        alive = [
            a for a, v in login_sessions.items()
            if v.get("thread") and v["thread"].is_alive()
        ]
        if account not in alive and len(alive) >= max_login:
            return {
                "started": False,
                "already_running": False,
                "limit_reached": True,
                "running": alive,
                "account": account,
                "mode": login_mode,
                "message": (
                    f"已有 {len(alive)} 个账号正在扫码登录（上限 {max_login} 个），"
                    "请等待其完成或取消后再试"
                ),
            }

        stop_event = threading.Event()

        thread = threading.Thread(
            target=_login_worker,
            args=(
                account,
                stop_event,
            ),
            daemon=True,
            name=f"login-{account}",
        )

        login_sessions[account] = {
            "thread": thread,
            "stop_event": stop_event,
            "started_at": time.time(),
            "account": account,
            "mode": login_mode,
            "qr": None,
            "qr_found": False,
            "message": "正在打开抖音登录页…",
            "shot": None,
            "shot_at": 0.0,
            "need_sms_code": False,
            "sms_send_clicked": False,
            "sms_option_clicked": False,
            "waiting_sms_confirm": False,
            "fallback_clicked": False,
            "sms_click_event": threading.Event(),
            "sms_clicked_at": 0.0,
            "qr_gone_at": 0.0,
            "sms_code": None,
            "code_event": threading.Event(),
        }

        thread.start()

    return {
        "started": True,
        "already_running": False,
        "account": account,
        "mode": login_mode,
    }


def stop_login(
    account: str,
) -> dict:

    with _login_guard:

        info = login_sessions.get(
            account
        )

        if not info:

            return {
                "stopped": False,
                "account": account,
            }

        event = info.get(
            "stop_event"
        )

        if event:
            event.set()

    return {
        "stopped": True,
        "account": account,
    }


def get_login_status(
    account: str,
) -> dict:

    rt = load_runtime(
        account
    )

    session_status = rt.get(
        "session_status",
        "unknown",
    )

    with _login_guard:

        info = login_sessions.get(
            account
        )

        in_progress = bool(
            info
            and info.get("thread")
            and info["thread"].is_alive()
        )

    return {
        "account": account,
        "in_progress": in_progress,

        "session_status":
            session_status,

        "douyin_name": rt.get("douyin_name") or "",
        "douyin_id": rt.get("douyin_id") or "",

        "login_error": rt.get(
            "login_error"
        ),

        "state_file_exists":
            state_path(
                account
            ).exists(),
    }


# ============================================================
# 启动续火花
# ============================================================

def _start_run(
    account: str,
    dry: bool = False,
    only_names: list[str] | None = None,
    force_email: bool = False,
):
    lock = _lock(account)

    if not lock.acquire(False):

        raise HTTPException(
            status_code=409,
            detail="该账号已有任务运行",
        )

    ok, busy_msg = _try_acquire_global(account)
    if not ok:
        lock.release()
        raise HTTPException(
            status_code=409,
            detail=busy_msg,
        )

    def worker():
        logger = _logger(account)

        try:

            set_running(
                account,
                True,
            )
            # 清除上次强制停止标志，开始新一轮
            update_runtime(account, _force_stop=False)

            result = automation.run_send(
                account,
                dry_run=dry,
                only_names=only_names,
            )

            if not isinstance(
                result,
                dict,
            ):

                result = {
                    "account": account,
                    "dry_run": dry,
                    "ok": [],
                    "failed": [
                        {
                            "name": "_system",
                            "reason": "任务返回结果格式错误",
                        }
                    ],
                    "logged_out": False,
                    "rate_limited": False,
                    "stopped": True,
                    "stop_reason": "任务返回结果格式错误",
                }

            # ====================================================
            # 登录异常
            # ====================================================

            if result.get("logged_out"):

                reason = (
                    result.get(
                        "stop_reason"
                    )
                    or "登录状态已失效，请重新登录"
                )

                update_runtime(
                    account,
                    session_status="expired",
                    login_error=reason,
                    login_started_at=None,
                )

                logger.warning(
                    "账号 %s：登录异常，"
                    "停止自动任务：%s",
                    account,
                    reason,
                )

            # ====================================================
            # 疑似限流
            # ====================================================

            elif result.get("rate_limited"):

                reason = (
                    result.get(
                        "stop_reason"
                    )
                    or "疑似触发抖音限制，任务已停止"
                )

                update_runtime(
                    account,
                    session_status="ok",
                    login_error=reason,
                    login_started_at=None,
                )

                logger.warning(
                    "账号 %s：疑似触发抖音限制，"
                    "停止自动任务：%s",
                    account,
                    reason,
                )

            elif (
                result.get("stopped")
                and result.get("stop_reason")
            ):

                logger.warning(
                    "账号 %s：任务停止：%s",
                    account,
                    result.get(
                        "stop_reason"
                    ),
                )

            record_run(
                account,
                result,
            )

            try:
                _notify_run_email(account, result, force_email=force_email)
            except Exception:
                pass

            # ====================================================
            # 自动补发：本轮无人发送成功时，稍后自动再试一次。
            # 排除：干跑（不真实发送）、登录失效（补发也会被门控拦下）、
            # 用户手动停止（_force_stop 是用户意图，不能违背）。
            # ====================================================
            try:
                if not dry and not result.get("logged_out"):
                    user_stopped = bool(load_runtime(account).get("_force_stop"))
                    if not user_stopped and not (result.get("ok") or []):
                        scheduler.schedule_retry(account)
                        logger.info(
                            "账号 %s：本轮无人发送成功，已安排稍后自动补发", account
                        )
            except Exception:
                pass

            logger.info(
                "账号 %s 完成：成功=%s 失败=%s dry=%s",
                account,
                len(
                    result.get(
                        "ok",
                        [],
                    )
                ),
                len(
                    result.get(
                        "failed",
                        [],
                    )
                ),
                dry,
            )

        except Exception as exc:

            logger.exception(
                "账号 %s 执行异常",
                account,
            )

            try:

                err_result = {
                    "account": account,
                    "dry_run": dry,
                    "ok": [],
                    "failed": [
                        {
                            "name": "_system",
                            "reason": str(exc),
                        }
                    ],
                    "logged_out": False,
                    "rate_limited": False,
                    "stopped": True,
                    "stop_reason": str(exc),
                }
                record_run(account, err_result)
                try:
                    _notify_run_email(account, err_result, force_email=force_email)
                except Exception:
                    pass

                # 任务异常（浏览器崩溃等）同样安排补发；用户手动停止的不补
                try:
                    if not dry:
                        user_stopped = bool(load_runtime(account).get("_force_stop"))
                        if not user_stopped:
                            scheduler.schedule_retry(account)
                except Exception:
                    pass

            except Exception:
                pass

        finally:

            try:

                set_running(
                    account,
                    False,
                )

            except Exception:
                pass

            # 及时回收浏览器/自动化产生的内存垃圾
            import gc as _gc
            _gc.collect()

            try:
                lock.release()
            except Exception:
                pass

            try:
                _release_global(account)
            except Exception:
                pass

    threading.Thread(
        target=worker,
        daemon=True,
        name=f"run-{account}",
    ).start()

# ============================================================
# 联系人
# ============================================================

def _start_fetch_contacts(
    account: str,
):

    lock = _lock(account)

    if not lock.acquire(False):

        raise HTTPException(
            status_code=409,
            detail="该账号正在运行其他任务",
        )

    def worker():

        logger = _logger(account)

        try:

            contacts_status[account] = True

            result = (
                automation.fetch_chat_contacts(
                    account
                )
            )

            if not isinstance(
                result,
                dict,
            ):

                result = {
                    "names": [],
                    "at": None,
                    "error": "联系人任务返回结果格式错误",
                }

            error_text = str(
                result.get("error")
                or ""
            ).strip()

            # ====================================================
            # 判断是否登录失效
            # ====================================================

            login_error_keywords = [
                "扫码登录",
                "登录已过期",
                "登录态已过期",
                "登录状态失效",
                "登录状态已失效",
                "页面已跳转到登录页",
                "登录提示",
                "验证码登录",
                "登录后查看",
                "登录后即可",
                "请先登录",
                "立即登录",
                "登录后使用",
                "登录后发送",
                "sessionid",
                "未检测到 sessionid",
            ]

            login_invalid = any(
                keyword in error_text
                for keyword
                in login_error_keywords
            )

            # ====================================================
            # 只要获取联系人失败，就标记为登录失效
            # ====================================================

            if result.get("error") or login_invalid:

                update_runtime(
                    account,
                    session_status="expired",
                    login_error=(
                        error_text
                        or "登录已失效，请重新登录"
                    ),
                    login_started_at=None,
                )

                logger.warning(
                    "账号 %s：获取联系人失败，标记为登录失效：%s",
                    account,
                    error_text,
                )

            else:

                # ====================================================
                # 获取联系人成功，更新登录状态
                # ====================================================
                update_runtime(
                    account,
                    session_status="ok",
                    login_error=None,
                )

                logger.info(
                    "账号 %s：获取联系人成功，登录状态正常",
                    account,
                )

                # ====================================================
                # 更新用户信息（昵称 + 抖音号）
                # ====================================================
                user_info = result.get("user_info", {})
                if user_info and user_info.get("nickname"):
                    update_runtime(
                        account,
                        douyin_name=user_info.get("nickname") or "",
                        douyin_id=user_info.get("unique_id") or "",
                        sec_uid=user_info.get("sec_uid") or "",
                        douyin_avatar=user_info.get("avatar") or "",
                    )
                    logger.info(
                        "账号 %s：更新用户信息 - 昵称=%s, 抖音号=%s",
                        account,
                        user_info.get("nickname", ""),
                        user_info.get("unique_id", ""),
                    )

            record_contacts(
                account,
                result,
            )

        except Exception as exc:

            logging.exception(
                "账号 %s 获取联系人异常",
                account,
            )

            # 异常也标记为登录失效
            update_runtime(
                account,
                session_status="expired",
                login_error=str(exc),
                login_started_at=None,
            )

            try:

                record_contacts(
                    account,
                    {
                        "names": [],
                        "at": None,
                        "error": str(exc),
                    },
                )

            except Exception:
                pass

        finally:

            contacts_status[account] = False

            lock.release()

    threading.Thread(
        target=worker,
        daemon=True,
        name=f"contacts-{account}",
    ).start()

# ============================================================
# 生命周期
# ============================================================

@asynccontextmanager
async def lifespan(
    app: FastAPI,
):

    try:
        auth_mod.ensure_default_admin()
        logging.info(
            "管理员账号已就绪（默认用户名 admin）"
        )
    except Exception as exc:
        logging.exception(
            "初始化管理员账号失败：%s",
            exc,
        )

    try:

        scheduler.configure_all(
            lambda account: _start_run(
                account,
                False,
            )
        )
        try:
            scheduler.apply_daily_digest_schedule(_send_daily_digest_all)
        except Exception:
            logging.exception("每日邮件汇总调度初始化失败")

        logging.info(
            "多账号调度器初始化完成：%s",
            list_accounts(),
        )

    except Exception as exc:

        logging.exception(
            "调度器启动失败：%s",
            exc,
        )

    yield

    try:
        scheduler.shutdown()

    except Exception:
        logging.exception(
            "调度器关闭失败"
        )


# ============================================================
# FastAPI
# ============================================================

APP_VERSION = "1.6.9"

app = FastAPI(
    title="Yunyi Spark Keeper",
    version=APP_VERSION,
    lifespan=lifespan,
)


class NoCacheStatic(StaticFiles):
    """带长效缓存的静态资源服务。

    优化点：
      1. 优先返回预压缩的 .gz 文件（部署时由 _gzip.py 生成），
         文本资源体积可降到 1/4 左右。
      2. 给静态资源加长缓存（immutable），浏览器二次访问不再重新下载。
      3. HTML 入口不缓存，保证更新后立刻生效。
    """

    def file_response(self, *args, **kwargs):  # type: ignore[override]
        resp = super().file_response(*args, **kwargs)
        try:
            path = str(getattr(resp, "path", "") or "")
        except Exception:
            path = ""

        low = path.lower()
        # vendor/ 是带版本号的第三方库，可以长缓存；
        # 自己的 app.css / boot.js / dist/* 会随发版变化，必须走重验证，
        # 否则浏览器会一直用旧文件（曾导致「新 HTML + 旧 CSS」排版错乱）。
        is_vendor = "/vendor/" in low
        if low.endswith((".js", ".css", ".woff", ".woff2", ".ttf", ".otf")):
            if is_vendor:
                resp.headers["Cache-Control"] = "public, max-age=2592000, immutable"
            else:
                resp.headers["Cache-Control"] = "no-cache, must-revalidate"
        elif low.endswith((".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico",
                           ".webp", ".avif")):
            resp.headers["Cache-Control"] = "public, max-age=604800"
        elif low.endswith(".html"):
            resp.headers["Cache-Control"] = "no-cache, must-revalidate"
        else:
            resp.headers["Cache-Control"] = "public, max-age=3600"

        resp.headers["Vary"] = "Accept-Encoding"
        return resp


@app.middleware("http")
async def gzip_static_middleware(request: Request, call_next):
    """优先返回预压缩的 .gz 资源。

    只处理 /static/ 下的 GET 请求；命中 .gz 则直接发送压缩内容并加上
    Content-Encoding: gzip，浏览器自动解压。未命中就正常走原流程。
    """
    path = request.url.path

    # 生产环境屏蔽未混淆源码；需要排障时设 EXPOSE_DEBUG_JS=1
    if "app.raw.js" in path and os.environ.get("EXPOSE_DEBUG_JS", "0") != "1":
        return Response(content="Not Found", status_code=404, media_type="text/plain")

    if request.method == "GET" and path.startswith("/static/"):
        accepts = (request.headers.get("accept-encoding") or "").lower()
        if "gzip" in accepts:
            # 防目录穿越：
            #  1) 去掉开头的斜杠/反斜杠 —— 否则 pathlib 拼接绝对路径会「重置」到盘根
            #     （Windows 上 '/foo' 会变成 E:\foo，'//host/share' 还会变 UNC）
            #  2) 归一化后必须仍位于 STATIC_DIR 之内
            rel = path[len("/static/"):].lstrip("/\\")
            if rel and ".." not in rel and "\\" not in rel:
                try:
                    root = STATIC_DIR.resolve()
                    gz = (root / (rel + ".gz")).resolve()
                except Exception:
                    gz = None
                if gz is not None and root in gz.parents and gz.is_file():
                    # 只在预压缩文件不比源文件旧时才用它。
                    # 如果只更新了源文件而 .gz 还是旧的（部分覆盖上传很常见），
                    # 继续发 .gz 会导致「新页面 + 旧脚本」，界面直接错乱。
                    # 这里检测到 .gz 过期就回退，让普通静态处理发源文件。
                    try:
                        _src = (root / rel).resolve()
                        if (_src.is_file()
                                and gz.stat().st_mtime < _src.stat().st_mtime - 1):
                            logging.warning(
                                "预压缩文件过期，已回退到源文件：%s", rel)
                            return await call_next(request)
                    except Exception:
                        logging.exception("检查预压缩文件新鲜度失败：%s", rel)
                    try:
                        data = gz.read_bytes()
                        low = rel.lower()
                        if low.endswith((".js", ".css")):
                            # 同样：vendor 长缓存，自己的资源重验证
                            if "/vendor/" in ("/" + low):
                                cc = "public, max-age=2592000, immutable"
                            else:
                                cc = "no-cache, must-revalidate"
                        elif low.endswith(".html"):
                            cc = "no-cache, must-revalidate"
                        else:
                            cc = "public, max-age=604800"
                        return Response(
                            content=data,
                            media_type=_guess_type(rel),
                            headers={
                                "Content-Encoding": "gzip",
                                "Cache-Control": cc,
                                "Vary": "Accept-Encoding",
                            },
                        )
                    except Exception:
                        logging.exception("发送预压缩资源失败：%s", rel)
    return await call_next(request)


@app.middleware("http")
async def access_log_middleware(request, call_next):
    """记录每次请求：IP、方法、路径、状态码、响应大小、耗时。

    放在 gzip 中间件之后注册 => 位于最外层，统计到的是完整耗时与压缩后大小。
    """
    if not ACCESS_LOG_ENABLED:
        return await call_next(request)

    start = time.time()
    response = None
    try:
        response = await call_next(request)
        return response
    finally:
        try:
            dur_ms = (time.time() - start) * 1000.0
            ip = stats_mod.client_ip_from_headers(
                request.headers,
                (request.client.host if request.client else "") or "-",
            )
            status = getattr(response, "status_code", 0) or 0
            size = _resp_size(response) if response is not None else -1
            ua = (request.headers.get("user-agent") or "-")[:90]
            query = request.url.query
            path = request.url.path + (("?" + query) if query else "")
            _get_access_logger().info(
                '%-15s  %-6s %-46s  %3s  %8s  %7.1fms  %s',
                ip, request.method, path[:46], status,
                _human_size(size), dur_ms, ua,
            )
        except Exception:
            # 记日志失败绝不能影响正常请求
            pass


def _guess_type(name: str) -> str:
    low = name.lower()
    if low.endswith(".js"):
        return "application/javascript; charset=utf-8"
    if low.endswith(".css"):
        return "text/css; charset=utf-8"
    if low.endswith(".html"):
        return "text/html; charset=utf-8"
    if low.endswith(".svg"):
        return "image/svg+xml"
    if low.endswith(".json"):
        return "application/json; charset=utf-8"
    return "application/octet-stream"


app.mount(
    "/static",
    NoCacheStatic(
        directory=STATIC_DIR,
    ),
    name="static",
)


# ============================================================
# 请求模型
# ============================================================

class ConfigBody(BaseModel):
    config: dict


class RunBody(BaseModel):
    dry: bool = False


class AccountBody(BaseModel):
    account: str


class LoginBody(BaseModel):
    username: str
    password: str


class RegisterBody(BaseModel):
    username: str
    password: str
    invite_key: str
    email: str
    qq: str | None = None
    phone: str | None = None


class ChangePasswordBody(BaseModel):
    old_password: str
    new_password: str


class UpdateEmailBody(BaseModel):
    email: str = ""


class AdminCreateUserBody(BaseModel):
    username: str
    password: str
    role: str = "user"
    email: str = ""


class InviteKeyBody(BaseModel):
    note: str = ""


class AdminResetPwdBody(BaseModel):
    username: str
    new_password: str


class AdminBindAccountsBody(BaseModel):
    username: str
    accounts: list[str] = []


class EmailConfigBody(BaseModel):
    enabled: bool | None = None
    smtp_host: str | None = None
    smtp_port: int | None = None
    use_ssl: bool | None = None
    username: str | None = None
    password: str | None = None
    from_addr: str | None = None
    to_addrs: list[str] | str | None = None
    daily_log_time: str | None = None
    notify: dict | None = None
    auth_expiry_remind_days: int | None = None


class SendLogEmailBody(BaseModel):
    account: str | None = None
    date: str | None = None
    to_addrs: list[str] | str | None = None


# ============================================================
# 首页
# ============================================================

def _entry_for(page: str) -> Path:
    """按页面挑选入口 HTML；缺失时回退到 index.html。"""
    f = STATIC_DIR / page
    if f.is_file():
        return f
    return STATIC_DIR / "index.html"


def _serve_spa(request: Request, record_visit: bool = False, entry: str = "index.html"):
    """统一返回前端页面。

    多入口：不同路径返回不同 HTML 文件（便于分别缓存与保护源码），
    但它们共用同一套压缩混淆后的 app.min.js。
    """
    if record_visit:
        try:
            stats_mod.bump_visit()
            # 统一走 client_ip_from_headers：它按 CF → True-Client-IP → X-Real-IP
            # → X-Forwarded-For → 直连地址 的顺序取，反代场景下才不会拿到内网 IP。
            ip = stats_mod.client_ip_from_headers(
                request.headers,
                (request.client.host if request.client else "") or "unknown",
            )
            ua = request.headers.get("User-Agent") or ""
            stats_mod.record_visit_ip(ip, ua)
        except Exception:
            logging.exception("记录访问量失败")

    resp = FileResponse(_entry_for(entry))
    # 入口 HTML 不缓存，保证发版后立刻生效
    resp.headers["Cache-Control"] = "no-cache, must-revalidate"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "SAMEORIGIN"
    resp.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    return resp


@app.get("/")
def index(request: Request):
    """宣传落地页"""
    return _serve_spa(request, record_visit=True)


@app.get("/login")
def page_login(request: Request):
    return _serve_spa(request, entry="login.html")


@app.get("/register")
def page_register(request: Request):
    return _serve_spa(request, entry="register.html")


@app.get("/forgot")
def page_forgot(request: Request):
    """忘记密码：独立页面。"""
    return _serve_spa(request, entry="forgot.html")


@app.get("/about")
def page_about(request: Request):
    """关于我们。"""
    return _serve_spa(request)


@app.get("/terms")
def page_terms(request: Request):
    """免责声明。"""
    return _serve_spa(request)


@app.get("/compliance")
def page_compliance(request: Request):
    """合规性声明。"""
    return _serve_spa(request)


@app.get("/privacy")
def page_privacy(request: Request):
    """隐私政策。"""
    return _serve_spa(request)


@app.get("/help")
def page_help(request: Request):
    """使用帮助（独立整页）。"""
    return _serve_spa(request)


@app.get("/home")
def page_home(request: Request):
    return _serve_spa(request, entry="app.html")


@app.get("/admin")
def page_admin(request: Request):
    return _serve_spa(request, entry="admin.html")


# ============================================================
# 登录 / 登出 / 修改密码
# ============================================================

@app.post("/api/auth/login")
def api_auth_login(body: LoginBody, request: Request):
    username = (body.username or "").strip()
    password = body.password or ""
    if not username or not password:
        raise HTTPException(
            status_code=400,
            detail="请输入账号和密码",
        )
    if not auth_mod.verify_login(username, password):
        raise HTTPException(
            status_code=401,
            detail="账号或密码错误",
        )
    token = auth_mod.create_session(username)
    try:
        ip = (
            request.headers.get("CF-Connecting-IP")
            or request.headers.get("X-Real-IP")
            or (request.headers.get("X-Forwarded-For") or "").split(",")[0].strip()
            or (request.client.host if request.client else "")
        )
        stats_mod.record_visit_ip(ip, request.headers.get("User-Agent") or "", username=username)
    except Exception:
        logging.exception("记录登录 IP 失败")

    # 记录最近一次登录时间与 IP（个人中心展示）
    try:
        _ip = (
            request.headers.get("CF-Connecting-IP")
            or request.headers.get("X-Real-IP")
            or (request.headers.get("X-Forwarded-For") or "").split(",")[0].strip()
            or (request.client.host if request.client else "")
        )
        auth_mod.record_login(username, _ip)
    except Exception:
        logging.exception("记录最近登录信息失败")
    user = auth_mod.get_user(username) or {}
    return {
        "ok": True,
        "token": token,
        "username": username,
        "role": user.get("role") or "user",
        "bound_account": user.get("bound_account"),
        "email": user.get("email") or "",
    }



class ForgotPasswordBody(BaseModel):
    email: str
    new_password: str
    username: str | None = None
    code: str = ""


class ForgotCodeBody(BaseModel):
    email: str


@app.post("/api/auth/forgot-code")
def api_forgot_code(body: ForgotCodeBody):
    """发送密码重置验证码到注册邮箱（需管理员已配置 SMTP）。"""
    try:
        auth_mod.create_reset_code((body.email or "").strip())
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {
        "ok": True,
        "message": "验证码已发送，请查收邮箱（10 分钟内有效）",
    }


@app.post("/api/auth/forgot-password")
def api_forgot_password(body: ForgotPasswordBody):
    """登录页忘记密码：邮箱 + 邮件验证码 + 新密码（可选账号）。

    v1.6.5 起必须携带邮箱验证码；此前仅凭「邮箱 + 新密码」即可重置，
    知道邮箱就能接管任意账号。
    """
    try:
        auth_mod.reset_password_by_email(
            email=(body.email or "").strip(),
            new_password=body.new_password or "",
            username=(body.username or "").strip() or None,
            code=(body.code or "").strip(),
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"ok": True, "message": "密码已重置，请使用新密码登录"}

@app.post("/api/auth/register")
def api_auth_register(body: RegisterBody):
    try:
        info = auth_mod.register_with_key(
            body.username,
            body.password,
            body.invite_key,
            email=(body.email or "").strip(),
            qq=(body.qq or "").strip(),
            phone=(body.phone or "").strip(),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    username = info["username"]
    days = int(info.get("default_auth_days") or 0)

    # ========================================================
    # 注册后自动创建账号（目录名 = 用户名），并发放默认授权天数
    # 这样后台不会出现乱七八糟的账号名，用户首次进入即可用
    # ========================================================
    created_account = None
    try:
        acct_name = username
        if acct_name not in list_accounts():
            create_account(acct_name)
        else:
            # 目录已存在：不能直接抢占。
            # 否则攻击者只要用「已存在的账号名」注册，就能绑到别人的
            # 账号目录，读取对方的 state.json（抖音登录态）并以其身份发消息。
            # owner 缺失/为空的旧目录同样拒绝——这类目录可能由管理员手工
            # 建立并已上传登录态，不能因为读不到 owner 就放行认领。
            try:
                _owner = (load_profile(acct_name).get("owner") or "").strip()
            except Exception:
                _owner = ""
            if _owner != username:
                raise ValueError(
                    "该用户名对应的账号已被占用，请换一个用户名注册"
                )

        # 归属人
        try:
            prof = load_profile(acct_name)
            prof["owner"] = username
            save_profile(acct_name, prof)
        except Exception:
            pass

        # 绑定
        try:
            auth_mod.bind_account(username, acct_name)
        except Exception:
            pass

        # 发放注册赠送的授权天数（写入独立授权文件）
        if days > 0:
            try:
                add_authorized_days(acct_name, days)
            except Exception:
                logging.exception("注册送授权失败：%s", acct_name)

        # 应用定时与状态
        try:
            scheduler.apply_schedule(acct_name)
            sync_mode_status(acct_name)
        except Exception:
            pass

        created_account = acct_name
    except Exception:
        logging.exception("注册后自动创建账号失败：%s", username)

    token = auth_mod.create_session(username)
    return {
        "ok": True,
        "token": token,
        "username": username,
        "role": info["role"],
        "bound_account": created_account,
        "account": created_account,
        "default_auth_days": days,
        "message": "注册成功，已为你创建账号并发放授权",
    }


@app.post("/api/auth/logout")
def api_auth_logout(
    token: str = Header(
        default="",
        alias="X-Auth-Token",
    ),
):
    auth_mod.revoke_session(token)
    return {"ok": True}


@app.get("/api/auth/me")
def api_auth_me(
    token: str = Header(
        default="",
        alias="X-Auth-Token",
    ),
):
    _check_auth(token)
    username = _current_username(token)
    user = auth_mod.get_user(username) or {}
    bound = auth_mod.accounts_owned_by(username)
    return {
        "ok": True,
        "username": username,
        "role": user.get("role") or ("admin" if auth_mod.is_admin(username) else "user"),
        "bound_account": user.get("bound_account"),
        "bound_accounts": bound,
        "email": user.get("email") or "",
        "created_at": user.get("created_at"),
        "updated_at": user.get("updated_at"),
        "last_login_at": user.get("last_login_at"),
        "last_login_ip": user.get("last_login_ip") or "",
    }


class ChangeUsernameBody(BaseModel):
    new_username: str
    password: str = ""


@app.post("/api/auth/change-username")
def api_auth_change_username(
    body: ChangeUsernameBody,
    token: str = Header(default="", alias="X-Auth-Token"),
):
    """用户自己修改登录用户名；改完仍保持登录。"""
    _check_auth(token)
    username = _current_username(token)

    # 若填了密码则校验，避免会话被他人劫持后随意改名
    if (body.password or "").strip():
        if not auth_mod.verify_login(username, body.password):
            raise HTTPException(status_code=400, detail="密码不正确")

    try:
        info = auth_mod.change_username(username, body.new_username)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    old_name = info["old_username"]
    new_name = info["username"]

    # ========================================================
    # 账号目录跟随用户名改动（约定：普通用户的账号名 = 用户名）
    # ========================================================
    account_renamed = None
    try:
        accounts = list_accounts()
        if old_name in accounts and new_name not in accounts:
            try:
                scheduler.remove_schedule(old_name)
            except Exception:
                pass
            rename_account(old_name, new_name)
            account_renamed = new_name

            # 改完目录后重新绑定
            try:
                auth_mod.set_bound_accounts(new_name, [new_name])
            except Exception:
                pass

            try:
                scheduler.apply_schedule(new_name)
                sync_mode_status(new_name)
            except Exception:
                logging.exception("改名后重建调度失败：%s", new_name)

            logging.info("用户名 %s -> %s，账号目录已同步重命名", old_name, new_name)
    except Exception:
        logging.exception("用户名 %s 改名后同步账号目录失败", old_name)

    return {
        "ok": True,
        "username": new_name,
        "old_username": old_name,
        "account_renamed": account_renamed,
        "message": "用户名已修改" + ("，账号已同步改名" if account_renamed else ""),
    }


@app.put("/api/auth/email")
def api_auth_update_email(
    body: UpdateEmailBody,
    token: str = Header(default="", alias="X-Auth-Token"),
):
    """用户自己填写/修改收件邮箱。"""
    _check_auth(token)
    username = _current_username(token)
    try:
        email = auth_mod.update_user_email(username, body.email or "")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"ok": True, "email": email}


@app.post("/api/auth/change-password")
def api_auth_change_password(
    body: ChangePasswordBody,
    token: str = Header(
        default="",
        alias="X-Auth-Token",
    ),
):
    _check_auth(token)
    username = auth_mod.validate_session(token)
    if not username:
        # 兼容 AUTH_TOKEN 登录时也可改密
        if AUTH_TOKEN and token == AUTH_TOKEN:
            username = auth_mod.get_admin_username()
        else:
            raise HTTPException(
                status_code=401,
                detail="登录已过期，请重新登录",
            )
    try:
        auth_mod.change_password(
            username,
            body.old_password or "",
            body.new_password or "",
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail=str(exc),
        )
    return {
        "ok": True,
        "message": "密码已修改，请重新登录",
    }


# ============================================================
# 管理员：用户与注册密钥
# ============================================================

@app.get("/api/admin/users")
def api_admin_users(
    token: str = Header(default="", alias="X-Auth-Token"),
):
    _require_admin(token)
    return {"ok": True, "users": auth_mod.list_users_for_admin()}


@app.post("/api/admin/users")
def api_admin_create_user(
    body: AdminCreateUserBody,
    token: str = Header(default="", alias="X-Auth-Token"),
):
    _require_admin(token)
    try:
        info = auth_mod.admin_create_user(
            body.username,
            body.password,
            body.role or "user",
            email=getattr(body, "email", "") or "",
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"ok": True, **info}


@app.delete("/api/admin/users/{username}")
def api_admin_delete_user(
    username: str,
    token: str = Header(default="", alias="X-Auth-Token"),
):
    admin_name = _require_admin(token)
    if username == admin_name:
        raise HTTPException(status_code=400, detail="不能删除当前登录的管理员")
    try:
        auth_mod.delete_user(username)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"ok": True}


@app.post("/api/admin/users/reset-password")
def api_admin_reset_password(
    body: AdminResetPwdBody,
    token: str = Header(default="", alias="X-Auth-Token"),
):
    _require_admin(token)
    try:
        auth_mod.admin_set_password(body.username, body.new_password)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"ok": True, "message": "密码已重置"}


@app.put("/api/admin/users/email")
def api_admin_set_user_email(
    body: dict,
    token: str = Header(default="", alias="X-Auth-Token"),
):
    """管理员设置用户收件邮箱。"""
    _require_admin(token)
    username = str((body or {}).get("username") or "").strip()
    email = str((body or {}).get("email") or "").strip()
    if not username:
        raise HTTPException(status_code=400, detail="缺少用户名")
    try:
        saved = auth_mod.update_user_email(username, email)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"ok": True, "username": username, "email": saved}


@app.put("/api/admin/users/bind-accounts")
def api_admin_bind_accounts(
    body: AdminBindAccountsBody,
    token: str = Header(default="", alias="X-Auth-Token"),
):
    """管理员设置用户绑定的抖音账号列表。"""
    _require_admin(token)
    all_acc = set(list_accounts())
    accounts = []
    for a in body.accounts or []:
        name = str(a or "").strip()
        if not name:
            continue
        if name not in all_acc:
            raise HTTPException(status_code=400, detail=f"抖音账号不存在: {name}")
        if name not in accounts:
            accounts.append(name)
    try:
        saved = auth_mod.set_bound_accounts(body.username, accounts)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"ok": True, "username": body.username, "bound_accounts": saved}




@app.get("/api/admin/access-log")
def api_admin_access_log(
    lines: int = 200,
    token: str = Header(default="", alias="X-Auth-Token"),
):
    """读取最近的访问日志（仅管理员）。

    返回：日志行、文件大小、轮转文件列表。
    """
    _require_admin(token)
    try:
        n = int(lines)
    except (TypeError, ValueError):
        n = 200
    n = max(1, min(n, 2000))

    from datetime import datetime as _dt

    files = []
    try:
        if LOG_DIR.exists():
            for f in sorted(LOG_DIR.glob("access.log*")):
                try:
                    files.append({
                        "name": f.name,
                        "size": f.stat().st_size,
                        "size_text": _human_size(f.stat().st_size),
                        "modified": _dt.fromtimestamp(
                            f.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
                    })
                except Exception:
                    continue
    except Exception:
        logging.exception("读取访问日志文件列表失败")

    tail = []
    try:
        if ACCESS_LOG_FILE.exists():
            # 只读尾部 n 行，避免大文件把内存吃满
            with open(ACCESS_LOG_FILE, "rb") as fh:
                fh.seek(0, os.SEEK_END)
                total = fh.tell()
                chunk = min(total, max(64 * 1024, n * 220))
                fh.seek(total - chunk)
                raw = fh.read()
            text = raw.decode("utf-8", errors="replace")
            tail = text.splitlines()[-n:]
    except Exception:
        logging.exception("读取访问日志失败")

    total_size = sum(f["size"] for f in files)
    return {
        "ok": True,
        "enabled": ACCESS_LOG_ENABLED,
        "lines": tail,
        "files": files,
        "total_size": total_size,
        "total_size_text": _human_size(total_size),
        "max_bytes": ACCESS_LOG_MAX_BYTES,
        "max_bytes_text": _human_size(ACCESS_LOG_MAX_BYTES),
        "backups": ACCESS_LOG_BACKUPS,
    }


# ============================================================
# 公告模板库（管理员一键套用，省得自己想文案和配色）
# ============================================================

ANNOUNCE_TEMPLATES = [
    # ---------------- 通知类 ----------------
    {
        "id": "update", "name": "版本更新", "category": "通知", "level": "info",
        "desc": "发布新功能时用，蓝色信息风格",
        "title": "版本更新通知",
        "content": "本次更新内容：\n\n1. 新增批量生成注册码\n2. 优化发送稳定性\n3. 修复若干已知问题\n\n如有疑问请联系客服。",
        "accent_color": "#2563eb", "bg_color": "#f0f7ff",
        "text_color": "#334155", "title_color": "#1e40af",
    },
    {
        "id": "maintenance", "name": "维护通知", "category": "通知", "level": "warning",
        "desc": "计划维护/停机，橙色提醒风格",
        "title": "系统维护通知",
        "content": "系统将于近期进行维护升级，期间可能短暂无法访问。\n\n请提前安排，避免影响使用。维护完成后会自动恢复。",
        "accent_color": "#f59e0b", "bg_color": "#fffbeb",
        "text_color": "#78350f", "title_color": "#b45309",
    },
    {
        "id": "pause", "name": "暂停服务", "category": "通知", "level": "error",
        "desc": "临时停服/故障公告，红色风格",
        "title": "服务临时不可用",
        "content": "因服务器故障，服务暂时不可用。\n\n我们正在加急修复，预计尽快恢复。给你带来不便深表歉意。",
        "accent_color": "#dc2626", "bg_color": "#fef2f2",
        "text_color": "#7f1d1d", "title_color": "#b91c1c",
    },
    {
        "id": "recovered", "name": "恢复公告", "category": "通知", "level": "success",
        "desc": "故障恢复后的安抚公告，绿色风格",
        "title": "服务已恢复",
        "content": "系统故障已修复，服务已恢复正常。\n\n本轮停机期间未执行的发送任务会在下个定时点自动继续，无需手动处理。",
        "accent_color": "#16a34a", "bg_color": "#f0fdf4",
        "text_color": "#14532d", "title_color": "#15803d",
    },
    {
        "id": "safe", "name": "安全提醒", "category": "通知", "level": "warning",
        "desc": "提醒用户保护账号安全，琥珀风格",
        "title": "账号安全提醒",
        "content": "请勿将登录态文件（state.json）或账号密码分享给任何人。\n\n登录态等同于账号本身，泄露后可能被他人顶号。建议定期修改密码。",
        "accent_color": "#d97706", "bg_color": "#fffbeb",
        "text_color": "#78350f", "title_color": "#b45309",
    },
    # ---------------- 运营类 ----------------
    {
        "id": "welcome", "name": "新用户引导", "category": "运营", "level": "success",
        "desc": "面向新注册用户，绿色成功风格",
        "title": "欢迎使用云逸续火花助手",
        "content": "三步开始使用：\n\n1. 「凭证」里扫码登录抖音\n2. 「好友与消息」勾选要续火花的好友\n3. 「定时设置」设好每日发送时间\n\n剩下的交给系统，每天自动帮你发。",
        "accent_color": "#16a34a", "bg_color": "#f0fdf4",
        "text_color": "#14532d", "title_color": "#15803d",
    },
    {
        "id": "expire", "name": "授权到期", "category": "运营", "level": "error",
        "desc": "提醒用户续费，红色警示风格",
        "title": "授权即将到期",
        "content": "你的授权即将到期，到期后自动发送会停止。\n\n请及时前往「概览 → 使用卡密」续期，避免火花中断。",
        "accent_color": "#dc2626", "bg_color": "#fef2f2",
        "text_color": "#7f1d1d", "title_color": "#b91c1c",
    },
    {
        "id": "promo", "name": "限时优惠", "category": "运营", "level": "warning",
        "desc": "卡密折扣/充值活动，暖橙促销风格",
        "title": "限时优惠活动",
        "content": "近期充值享优惠：\n\n· 90 天卡密 9 折\n· 365 天卡密 8 折\n\n活动时间有限，请联系客服获取优惠卡密。",
        "accent_color": "#f97316", "bg_color": "#fff7ed",
        "text_color": "#7c2d12", "title_color": "#c2410c",
    },
    {
        "id": "invite", "name": "邀请有礼", "category": "运营", "level": "success",
        "desc": "邀请新用户奖励活动，青绿风格",
        "title": "邀请好友得天数",
        "content": "邀请好友注册并完成首次登录，你和好友各获 3 天授权。\n\n请联系客服登记邀请关系后发放。",
        "accent_color": "#0d9488", "bg_color": "#f0fdfa",
        "text_color": "#134e4a", "title_color": "#0f766e",
    },
    {
        "id": "feature", "name": "功能推荐", "category": "运营", "level": "info",
        "desc": "推广 AI 文案等新功能，青色风格",
        "title": "试试 AI 智能文案",
        "content": "开启 AI 文案后，系统会按好友昵称自动生成自然的问候语，比固定模板更像真人。\n\n在「定时设置 → AI 文案」里配置即可，兼容 OpenAI 接口。",
        "accent_color": "#0891b2", "bg_color": "#ecfeff",
        "text_color": "#164e63", "title_color": "#0e7490",
    },
    {
        "id": "feedback", "name": "反馈征集", "category": "运营", "level": "info",
        "desc": "收集用户建议，靛蓝风格",
        "title": "意见反馈征集",
        "content": "你希望增加什么功能？对现有功能有什么建议？\n\n欢迎通过「联系客服」告诉我们，优质建议被采纳后将赠送授权天数。",
        "accent_color": "#4f46e5", "bg_color": "#eef2ff",
        "text_color": "#312e81", "title_color": "#4338ca",
    },
    # ---------------- 节日类 ----------------
    {
        "id": "holiday", "name": "节日祝福", "category": "节日", "level": "info",
        "desc": "通用节日问候，紫色柔和风格",
        "title": "节日快乐",
        "content": "祝大家节日快乐，假期期间系统照常运行。\n\n如需调整发送时间，可在「定时设置」里修改。",
        "accent_color": "#7c3aed", "bg_color": "#faf5ff",
        "text_color": "#4c1d95", "title_color": "#6d28d9",
    },
    {
        "id": "newyear", "name": "新年快乐", "category": "节日", "level": "success",
        "desc": "元旦/春节通用，红金喜庆风格",
        "title": "新年快乐 🎉",
        "content": "新的一年，愿你的火花天天延续、友谊长长久久。\n\n假期期间系统照常运行，祝大家新年愉快！",
        "accent_color": "#e11d48", "bg_color": "#fff1f2",
        "text_color": "#881337", "title_color": "#be123c",
    },
    {
        "id": "midautumn", "name": "中秋祝福", "category": "节日", "level": "info",
        "desc": "中秋节问候，月白金风格",
        "title": "中秋团圆",
        "content": "花好月圆人团圆。\n\n中秋假期系统照常运行，记得抬头看看月亮，也记得让火花继续燃烧。",
        "accent_color": "#b45309", "bg_color": "#fffbeb",
        "text_color": "#78350f", "title_color": "#92400e",
    },
    {
        "id": "national", "name": "国庆祝福", "category": "节日", "level": "success",
        "desc": "国庆假期通知，红金风格",
        "title": "国庆快乐",
        "content": "祝祖国繁荣昌盛，祝大家假期愉快！\n\n假期期间系统照常运行；出行途中注意网络环境变化，登录态尽量在常用网络获取。",
        "accent_color": "#dc2626", "bg_color": "#fef2f2",
        "text_color": "#7f1d1d", "title_color": "#b91c1c",
    },
    # ---------------- 使用技巧 ----------------
    {
        "id": "risk", "name": "风控提醒", "category": "技巧", "level": "warning",
        "desc": "账号异常/需注意，橙色警示风格",
        "title": "账号状态提醒",
        "content": "检测到部分账号登录态异常，自动发送已暂停。\n\n请前往「凭证」重新扫码登录。建议保持每日发送间隔不要太短，降低风控概率。",
        "accent_color": "#ea580c", "bg_color": "#fff7ed",
        "text_color": "#7c2d12", "title_color": "#c2410c",
    },
    {
        "id": "tips", "name": "养号技巧", "category": "技巧", "level": "info",
        "desc": "降低风控的使用建议，蓝色风格",
        "title": "降低风控小技巧",
        "content": "1. 登录态尽量在常用网络环境获取\n2. 好友数量控制在少量，间隔不要太短\n3. 出现验证码立即停止，次日再试\n4. 消息内容多样化，配合 AI 文案更自然",
        "accent_color": "#2563eb", "bg_color": "#f0f7ff",
        "text_color": "#334155", "title_color": "#1e40af",
    },
    {
        "id": "backup", "name": "备份提醒", "category": "技巧", "level": "info",
        "desc": "提醒备份登录态与配置，石板灰风格",
        "title": "记得备份登录态",
        "content": "建议定期在常用电脑执行 extract_cookie.py 导出最新的 state.json 备份。\n\n服务器数据丢失时可以快速恢复，不用重新扫码。",
        "accent_color": "#475569", "bg_color": "#f8fafc",
        "text_color": "#1e293b", "title_color": "#334155",
    },
    {
        "id": "privacy", "name": "隐私声明", "category": "技巧", "level": "info",
        "desc": "数据与隐私说明，深灰风格",
        "title": "数据与隐私说明",
        "content": "本系统所有数据（含登录态）仅保存在你自己的服务器上，不会上传到任何第三方。\n\n请妥善保管服务器与 data 目录的访问权限。",
        "accent_color": "#334155", "bg_color": "#f1f5f9",
        "text_color": "#0f172a", "title_color": "#1e293b",
    },
    # ---------------- 基础 ----------------
    {
        "id": "plain", "name": "简洁纯文字", "category": "基础", "level": "info",
        "desc": "无配色，跟随系统默认样式",
        "title": "公告",
        "content": "在此填写公告内容。",
        "accent_color": "", "bg_color": "",
        "text_color": "", "title_color": "",
    },
]


@app.get("/api/admin/announce-templates")
def api_admin_announce_templates(
    token: str = Header(default="", alias="X-Auth-Token"),
):
    """返回公告模板列表（仅管理员）。"""
    _require_admin(token)
    return {"ok": True, "templates": ANNOUNCE_TEMPLATES}


# ============================================================
# 管理看板增强：批量校正 / 清理 / 导出
# ============================================================


@app.post("/api/admin/visit-ips/refresh-all-unknown")
def api_admin_refresh_all_unknown(
    token: str = Header(default="", alias="X-Auth-Token"),
):
    """一键把所有查询失败的 IP 重新查一遍归属地。

    只处理 region 为空或「未知」的记录，避免无谓的第三方请求。
    """
    _require_admin(token)

    items = stats_mod.list_ip_records()
    targets = []
    seen = set()
    for it in items:
        ip = (it or {}).get("ip") or ""
        r = (it or {}).get("region") or ""
        if ip and (not r or r == "未知") and ip not in seen:
            # 内网地址不用查
            if stats_mod._is_private_ip(ip):
                continue
            seen.add(ip)
            targets.append(ip)

    results = []
    fixed = 0
    for ip in targets[:120]:          # 单次上限，避免请求过多
        try:
            region = stats_mod.resolve_ip_region(ip, force=True)
        except Exception:
            region = "未知"
        if region and region != "未知":
            fixed += 1
            try:
                stats_mod.update_ip_region(ip, region)
            except Exception:
                logging.exception("写回归属地失败：%s", ip)
        results.append({"ip": ip, "region": region})

    return {
        "ok": True,
        "checked": len(results),
        "fixed": fixed,
        "results": results,
    }


@app.post("/api/admin/visit-ips/cleanup")
def api_admin_visit_ips_cleanup(
    body: dict,
    token: str = Header(default="", alias="X-Auth-Token"),
):
    """清理访问 IP 记录里「未知」或内网的条目，减少列表噪音。"""
    _require_admin(token)
    mode = str((body or {}).get("mode") or "unknown")
    before = stats_mod.list_ip_records()

    kept = []
    for it in before:
        ip = (it or {}).get("ip") or ""
        r = (it or {}).get("region") or ""
        drop = False
        if mode == "unknown":
            drop = (not r) or r == "未知"
        elif mode == "private":
            drop = stats_mod._is_private_ip(ip)
        elif mode == "both":
            drop = ((not r) or r == "未知") or stats_mod._is_private_ip(ip)
        if not drop:
            kept.append(it)

    removed = len(before) - len(kept)
    if removed:
        stats_mod.save_ip_records(kept)
    return {"ok": True, "removed": removed, "left": len(kept), "mode": mode}


@app.get("/api/admin/visit-ips/export")
def api_admin_visit_ips_export(
    token: str = Header(default="", alias="X-Auth-Token"),
):
    """导出访问 IP 记录为 CSV 文本。"""
    _require_admin(token)
    rows = [["IP", "归属地", "用户", "访问时间", "User-Agent"]]
    for it in stats_mod.list_ip_records():
        it = it or {}
        rows.append([
            str(it.get("ip") or ""),
            str(it.get("region") or ""),
            str(it.get("username") or ""),
            str(it.get("time") or ""),
            str(it.get("user_agent") or "").replace('"', "'"),
        ])
    lines = []
    for r in rows:
        lines.append(",".join('"%s"' % str(c).replace('"', '""') for c in r))
    return {"ok": True, "csv": "\ufeff" + "\n".join(lines)}


# ============================================================
# 卡密查询（公开页面 /kami 用）
# ============================================================

_KAMI_QUERY_HITS: dict[str, list] = {}
_KAMI_QUERY_LOCK = threading.Lock()
_KAMI_QUERY_MAX_PER_HOUR = 40


def _kami_rate_ok(ip: str) -> bool:
    """简单限流：同一 IP 每小时最多查 40 次。"""
    import time as _t
    now = _t.time()
    with _KAMI_QUERY_LOCK:
        hits = [x for x in _KAMI_QUERY_HITS.get(ip, []) if now - x < 3600]
        if len(hits) >= _KAMI_QUERY_MAX_PER_HOUR:
            _KAMI_QUERY_HITS[ip] = hits
            return False
        hits.append(now)
        _KAMI_QUERY_HITS[ip] = hits
        # 顺带清理过期条目，避免无限增长
        if len(_KAMI_QUERY_HITS) > 500:
            for k in list(_KAMI_QUERY_HITS.keys()):
                if not [x for x in _KAMI_QUERY_HITS[k] if now - x < 3600]:
                    _KAMI_QUERY_HITS.pop(k, None)
        return True


@app.post("/api/kami/query")
def api_kami_query(body: dict, request: Request):
    """查询卡密状态（公开，带限流）。

    只返回「是否已使用 / 面额 / 使用时间」等非敏感信息，
    不返回使用者账号，避免被人扫库。
    """
    ip = stats_mod.client_ip_from_headers(
        request.headers,
        (request.client.host if request.client else "") or "unknown",
    )
    if not _kami_rate_ok(ip):
        raise HTTPException(status_code=429, detail="查询过于频繁，请稍后再试")

    key = str((body or {}).get("key") or "").strip().upper()
    if not key:
        raise HTTPException(status_code=400, detail="请输入卡密")
    if len(key) > 64:
        raise HTTPException(status_code=400, detail="卡密格式不正确")

    try:
        info = card_keys.query_card_key(key)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception:
        logging.exception("查询卡密失败")
        raise HTTPException(status_code=500, detail="查询失败，请稍后再试")

    if not info or not info.get("found"):
        return {"ok": True, "found": False, "message": "未找到该卡密，请检查是否输入有误"}

    return {
        "ok": True,
        "found": True,
        "used": bool(info.get("used")),
        "days": info.get("days") or 0,
        "used_at": info.get("used_at") or "",
        "created_at": info.get("created_at") or "",
        "note": info.get("note") or "",
    }


@app.get("/api/admin/access-log/stats")
def api_admin_access_log_stats(
    token: str = Header(default="", alias="X-Auth-Token"),
):
    """统计访问日志：按 IP 和状态码聚合，方便一眼看出异常。"""
    _require_admin(token)

    from collections import Counter
    ip_counter = Counter()
    status_counter = Counter()
    path_counter = Counter()
    total = 0

    try:
        if ACCESS_LOG_FILE.exists():
            with open(ACCESS_LOG_FILE, "rb") as fh:
                fh.seek(0, os.SEEK_END)
                size = fh.tell()
                fh.seek(max(0, size - 2 * 1024 * 1024))   # 只看最后 2MB
                raw = fh.read().decode("utf-8", errors="replace")
            lines = raw.splitlines()
            if lines and not lines[0].startswith("20"):
                lines = lines[1:]
            for ln in lines:
                # 格式：时间 IP 方法 路径 状态 ...
                parts = ln.split()
                if len(parts) < 5:
                    continue
                total += 1
                ip_counter[parts[2]] += 1
                # 找纯数字状态码
                for p in parts[3:8]:
                    if p.isdigit() and len(p) == 3:
                        status_counter[p] += 1
                        break
                path_counter[parts[4][:60]] += 1
    except Exception:
        logging.exception("统计访问日志失败")

    return {
        "ok": True,
        "total": total,
        "top_ips": [{"ip": k, "count": v} for k, v in ip_counter.most_common(20)],
        "status": [{"code": k, "count": v} for k, v in status_counter.most_common(10)],
        "top_paths": [{"path": k, "count": v} for k, v in path_counter.most_common(15)],
    }


@app.get("/api/admin/global-stats")
def api_admin_global_stats(
    token: str = Header(default="", alias="X-Auth-Token"),
):
    """管理员：全局数据统计"""
    _require_admin(token)
    return {"ok": True, "stats": stats_mod.collect_global_stats()}


@app.get("/api/stats/visits")
def api_get_visits():
    """公开：读取当前访问量（用于顶栏展示）"""
    return {"ok": True, "visits": stats_mod.get_visits()}


@app.post("/api/stats/visit")
def api_stats_visit():
    """公开：记录一次访问。
    Cloudflare 若缓存了 HTML，源站 / 可能不触发，由前端 POST 兜底。
    同一浏览器标签页内用 session 去重由前端控制。
    """
    try:
        n = stats_mod.bump_visit()
    except Exception as exc:
        logging.exception("bump_visit 失败")
        raise HTTPException(status_code=500, detail=f"记录访问失败：{exc}")
    return {"ok": True, "visits": n}


@app.get("/api/site/settings")
def api_site_settings_public(
    token: str = Header(None, alias="X-Auth-Token"),
):
    """登录用户获取公告与客服信息"""
    _current_username(token)
    return {"ok": True, **site_settings.public_view()}


@app.get("/api/site/register-help")
def api_site_register_help():
    """注册页专用（无需登录）：注册码获取引导。"""
    return {"ok": True, "help": site_settings.register_help_view()}


@app.get("/api/admin/site-settings")
def api_admin_site_settings_get(
    token: str = Header(None, alias="X-Auth-Token"),
):
    _require_admin(token)
    return {"ok": True, "settings": site_settings.load()}


@app.put("/api/admin/site-settings")
def api_admin_site_settings_put(
    body: dict,
    token: str = Header(None, alias="X-Auth-Token"),
):
    _require_admin(token)
    saved = site_settings.save(body or {})
    return {"ok": True, "settings": saved}


@app.get("/api/admin/email-config")
def api_admin_email_config_get(
    token: str = Header(default="", alias="X-Auth-Token"),
):
    _require_admin(token)
    cfg = email_util.load_email_config()
    safe = dict(cfg)
    safe["password_set"] = bool(cfg.get("password"))
    safe["password"] = ""
    safe.pop("to_addrs", None)
    safe["notify_labels"] = dict(email_util.NOTIFY_TYPES)
    return {"ok": True, "config": safe}


@app.put("/api/admin/email-config")
def api_admin_email_config_put(
    body: EmailConfigBody,
    token: str = Header(default="", alias="X-Auth-Token"),
):
    _require_admin(token)
    data = body.model_dump(exclude_none=True)
    # 空密码表示不修改
    if data.get("password") == "":
        data.pop("password", None)
    cfg = email_util.save_email_config(data)
    try:
        scheduler.apply_daily_digest_schedule(_send_daily_digest_all)
    except Exception:
        logging.exception("更新每日邮件汇总时间失败")
    safe = dict(cfg)
    safe["password_set"] = bool(cfg.get("password"))
    safe["password"] = ""
    # 不再回传 to_addrs
    safe.pop("to_addrs", None)
    return {"ok": True, "config": safe}


@app.post("/api/admin/email-test")
def api_admin_email_test(
    token: str = Header(default="", alias="X-Auth-Token"),
):
    """向发件人邮箱发送测试邮件。"""
    _require_admin(token)
    try:
        to = email_util.send_test_to_sender()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"ok": True, "message": f"测试邮件已发送至 {to}"}


@app.post("/api/logs/email")
def api_logs_email(
    body: SendLogEmailBody,
    token: str = Header(default="", alias="X-Auth-Token"),
):
    """手动发送今日日志到当前用户自己的收件邮箱。"""
    _check_auth(token)
    username = _current_username(token)
    account = (body.account or "").strip()
    if not account:
        raise HTTPException(status_code=400, detail="请指定账号")
    _assert_account_access(token, account)
    user = auth_mod.get_user(username) or {}
    to_email = (user.get("email") or "").strip()
    if not to_email:
        raise HTTPException(status_code=400, detail="请先在「邮箱」中填写收件邮箱")
    from datetime import datetime as _dt
    date = (body.date or "").strip() or _dt.now().strftime("%Y-%m-%d")
    log_text = read_log_file(account, date, 1200)
    rt = load_runtime(account)
    try:
        email_util.send_daily_digest(
            account=account,
            username=username,
            to_email=to_email,
            log_text=log_text,
            runtime=rt,
            douyin_name=(rt.get("douyin_name") or ""),
            douyin_id=(rt.get("douyin_id") or ""),
            date=date,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"ok": True, "message": f"已发送至 {to_email}"}


@app.get("/api/logs/files")
def api_logs_files(
    account: str | None = None,
    token: str = Header(default="", alias="X-Auth-Token"),
):
    _check_auth(token)
    account = _get_account(account, token)
    return {"ok": True, "account": account, "files": list_log_files(account)}


@app.get("/api/logs/by-date")
def api_logs_by_date(
    account: str | None = None,
    date: str | None = None,
    n: int = 500,
    token: str = Header(default="", alias="X-Auth-Token"),
):
    _check_auth(token)
    account = _get_account(account, token)
    text = read_log_file(account, date, n)
    return {"ok": True, "account": account, "date": date, "logs": text}


@app.get("/api/admin/keys")
def api_admin_keys(
    token: str = Header(default="", alias="X-Auth-Token"),
):
    _require_admin(token)
    return {"ok": True, "keys": auth_mod.list_invite_keys()}


@app.post("/api/admin/keys")
def api_admin_create_key(
    body: InviteKeyBody,
    token: str = Header(default="", alias="X-Auth-Token"),
):
    admin_name = _require_admin(token)
    info = auth_mod.create_invite_key(admin_name, body.note or "")
    return {"ok": True, "key": info}


@app.delete("/api/admin/keys/{key}")
def api_admin_delete_key(
    key: str,
    token: str = Header(default="", alias="X-Auth-Token"),
):
    _require_admin(token)
    try:
        auth_mod.delete_invite_key(key)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"ok": True}



def _notify_run_email(account: str, result: dict, force_email: bool = False) -> None:
    """续火花结束/异常后，按用户邮箱发送分类通知（不使用全局默认收件人）。
    force_email=True：手动「立即发送」完成后强制发信（仍需启用 SMTP 且用户有邮箱）。
    """
    try:
        cfg = email_util.load_email_config()
        if not cfg.get("enabled"):
            return
        if result.get("dry_run"):
            return
        from datetime import datetime as _dt
        date = _dt.now().strftime("%Y-%m-%d")
        log_text = read_log_file(account, date, 800)
        rt = load_runtime(account)
        douyin_name = (rt.get("douyin_name") or "").strip()
        douyin_id = (rt.get("douyin_id") or "").strip()
        owners = auth_mod.find_users_for_account(account)
        for u in owners:
            email = (u.get("email") or "").strip()
            if not email:
                continue
            try:
                kind = email_util.send_run_report(
                    account=account,
                    username=u.get("username") or "用户",
                    to_email=email,
                    result=result,
                    log_text=log_text,
                    date=date,
                    douyin_name=douyin_name,
                    douyin_id=douyin_id,
                    force_send=force_email,
                )
                if kind:
                    logging.info(
                        "已向 %s (%s) 发送 %s 邮件",
                        u.get("username"),
                        email,
                        kind,
                    )
            except Exception as exc:
                logging.warning("发送续火花邮件失败 %s: %s", email, exc)
    except Exception as exc:
        logging.warning("续火花邮件通知异常: %s", exc)


def _send_daily_digest_all() -> None:
    """每日汇总：向所有绑定账号且有邮箱的用户发送当日日志。"""
    try:
        cfg = email_util.load_email_config()
        if not email_util.is_notify_enabled("daily_digest", cfg):
            return
        from datetime import datetime as _dt
        date = _dt.now().strftime("%Y-%m-%d")
        for account in list_accounts():
            try:
                log_text = read_log_file(account, date, 1200)
                rt = load_runtime(account)
                douyin_name = (rt.get("douyin_name") or "").strip()
                douyin_id = (rt.get("douyin_id") or "").strip()
                owners = auth_mod.find_users_for_account(account)
                for u in owners:
                    email = (u.get("email") or "").strip()
                    if not email:
                        continue
                    try:
                        email_util.send_daily_digest(
                            account=account,
                            username=u.get("username") or "用户",
                            to_email=email,
                            log_text=log_text,
                            runtime=rt,
                            douyin_name=douyin_name,
                            douyin_id=douyin_id,
                            date=date,
                        )
                        logging.info("每日汇总已发送 %s -> %s", account, email)
                    except Exception as exc:
                        logging.warning("每日汇总发送失败 %s %s: %s", account, email, exc)
            except Exception as exc:
                logging.warning("每日汇总账号 %s 异常: %s", account, exc)
    except Exception as exc:
        logging.exception("每日邮件汇总失败: %s", exc)


# ============================================================
# 账号列表
# ============================================================

@app.get("/api/accounts")
def api_accounts(
    token: str = Header(
        default="",
        alias="X-Auth-Token",
    ),
):

    _check_auth(token)

    result = []

    for account in _visible_accounts(token):

        try:

            rt = load_runtime(
                account
            )

            login = get_login_status(
                account
            )

            mode_status = get_mode_status(
                account
            )

            result.append(
                {
                    "account": account,

                    "douyin_name":
                        rt.get(
                            "douyin_name"
                        ) or "",

                    "douyin_id":
                        rt.get(
                            "douyin_id"
                        ) or "",

                    "douyin_avatar":
                        rt.get(
                            "douyin_avatar"
                        ) or "",

                    "priority":
                        int(load_profile(account).get("priority") or 0),

                    "session_status":
                        rt.get(
                            "session_status",
                            "unknown",
                        ),

                    "running":
                        bool(
                            rt.get(
                                "running",
                                False,
                            )
                        ),

                    "login_in_progress":
                        login[
                            "in_progress"
                        ],

                    "last_login_at":
                        rt.get(
                            "last_login_at"
                        ),

                    "last_run":
                        rt.get(
                            "last_run"
                        ),

                    "state_file_exists":
                        state_path(
                            account
                        ).exists(),

                    "mode": mode_status.get("mode", "independent"),
                    "mode_active": mode_status.get("is_active", False),
                    "mode_status_text": mode_status.get("status_text", ""),
                }
            )

        except Exception:

            result.append(
                {
                    "account": account,
                    "douyin_name": "",
                    "douyin_id": "",
                    "douyin_avatar": "",
                    "priority": 0,
                    "session_status":
                        "unknown",
                    "running": False,
                    "login_in_progress":
                        False,
                    "last_login_at":
                        None,
                    "last_run": None,
                    "state_file_exists":
                        state_path(
                            account
                        ).exists(),
                    "mode": "independent",
                    "mode_active": False,
                    "mode_status_text": "",
                }
            )

    return {
        "accounts": result,
    }


# ============================================================
# 创建账号
# ============================================================

@app.post("/api/accounts")
def api_account_create(
    body: AccountBody,
    token: str = Header(
        default="",
        alias="X-Auth-Token",
    ),
):

    _check_auth(token)
    username = _current_username(token)
    is_adm = auth_mod.is_admin(username)

    # 普通用户：强制使用自己的用户名作为账号名
    # （data/accounts/<用户名>/，改名时账号目录会一起跟随）
    if not is_adm:
        name = username
    else:
        name = body.account.strip()

    if not name:
        raise HTTPException(
            status_code=400,
            detail="账号名称不能为空",
        )

    # 普通用户自行添加时仅允许绑定一个；管理员可在后台改成列表多个
    if not is_adm:
        existing = auth_mod.accounts_owned_by(username)
        if existing:
            # 已存在则直接复用，不报错（用户重新进入页面时自动选中）
            first = existing[0]
            if first in list_accounts():
                return {"ok": True, "account": first, "name": first, "reused": True}
            raise HTTPException(
                status_code=400,
                detail=f"您已绑定账号「{', '.join(existing)}」，请联系管理员调整",
            )

    try:

        result = create_account(
            name
        )

        # 写入 owner 到 profile
        try:
            from core.config import load_profile, save_profile
            prof = load_profile(name)
            prof["owner"] = username
            save_profile(name, prof)
        except Exception:
            pass

        if not is_adm:
            auth_mod.bind_account(username, name)

        # 说明：注册赠送的授权天数已在「注册时」发放到账号（见 api_auth_register），
        #       此处不再重复发放，避免重复加时。

        scheduler.apply_schedule(
            name
        )

        update_runtime(
            name,
            douyin_name="",
            douyin_id="",
            sec_uid="",
            session_status="unknown",
            running=False,
            login_error=None,
        )

        sync_mode_status(name)

        return {
            "ok": True,
            "account": name,
            "name": name,
        }

    except ValueError as exc:

        raise HTTPException(
            status_code=400,
            detail=str(exc),
        )

    except Exception as exc:

        logging.exception(
            "创建账号失败：%s",
            exc,
        )

        raise HTTPException(
            status_code=500,
            detail=f"创建账号失败：{exc}",
        )


@app.put("/api/accounts/{account}")
def api_account_rename(
    account: str,
    body: AccountBody,
    token: str = Header(
        default="",
        alias="X-Auth-Token",
    ),
):

    _check_auth(token)
    _assert_account_access(token, account.strip())
    username = _current_username(token)

    account = account.strip()
    new_name = body.account.strip()

    if not new_name:
        raise HTTPException(
            status_code=400,
            detail="账号名称不能为空",
        )

    if account == new_name:

        return {
            "ok": True,
            "account": account,
            "name": new_name,
        }

    # 正在登录时不允许改名
    with _login_guard:

        login_info = login_sessions.get(
            account
        )

        if (
            login_info
            and login_info.get("thread")
            and login_info["thread"].is_alive()
        ):
            raise HTTPException(
                status_code=409,
                detail="该账号正在登录，请先停止登录",
            )

    # 正在运行任务时不允许改名
    rt = load_runtime(
        account
    )

    if rt.get("running"):

        raise HTTPException(
            status_code=409,
            detail="该账号正在执行任务，请等待任务结束",
        )

    try:

        # 删除旧的每日调度
        scheduler.remove_schedule(
            account
        )

        # 重命名账号目录
        result = rename_account(
            account,
            new_name,
        )

        # 新名称重新建立调度
        scheduler.apply_schedule(
            new_name
        )

        sync_mode_status(new_name)

        # 若该账号绑定到当前用户，更新绑定名
        if auth_mod.get_bound_account(username) == account:
            auth_mod.bind_account(username, new_name)
        # 管理员改名时，同步所有用户绑定
        if auth_mod.is_admin(username):
            for u in auth_mod.list_users_for_admin():
                if u.get("bound_account") == account:
                    auth_mod.bind_account(u["username"], new_name)

        return {
            "ok": True,
            "old_account": account,
            "account": new_name,
            "name": new_name,
        }

    except ValueError as exc:

        # 如果改名失败，尽量恢复原来的调度
        try:
            scheduler.apply_schedule(
                account
            )
        except Exception:
            pass

        raise HTTPException(
            status_code=400,
            detail=str(exc),
        )

    except Exception as exc:

        logging.exception(
            "修改账号名称失败：%s",
            exc,
        )

        try:
            scheduler.apply_schedule(
                account
            )
        except Exception:
            pass

        raise HTTPException(
            status_code=500,
            detail=f"修改账号名称失败：{exc}",
        )


@app.delete("/api/accounts/{account}")
def api_account_delete(
    account: str,
    token: str = Header(
        default="",
        alias="X-Auth-Token",
    ),
):

    _check_auth(token)
    _assert_account_access(token, account.strip())
    username = _current_username(token)

    account = account.strip()

    if not account:
        raise HTTPException(
            status_code=400,
            detail="账号名称不能为空",
        )

    # ========================================================
    # 检查账号是否存在
    # ========================================================

    accounts = list_accounts()

    if account not in accounts:
        raise HTTPException(
            status_code=404,
            detail=f"账号不存在: {account}",
        )

    # ========================================================
    # 不允许删除正在运行的账号
    # ========================================================

    rt = load_runtime(account)

    if rt.get("running"):

        raise HTTPException(
            status_code=409,
            detail="该账号正在运行任务，请等待任务结束后再删除",
        )

    # ========================================================
    # 停止登录流程
    # ========================================================

    try:
        stop_login(account)
    except Exception:
        pass

    # ========================================================
    # 删除调度任务
    # ========================================================

    try:
        scheduler.remove_schedule(account)
    except Exception:
        pass

    # ========================================================
    # 删除可能存在的补发任务
    # ========================================================

    try:
        scheduler.cancel_retry(account)
    except Exception:
        pass

    is_adm = auth_mod.is_admin(username)

    # ========================================================
    # 普通用户：只清理「抖音相关数据」，保留授权 / 等级 / 到期时间
    #   删除：state.json、好友列表、日志、运行记录、截图
    #   保留：mode、enabled、authorized_until、priority（等级）
    # 管理员：整目录强制删除（彻底清理）
    # ========================================================

    if not is_adm:
        try:
            acct_path = account_dir(account)

            # 1) 删除登录态
            try:
                sp = state_path(account)
                if sp.exists():
                    sp.unlink()
            except Exception:
                logging.warning("账号 %s：删除 state.json 失败", account)

            # 2) 清空好友列表（保留其余配置：授权/等级/定时等）
            try:
                cfg = load_config(account)
                cfg["friends"] = []
                save_config(account, cfg)
            except Exception:
                logging.warning("账号 %s：清空好友列表失败", account)

            # 3) 删除日志与运行状态、截图等临时文件
            for rel in ("logs", "runtime.json", "last_error.png"):
                try:
                    target = acct_path / rel
                    if target.is_dir():
                        shutil.rmtree(target, ignore_errors=True)
                        drop_ring(target)
                    elif target.exists():
                        target.unlink()
                except Exception:
                    pass

            # 4) 重置运行状态（授权信息由 config.json 承载，不受影响）
            try:
                update_runtime(
                    account,
                    session_status="unknown",
                    running=False,
                    last_run=None,
                    last_run_at=None,
                    history=[],
                    history_count=0,
                    contacts=[],
                    contacts_at=None,
                    contacts_error=None,
                    login_error=None,
                    douyin_name="",
                    douyin_id="",
                    sec_uid="",
                    douyin_avatar="",
                    _progress=None,
                    _stop_progress=None,
                    _force_stop=False,
                )
            except Exception:
                logging.exception("账号 %s：重置运行状态失败", account)

            # 5) 解除绑定，但保留用户记录（授权仍在）
            try:
                for u in auth_mod.list_users_for_admin():
                    if account in (u.get("bound_accounts") or []) or u.get("bound_account") == account:
                        kept = [a for a in (u.get("bound_accounts") or []) if a != account]
                        auth_mod.set_bound_accounts(u["username"], kept)
            except Exception:
                logging.exception("账号 %s：解除用户绑定失败", account)

            try:
                scheduler.apply_schedule(account)
            except Exception:
                pass

            logging.info(
                "账号 %s 已由用户 %s 清理抖音数据（保留授权/等级）",
                account,
                username,
            )

            return {
                "ok": True,
                "account": account,
                "deleted": False,
                "cleared": True,
                "kept_auth": True,
                "message": "已清除该账号的登录态与好友数据，授权与等级已保留",
            }

        except HTTPException:
            raise
        except Exception as exc:
            logging.exception("清理账号数据失败：%s", exc)
            raise HTTPException(
                status_code=500,
                detail=f"清理账号数据失败：{exc}",
            )

    # ========================================================
    # 管理员：整目录强制删除
    # ========================================================

    try:

        account_path = account_dir(account)

        if account_path.exists():

            if not account_path.is_dir():

                raise HTTPException(
                    status_code=500,
                    detail=(
                        f"账号路径不是目录，"
                        f"拒绝删除: {account_path}"
                    ),
                )

            shutil.rmtree(
                account_path
            )
            drop_ring(target)

        # 管理员强制删除：连独立授权文件里的记录也一并清除
        try:
            drop_license(account)
        except Exception:
            logging.exception("账号 %s：清除独立授权记录失败", account)

        # ====================================================
        # 清理内存状态
        # ====================================================

        with _login_guard:

            login_sessions.pop(
                account,
                None,
            )

        with _locks_guard:

            run_locks.pop(
                account,
                None,
            )

        contacts_status.pop(
            account,
            None,
        )

        logging.info(
            "账号 %s 已删除，账号目录: %s",
            account,
            account_path,
        )

        # 解除用户绑定
        try:
            if auth_mod.get_bound_account(username) == account:
                auth_mod.bind_account(username, None)
            if auth_mod.is_admin(username):
                for u in auth_mod.list_users_for_admin():
                    if u.get("bound_account") == account:
                        auth_mod.bind_account(u["username"], None)
        except Exception:
            pass

        return {
            "ok": True,
            "account": account,
            "deleted": True,
            "path": str(account_path),
        }

    except HTTPException:
        raise

    except Exception as exc:

        logging.exception(
            "删除账号失败：%s",
            exc,
        )

        raise HTTPException(
            status_code=500,
            detail=f"删除账号失败：{exc}",
        )


# ============================================================
# 健康检查
# ============================================================

@app.get("/api/health")
def health():

    return {
        "ok": True,
        "accounts":
            len(
                list_accounts()
            ),
    }


# ============================================================
# 状态
# ============================================================

@app.get("/api/status")
def api_status(
    account: str | None = None,
    token: str = Header(
        default="",
        alias="X-Auth-Token",
    ),
):

    _check_auth(token)

    account = _get_account(account, token)

    rt = load_runtime(
        account
    )

    login = get_login_status(
        account
    )

    mode_status = get_mode_status(
        account
    )

    history = rt.get(
        "history",
        [],
    )

    if not isinstance(
        history,
        list,
    ):
        history = []

    return {
        "account": account,

        "douyin_name":
            rt.get(
                "douyin_name"
            ) or "",

        "douyin_id":
            rt.get(
                "douyin_id"
            ) or "",

        "douyin_avatar":
            rt.get(
                "douyin_avatar"
            ) or "",

        "state_file_exists":
            state_path(
                account
            ).exists(),

        "session_status":
            rt.get(
                "session_status",
                "unknown",
            ),

        "running":
            bool(
                rt.get(
                    "running",
                    False,
                )
            ),

        "last_run":
            rt.get(
                "last_run"
            ),

        "next_run":
            scheduler.next_run_time(
                account
            ),

        "history_count":
            len(history),

        "login_in_progress":
            login[
                "in_progress"
            ],

        "login_error":
            login[
                "login_error"
            ],

        "auth_required":
            bool(
                AUTH_TOKEN
            ),

        "version":
            APP_VERSION,

        "mode": mode_status.get("mode", "independent"),
        "mode_enabled": mode_status.get("enabled", True),
        "mode_active": mode_status.get("is_active", False),
        "mode_status_text": mode_status.get("status_text", ""),
        "authorized_until": mode_status.get("authorized_until"),
        "remaining_days": mode_status.get("remaining_days"),
    }


# ============================================================
# 登录
# ============================================================

@app.post("/api/login/start")
def api_login_start(
    account: str | None = None,
    token: str = Header(
        default="",
        alias="X-Auth-Token",
    ),
):

    _check_auth(token)
    if not _login_method_enabled("web_login"):
        raise HTTPException(status_code=403, detail="管理员已关闭网页登录方式")

    account = _get_account(account, token)

    return start_login(account)


@app.get("/api/login/qr")
def api_login_qr(
    account: str | None = None,
    token: str = Header(
        default="",
        alias="X-Auth-Token",
    ),
):
    """扫码登录状态：二维码 / 提示文案 / 验证页截图（前端 1~2 秒轮询）。"""
    _check_auth(token)
    account = _get_account(account, token)

    with _login_guard:
        info = login_sessions.get(account)
        if not info:
            return {
                "ok": False,
                "in_progress": False,
                "account": account,
                "message": "当前没有进行中的登录",
            }
        thread = info.get("thread")
        in_progress = bool(thread and thread.is_alive())

    return {
        "ok": True,
        "in_progress": in_progress,
        "account": account,
        "mode": info.get("mode") or "headed",
        "qr": info.get("qr"),
        "message": info.get("message") or "",
        "need_sms_code": bool(info.get("need_sms_code")),
        "waiting_sms_confirm": bool(info.get("waiting_sms_confirm")),
        "shot": info.get("shot"),
        "shot_ts": info.get("shot_at") or 0,
        "elapsed": int(time.time() - float(info.get("started_at") or time.time())),
    }


@app.get("/api/login/status")
def api_login_status(
    account: str | None = None,
    token: str = Header(
        default="",
        alias="X-Auth-Token",
    ),
):

    _check_auth(token)

    account = _get_account(account, token)

    return get_login_status(
        account
    )


class VerifyCodeBody(BaseModel):
    code: str


@app.post("/api/login/verify-code")
def api_login_verify_code(
    body: VerifyCodeBody,
    account: str | None = None,
    token: str = Header(
        default="",
        alias="X-Auth-Token",
    ),
):
    """扫码后的短信验证码：网页弹窗提交 → 登录线程自动填入抖音页面。"""
    _check_auth(token)
    account = _get_account(account, token)

    code = (body.code or "").strip()
    if not code.isdigit() or len(code) != 6:
        raise HTTPException(status_code=400, detail="请输入 6 位数字验证码")

    with _login_guard:
        info = login_sessions.get(account)
        thread = info.get("thread") if info else None
        if not info or not (thread and thread.is_alive()):
            raise HTTPException(status_code=400, detail="当前没有进行中的登录")
        info["sms_code"] = code
        info["code_event"].set()

    return {"ok": True, "message": "验证码已提交，正在自动填入抖音页面…"}


@app.post("/api/login/click-sms")
def api_login_click_sms(
    account: str | None = None,
    token: str = Header(
        default="",
        alias="X-Auth-Token",
    ),
):
    """用户在网页点击倒计时按钮 → 登录线程点击「接收短信验证码」。"""
    _check_auth(token)
    account = _get_account(account, token)

    with _login_guard:
        info = login_sessions.get(account)
        thread = info.get("thread") if info else None
        if not info or not (thread and thread.is_alive()):
            raise HTTPException(status_code=400, detail="当前没有进行中的登录")
        info["sms_click_event"].set()

    return {"ok": True, "message": "已发送点击指令"}


@app.post("/api/login/stop")
def api_login_stop(
    account: str | None = None,
    token: str = Header(
        default="",
        alias="X-Auth-Token",
    ),
):

    _check_auth(token)

    account = _get_account(account, token)

    return stop_login(
        account
    )


# ============================================================
# 配置
# ============================================================

@app.get("/api/config")
def api_config(
    account: str | None = None,
    token: str = Header(
        default="",
        alias="X-Auth-Token",
    ),
):

    _check_auth(token)

    account = _get_account(account, token)

    return load_config(
        account
    )




@app.post("/api/ai/generate")
def api_ai_generate(
    body: dict | None = None,
    account: str | None = None,
    token: str = Header(default="", alias="X-Auth-Token"),
):
    """测试生成一条续火花文案，并写入账号系统日志。"""
    _check_auth(token)
    body = body or {}
    friend = str(body.get("friend_name") or body.get("name") or "测试好友").strip() or "测试好友"
    cfg = {
        "ai_api_base": body.get("ai_api_base"),
        "ai_api_key": body.get("ai_api_key"),
        "ai_model": body.get("ai_model"),
        "ai_system_prompt": body.get("ai_system_prompt"),
        "ai_enabled": True,
    }
    acc_for_log = None
    try:
        if account or body.get("account"):
            acc_for_log = _get_account(account or body.get("account"), token)
            saved = load_config(acc_for_log)
            for k, v in list(cfg.items()):
                if k == "ai_enabled":
                    continue
                if v is None or (isinstance(v, str) and not str(v).strip()):
                    cfg[k] = saved.get(k)
    except Exception:
        acc_for_log = None

    text = automation.generate_ai_message(cfg, friend_name=friend)
    if text:
        return {"ok": True, "sample": text, "friend_name": friend, "message": "生成成功"}
    # 再跑一次 test 拿详细错误（不写系统日志，仅返回给定时页展示）
    detail = automation.test_ai_api(cfg)
    err = detail.get("error") or "生成失败，未返回内容"
    return {"ok": False, "error": err, "friend_name": friend, "latency_ms": detail.get("latency_ms")}


@app.post("/api/ai/test")
def api_ai_test(
    body: dict | None = None,
    account: str | None = None,
    token: str = Header(default="", alias="X-Auth-Token"),
):
    """测试当前表单或已保存的 AI 接口是否连通。"""
    _check_auth(token)
    body = body or {}
    # 优先用请求体中的临时配置（未保存也能测）
    cfg = {
        "ai_api_base": body.get("ai_api_base"),
        "ai_api_key": body.get("ai_api_key"),
        "ai_model": body.get("ai_model"),
        "ai_system_prompt": body.get("ai_system_prompt"),
    }
    # 缺省项从账号配置补全
    if account or body.get("account"):
        try:
            acc = _get_account(account or body.get("account"), token)
            saved = load_config(acc)
            for k, v in list(cfg.items()):
                if v is None or (isinstance(v, str) and not str(v).strip()):
                    cfg[k] = saved.get(k)
        except Exception:
            pass
    result = automation.test_ai_api(cfg)
    # 测试结果只返回前端在「定时」页展示，不写系统运行日志
    return {"ok": bool(result.get("ok")), **result}


@app.put("/api/config")
def api_config_save(
    body: ConfigBody,
    account: str | None = None,
    token: str = Header(
        default="",
        alias="X-Auth-Token",
    ),
):

    _check_auth(token)

    account = _get_account(account, token)

    # ------------------------------------------------------------
    # 授权相关字段只允许管理员修改。
    # 否则普通用户 PUT {"mode":"independent","enabled":true} 就能
    # 绕过卡密/授权（调度器 _should_run 只读 config.json 的这两个字段）。
    # ------------------------------------------------------------
    incoming = body.config or {}
    username = _current_username(token)
    if not auth_mod.is_admin(username):
        incoming = dict(incoming)
        for k in ("mode", "enabled", "authorized_until"):
            incoming.pop(k, None)

    cfg = save_config(
        account,
        incoming,
    )

    scheduler.apply_schedule(
        account
    )

    sync_mode_status(account)

    return {
        "ok": True,
        "account": account,
        "config": cfg,
    }


# ============================================================
# 手动运行
# ============================================================

@app.post("/api/run")
def api_run(
    body: RunBody,
    account: str | None = None,
    token: str = Header(
        default="",
        alias="X-Auth-Token",
    ),
):

    _check_auth(token)

    account = _get_account(account, token)

    # ====== 检查模式状态 ======
    mode_status = get_mode_status(account)
    if not mode_status.get("is_active", False):
        raise HTTPException(
            status_code=403,
            detail=f"当前模式已禁用：{mode_status.get('status_text', '未知原因')}，请先启用续火花功能",
        )

    dry = bool(body.dry)
    # 用户点击「立即发送」（非干跑）完成后，强制发送邮箱日志
    _start_run(
        account,
        dry,
        force_email=(not dry),
    )

    return {
        "ok": True,
        "started": True,
        "account": account,
        "dry": body.dry,
    }


# ============================================================
# 强制停止
# ============================================================

@app.post("/api/stop")
def api_stop(
    account: str | None = None,
    token: str = Header(
        default="",
        alias="X-Auth-Token",
    ),
):

    _check_auth(token)

    account = _get_account(account, token)

    logger = _logger(account)

    # ====================================================
    # 1. 检查是否有正在运行的任务
    # ====================================================

    rt = load_runtime(account)

    if not rt.get("running", False):

        raise HTTPException(
            status_code=409,
            detail="该账号当前没有正在运行的任务",
        )

    # ====================================================
    # 2. 获取当前任务的进度
    # ====================================================

    # 注意：_progress 的键总是存在，且常为 None（默认值 + 每轮结束时写回 null），
    # 所以必须用 `or {}` 兜底，不能依赖 dict.get 的默认值，否则 None.get() 会 500。
    progress = rt.get("_progress") or {}
    if not isinstance(progress, dict):
        progress = {}
    ok_list = progress.get("ok", []) or []
    failed_list = progress.get("failed", []) or []
    total = progress.get("total", 0) or 0
    sent = progress.get("sent", 0) or 0

    # ====================================================
    # 3. 发出强制停止信号（发送循环会检测并真正中断）
    # ====================================================

    update_runtime(
        account,
        _force_stop=True,
        _stopped_at=time.strftime("%Y-%m-%d %H:%M:%S"),
        _stop_progress={
            "ok": ok_list if isinstance(ok_list, list) else [],
            "failed": failed_list if isinstance(failed_list, list) else [],
            "total": total,
            "sent": sent,
        },
    )

    # 注意：不要在这里强行 release 锁 / 置 running=False
    # 否则工作线程仍在发消息，UI 却显示已停止。
    # 工作线程在检测 _force_stop 后会自行结束并在 finally 里 set_running(False)+release。

    # ====================================================
    # 5. 记录停止日志
    # ====================================================

    logger.warning("=" * 50)
    logger.warning("【强制停止】账号 %s 的续火花进程已被手动停止", account)
    logger.warning("  停止时间: %s", time.strftime("%Y-%m-%d %H:%M:%S"))
    logger.warning("  已发送: %s 人", sent)
    logger.warning("  成功: %s 人", len(ok_list))
    logger.warning("  失败: %s 人", len(failed_list))
    logger.warning("  剩余: %s 人", total - sent)
    logger.warning("=" * 50)

    return {
        "ok": True,
        "account": account,
        "stopped": True,
        "message": f"已强制停止账号 {account} 的续火花进程",
        "stopped_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "progress": {
            "sent": sent,
            "ok": len(ok_list),
            "failed": len(failed_list),
            "total": total,
            "remaining": total - sent,
        }
    }


# ============================================================
# 一键刷新全部账号 Cookie
# ============================================================

@app.post("/api/refresh-cookie-all")
def api_refresh_cookie_all(
    token: str = Header(
        default="",
        alias="X-Auth-Token",
    ),
):
    _check_auth(token)
    results = []
    for account in _visible_accounts(token):
        item = {
            "account": account,
            "ok": False,
            "douyin_name": "",
            "douyin_id": "",
            "douyin_avatar": "",
            "error": None,
        }
        try:
            state = state_path(account)
            if not state.exists():
                item["error"] = "state.json 不存在"
                results.append(item)
                continue
            user_info = automation.get_user_info_from_cookie(account)
            if "error" in user_info:
                err = user_info.get("error") or "刷新失败"
                item["error"] = err
                login_error_keywords = [
                    "sessionid", "登录已过期", "登录态已失效", "请重新登录",
                    "cookie 已失效", "缺少 sessionid", "未登录",
                ]
                err_l = str(err).lower()
                is_login = any(k.lower() in err_l for k in login_error_keywords)
                if is_login:
                    update_runtime(
                        account,
                        session_status="expired",
                        login_error=f"Cookie 刷新失败：{err}",
                        douyin_name="",
                        douyin_id="",
                        sec_uid="",
                        douyin_avatar="",
                    )
                results.append(item)
                continue

            update_runtime(
                account,
                douyin_name=user_info.get("nickname") or "",
                douyin_id=user_info.get("unique_id") or "",
                sec_uid=user_info.get("sec_uid") or "",
                douyin_avatar=user_info.get("avatar") or "",
                session_status="ok",
                login_error=None,
            )
            item["ok"] = True
            item["douyin_name"] = user_info.get("nickname") or ""
            item["douyin_id"] = user_info.get("unique_id") or ""
            item["douyin_avatar"] = user_info.get("avatar") or ""
            results.append(item)
        except Exception as e:
            item["error"] = str(e)
            results.append(item)

    ok_n = sum(1 for r in results if r["ok"])
    return {
        "ok": True,
        "total": len(results),
        "success": ok_n,
        "failed": len(results) - ok_n,
        "results": results,
    }


# ============================================================
# 设置账号优先级（数字越大越优先续火花 / 列表越靠前）
# ============================================================

@app.post("/api/accounts/priority")
def api_set_priority(
    body: dict,
    account: str | None = None,
    token: str = Header(
        default="",
        alias="X-Auth-Token",
    ),
):
    _require_admin(token)
    account = _get_account(account, token)
    if "priority" not in body:
        raise HTTPException(status_code=400, detail="缺少 priority 字段")
    try:
        priority = int(body.get("priority"))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="priority 必须是整数")
    # 等级统一为 1~8（V1~V8），数字越大越优先
    priority = max(1, min(priority, 8))
    profile = load_profile(account)
    profile["priority"] = priority
    save_profile(account, profile)
    # 同步写入独立授权文件，避免用户清理数据后等级丢失
    try:
        save_license(account, {"priority": priority})
    except Exception:
        logging.exception("账号 %s：写入独立授权等级失败", account)
    return {
        "ok": True,
        "account": account,
        "priority": priority,
    }


# ============================================================
# 刷新 Cookie（重新提取用户信息 + 刷新登录状态）
# ============================================================

@app.post("/api/refresh-cookie")
def api_refresh_cookie(
    account: str | None = None,
    token: str = Header(
        default="",
        alias="X-Auth-Token",
    ),
):

    _check_auth(token)

    account = _get_account(account, token)

    logger = _logger(account)

    # ====================================================
    # 检查 state.json 是否存在
    # ====================================================

    state_file = state_path(account)
    if not state_file.exists():
        raise HTTPException(
            status_code=404,
            detail="state.json 不存在，请先登录",
        )

    # ====================================================
    # 调用 douyin-api 提取用户信息，同时验证登录状态
    # ====================================================

    logger.info("账号 %s：开始刷新 Cookie 信息...", account)

    try:
        user_info = automation.get_user_info_from_cookie(account)

        # 判断是否登录失效
        if "error" in user_info:
            error_msg = user_info.get("error", "")
            
            # 仅把明确指向登录态失效的文案标为 expired
            # 避免把「网络/风控/解析失败」误判成掉线
            login_error_keywords = [
                "sessionid",
                "登录已过期",
                "登录态已失效",
                "登录态失效",
                "请重新登录",
                "cookie 已失效",
                "cookie中缺少",
                "cookie 中缺少",
                "未登录",
                "扫码登录",
                "invalid session",
                "expired",
            ]
            err_lower = error_msg.lower()
            is_login_error = any(
                keyword.lower() in err_lower
                for keyword in login_error_keywords
            )
            
            if is_login_error:
                # 登录失效 → 标记为 expired
                update_runtime(
                    account,
                    session_status="expired",
                    login_error=f"Cookie 刷新失败：{error_msg}",
                    douyin_name="",
                    douyin_id="",
                    sec_uid="",
                    douyin_avatar="",
                )
                logger.warning("账号 %s：Cookie 已失效，标记为登录过期：%s", account, error_msg)
                raise HTTPException(
                    status_code=401,
                    detail=f"登录已过期，请重新登录：{error_msg}",
                )
            else:
                # 其他错误
                logger.warning("账号 %s：刷新 Cookie 失败：%s", account, error_msg)
                raise HTTPException(
                    status_code=400,
                    detail=f"刷新失败：{error_msg}",
                )

        # 提取成功 → 更新用户信息，标记登录正常
        update_runtime(
            account,
            douyin_name=user_info.get("nickname") or "",
            douyin_id=user_info.get("unique_id") or "",
            sec_uid=user_info.get("sec_uid") or "",
            douyin_avatar=user_info.get("avatar") or "",
            session_status="ok",
            login_error=None,
        )

        logger.info(
            "账号 %s：刷新 Cookie 成功 - 昵称=%s, 抖音号=%s，登录状态正常",
            account,
            user_info.get("nickname", ""),
            user_info.get("unique_id", ""),
        )

        # 同步到前端显示
        sync_mode_status(account)

        return {
            "ok": True,
            "account": account,
            "douyin_name": user_info.get("nickname", ""),
            "douyin_id": user_info.get("unique_id", ""),
            "sec_uid": user_info.get("sec_uid", ""),
            "douyin_avatar": user_info.get("avatar", ""),
            "session_status": "ok",
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error("账号 %s：刷新 Cookie 异常：%s", account, e)
        # 异常也标记为登录失效
        update_runtime(
            account,
            session_status="expired",
            login_error=f"刷新异常：{e}",
        )
        raise HTTPException(
            status_code=500,
            detail=f"刷新异常：{e}",
        )


# ============================================================
# 联系人
# ============================================================

@app.get("/api/contacts")
def api_contacts(
    account: str | None = None,
    token: str = Header(
        default="",
        alias="X-Auth-Token",
    ),
):

    _check_auth(token)

    account = _get_account(account, token)

    rt = load_runtime(
        account
    )

    raw_contacts = rt.get("contacts", [])
    if not isinstance(raw_contacts, list):
        raw_contacts = []

    return {
        "account": account,
        "contacts": _normalize_contacts(raw_contacts),
        "contacts_at": rt.get("contacts_at"),
        "contacts_error": rt.get("contacts_error"),
        "fetching": contacts_status.get(account, False),
    }


@app.post("/api/contacts/fetch")
def api_contacts_fetch(
    account: str | None = None,
    token: str = Header(
        default="",
        alias="X-Auth-Token",
    ),
):

    _check_auth(token)

    account = _get_account(account, token)

    _start_fetch_contacts(
        account
    )

    return {
        "ok": True,
        "started": True,
        "account": account,
    }


# ============================================================

# 退出抖音登录：删除 state.json，保留账号配置
@app.post("/api/state/clear")
def api_clear_state(
    account: str | None = None,
    token: str = Header(default="", alias="X-Auth-Token"),
):
    _check_auth(token)
    account = _get_account(account, token)
    _assert_account_access(token, account)

    sp = state_path(account)
    deleted = False
    if sp.exists():
        try:
            sp.unlink()
            deleted = True
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"删除 state.json 失败：{exc}")

    # 停止进行中的网页登录。
    # 注意：只 pop 会话不够 —— 登录线程仍在运行，一旦中途登录成功会把
    # state.json 重新写回来，等于撤销了这次「退出登录」。
    # 必须先给线程发停止信号，再移除会话。
    with _login_guard:
        _sess = login_sessions.pop(account, None)
    if isinstance(_sess, dict):
        try:
            _ev = _sess.get("stop_event")
            if _ev is not None:
                _ev.set()
        except Exception:
            logging.exception("停止登录线程失败：%s", account)

    update_runtime(
        account,
        session_status="unknown",
        login_in_progress=False,
        login_error=None,
        douyin_name="",
        douyin_id="",
        douyin_avatar="",
    )
    return {
        "ok": True,
        "account": account,
        "deleted": deleted,
        "message": "已退出抖音登录" if deleted else "当前无登录态文件",
    }


# 上传 state.json
# ============================================================

@app.post("/api/upload-state")
async def api_upload_state(
    file: UploadFile = File(...),
    account: str | None = None,
    token: str = Header(
        default="",
        alias="X-Auth-Token",
    ),
):

    _check_auth(token)
    if not _login_method_enabled("upload_state"):
        raise HTTPException(status_code=403, detail="管理员已关闭「上传登录态」方式")

    account = _get_account(account, token)

    raw = await file.read()

    if len(raw) > 5 * 1024 * 1024:

        raise HTTPException(
            status_code=400,
            detail="文件过大",
        )

    try:
        # utf-8-sig 兼容带 BOM 的 state.json
        data = json.loads(raw.decode("utf-8-sig"))
    except Exception:
        raise HTTPException(
            status_code=400,
            detail="JSON格式错误",
        )

    if not isinstance(data, dict):
        raise HTTPException(
            status_code=400,
            detail="JSON根节点必须是对象",
        )

    if (
        not isinstance(data.get("cookies"), list)
        or not data["cookies"]
    ):
        raise HTTPException(
            status_code=400,
            detail="缺少cookies字段",
        )

    path = state_path(account)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    # 上传后立即用 Cookie 提取昵称 / 抖音号
    douyin_name = ""
    douyin_id = ""
    sec_uid = ""
    douyin_avatar = ""
    session_status = "unknown"
    login_error = None
    try:
        # Playwright 同步 API 不能在 asyncio 事件循环里直接调用
        # （否则报 "It looks like you are using Playwright Sync API inside the asyncio loop"）
        # 放到独立线程中执行。
        user_info = await asyncio.to_thread(
            automation.get_user_info_from_cookie,
            account,
        )
        if "error" not in user_info:
            douyin_name = user_info.get("nickname") or ""
            douyin_id = user_info.get("unique_id") or ""
            sec_uid = user_info.get("sec_uid") or ""
            douyin_avatar = user_info.get("avatar") or ""
            session_status = "ok"
            logger = _logger(account)
            logger.info(
                "账号 %s：上传 state 后提取用户信息 - 昵称=%s, 抖音号=%s",
                account,
                douyin_name,
                douyin_id,
            )
        else:
            login_error = user_info.get("error")
            # 有 sessionid 但提取失败时，先标为 unknown，不直接判过期
            err = str(login_error)
            if "sessionid" in err.lower() or "缺少 sessionid" in err:
                session_status = "expired"
    except Exception as e:
        login_error = f"提取用户信息失败：{e}"

    update_runtime(
        account,
        session_status=session_status,
        login_error=login_error,
        douyin_name=douyin_name,
        douyin_id=douyin_id,
        sec_uid=sec_uid,
        douyin_avatar=douyin_avatar,
    )

    return {
        "ok": True,
        "account": account,
        "size": len(raw),
        "douyin_name": douyin_name,
        "douyin_id": douyin_id,
        "sec_uid": sec_uid,
        "douyin_avatar": douyin_avatar,
        "session_status": session_status,
    }


# ============================================================
# 快捷授权 API
# ============================================================

@app.post("/api/mode/add-days")
def api_mode_add_days(
    body: dict,
    account: str | None = None,
    token: str = Header(
        default="",
        alias="X-Auth-Token",
    ),
):
    """
    快捷授权：添加指定天数（仅管理员）
    body: {
        "days": 3  # 1, 3, 7, 15, 30, 90, 180, 365
    }
    """
    _require_admin(token)
    account = _get_account(account, token)

    days = body.get("days")
    if not days:
        raise HTTPException(
            status_code=400,
            detail="请指定授权天数"
        )

    try:
        days = int(days)
    except (TypeError, ValueError):
        raise HTTPException(
            status_code=400,
            detail="days 必须是整数"
        )

    if days <= 0:
        raise HTTPException(
            status_code=400,
            detail="天数必须大于 0"
        )

    result = add_authorized_days(account, days)
    sync_mode_status(account)

    # 重新应用调度
    scheduler.apply_schedule(account)

    return {
        "ok": True,
        **result,
    }


# ============================================================
# 模式管理 API
# ============================================================

@app.get("/api/mode/status")
def api_mode_status(
    account: str | None = None,
    token: str = Header(
        default="",
        alias="X-Auth-Token",
    ),
):
    """获取账号的模式状态"""
    _check_auth(token)
    account = _get_account(account, token)
    status = get_mode_status(account)
    sync_mode_status(account)
    return {
        "account": account,
        **status,
    }


@app.put("/api/mode")
def api_mode_set(
    body: dict,
    account: str | None = None,
    token: str = Header(
        default="",
        alias="X-Auth-Token",
    ),
):
    """
    设置账号模式（仅管理员）
    body: {
        "mode": "independent" | "authorized",
        "enabled": bool,
        "authorized_until": "2026-12-31T23:59:59"
    }
    """
    _require_admin(token)
    account = _get_account(account, token)

    cfg = load_config(account)
    patch: dict = {}

    if "mode" in body:
        mode = body["mode"]
        if mode not in ["independent", "authorized"]:
            raise HTTPException(
                status_code=400,
                detail="mode 必须是 independent 或 authorized"
            )
        cfg["mode"] = mode
        patch["mode"] = mode

    if "enabled" in body:
        cfg["enabled"] = bool(body["enabled"])
        patch["enabled"] = bool(body["enabled"])

    if "authorized_until" in body:
        val = body["authorized_until"]
        if val is not None:
            try:
                from datetime import datetime
                datetime.fromisoformat(str(val))
            except Exception:
                raise HTTPException(
                    status_code=400,
                    detail="authorized_until 必须是 ISO 格式日期"
                )
        cfg["authorized_until"] = val
        patch["authorized_until"] = val

    saved = save_config(account, cfg)

    # 授权信息同时写入独立文件（防删除）
    try:
        if patch:
            save_license(account, patch)
    except Exception:
        logging.exception("账号 %s：写入独立授权文件失败", account)

    sync_mode_status(account)

    scheduler.apply_schedule(account)

    return {
        "ok": True,
        "account": account,
        "config": saved,
    }


@app.post("/api/mode/toggle")
def api_mode_toggle(
    account: str | None = None,
    token: str = Header(
        default="",
        alias="X-Auth-Token",
    ),
):
    """切换独立模式的启用/禁用状态（仅管理员）"""
    _require_admin(token)
    account = _get_account(account, token)

    cfg = load_config(account)
    cfg["enabled"] = not cfg.get("enabled", True)
    saved = save_config(account, cfg)
    sync_mode_status(account)

    return {
        "ok": True,
        "account": account,
        "enabled": saved["enabled"],
        "status": "已开启" if saved["enabled"] else "已关闭",
    }



# ============================================================
# 授权卡密
# ============================================================



@app.get("/api/admin/spark-runtime")
def api_admin_spark_runtime_get(token: str = Header(default="", alias="X-Auth-Token")):
    _require_admin(token)
    s = site_settings.load()
    with _global_send_lock:
        running = sorted(_global_running_accounts)
    return {
        "ok": True,
        "max_parallel": int((s.get("spark_runtime") or {}).get("max_parallel") or 1),
        "running_accounts": running,
        "running_count": len(running),
    }


@app.put("/api/admin/spark-runtime")
def api_admin_spark_runtime_put(body: dict, token: str = Header(default="", alias="X-Auth-Token")):
    _require_admin(token)
    saved = site_settings.save({"spark_runtime": {"max_parallel": body.get("max_parallel", 1)}})
    return {"ok": True, "max_parallel": saved["spark_runtime"]["max_parallel"]}


@app.get("/api/admin/spark-tasks")
def api_admin_spark_tasks(token: str = Header(default="", alias="X-Auth-Token")):
    """续火花任务列表：运行中 / 待处理账号状态。"""
    _require_admin(token)
    items = []
    with _global_send_lock:
        running_set = set(_global_running_accounts)
    for name in list_accounts():
        try:
            rt = load_runtime(name)
            cfg = load_config(name)
            prof = load_profile(name)
            mode = get_mode_status(name)
            pri = int(prof.get("priority") or 1)
            pri = max(1, min(pri, 8))

            # ===== 登录态 =====
            session = str(rt.get("session_status") or "unknown")
            if state_path(name).exists():
                if session == "ok":
                    session_text = "已登录"
                elif session == "logging_in":
                    session_text = "登录中"
                elif session == "expired":
                    session_text = "已失效"
                elif session == "failed":
                    session_text = "异常"
                else:
                    session_text = "待校验"
            else:
                session_text = "无登录态" if session != "logging_in" else "登录中"

            # ===== 授权：独立模式显示无限制，授权模式显示到期时间 =====
            mode_name = mode.get("mode") or cfg.get("mode") or "independent"
            if mode_name == "independent":
                auth_text = "无限制" if mode.get("is_active") else "已关闭"
            else:
                until = mode.get("authorized_until")
                if until:
                    auth_text = str(until).replace("T", " ")[:16]
                    rd = mode.get("remaining_days")
                    if rd is not None:
                        auth_text += f"（剩{rd}天）"
                else:
                    auth_text = "未授权"

            # ===== 下次发送：优先调度器实时值，回退 runtime =====
            next_run = None
            try:
                next_run = scheduler.next_run_time(name)
            except Exception:
                next_run = None
            if not next_run:
                next_run = rt.get("next_run")

            # ===== 上次运行 =====
            last_run_at = rt.get("last_run_at")
            if not last_run_at:
                last = rt.get("last_run")
                if isinstance(last, dict):
                    last_run_at = last.get("started_at") or last.get("at")

            hist = rt.get("history") or []
            hist_count = rt.get("history_count")
            if hist_count is None:
                hist_count = len(hist) if isinstance(hist, list) else 0

            items.append({
                "account": name,
                "douyin_name": rt.get("douyin_name") or prof.get("douyin_name") or "",
                "douyin_id": rt.get("douyin_id") or "",
                "vip_level": pri,
                "vip_label": f"V{pri}",
                "running": bool(rt.get("running")) or name in running_set,
                "session_status": session,
                "session_text": session_text,
                "has_state": state_path(name).exists(),
                "mode": mode_name,
                "mode_active": bool(mode.get("is_active")),
                "authorized_until": mode.get("authorized_until"),
                "remaining_days": mode.get("remaining_days"),
                "auth_text": auth_text,
                "next_run": next_run,
                "last_run_at": last_run_at,
                "history_count": int(hist_count or 0),
                "friends_count": len(cfg.get("friends") or []),
            })
        except Exception:
            continue
    # 等级数字越大越优先（V8 最前），运行中靠在最前
    items.sort(key=lambda x: (0 if x["running"] else 1, -int(x.get("vip_level") or 1), x.get("account") or ""))
    return {"ok": True, "items": items, "max_parallel": _max_parallel()}

@app.get("/api/admin/visit-ips")
def api_admin_visit_ips(
    limit: int = 100,
    token: str = Header(default="", alias="X-Auth-Token"),
):
    """访问 IP 记录（管理员，原始明细）"""
    _require_admin(token)
    return {"ok": True, "items": stats_mod.list_visit_ips(limit)}


@app.get("/api/admin/visit-ips-summary")
def api_admin_visit_ips_summary(
    limit: int = 500,
    token: str = Header(default="", alias="X-Auth-Token"),
):
    """按 IP 聚合的访问列表：标明是哪个用户登录的，区分用户/访客。"""
    _require_admin(token)
    data = stats_mod.visit_ip_summary(limit)
    return {"ok": True, **data}


@app.post("/api/admin/visit-ips/refresh-region")
def api_admin_visit_ips_refresh_region(
    body: dict,
    token: str = Header(default="", alias="X-Auth-Token"),
):
    """重新查询某个 IP 的归属地（对结果不准的 IP 手动刷新）。"""
    _require_admin(token)
    ip = str((body or {}).get("ip") or "").strip()
    if not ip:
        raise HTTPException(status_code=400, detail="缺少 IP")
    # 强制重新查询（绕过缓存）
    region = stats_mod.resolve_ip_region(ip, force=True)
    # 把新结果写回历史记录，列表刷新后立即看到变化
    try:
        updated = stats_mod.update_ip_region(ip, region)
    except Exception:
        logging.exception("写回归属地失败：%s", ip)
        updated = 0
    return {"ok": True, "ip": ip, "region": region, "updated": updated}


# ============================================================
# 卡密核销系统（管理员端）
# ============================================================

@app.get("/api/admin/card-keys/lookup")
def api_admin_card_key_lookup(
    key: str,
    token: str = Header(default="", alias="X-Auth-Token"),
):
    """核销查询：输入卡密，返回面额、状态、使用者等信息（预览用，不核销）。"""
    _require_admin(token)
    raw = (key or "").strip()
    if not raw:
        raise HTTPException(status_code=400, detail="请输入卡密")

    info = card_keys.find_card_key(raw)
    if not info:
        # 放宽：用前缀读取面额，提示无效
        guess = card_keys.parse_days(raw)
        return {
            "ok": True,
            "found": False,
            "days_guess": guess,
            "message": "卡密不存在" + (f"（按前缀看像是 {guess} 天卡）" if guess else ""),
        }
    return {"ok": True, "found": True, "key_info": info}


@app.post("/api/admin/card-keys/redeem")
def api_admin_card_key_redeem(
    body: dict,
    token: str = Header(default="", alias="X-Auth-Token"),
):
    """管理员代为核销卡密：给指定账号叠加天数。"""
    admin = _require_admin(token)
    raw = str((body or {}).get("key") or "").strip()
    account = str((body or {}).get("account") or "").strip()
    if not raw:
        raise HTTPException(status_code=400, detail="请输入卡密")
    if not account:
        raise HTTPException(status_code=400, detail="请选择要核销的账号")
    if account not in list_accounts():
        raise HTTPException(status_code=404, detail=f"账号不存在: {account}")

    try:
        info = card_keys.redeem_card_key(raw, admin, account)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    result = add_authorized_days(account, info["days"])
    sync_mode_status(account)
    try:
        scheduler.apply_schedule(account)
    except Exception:
        pass

    return {
        "ok": True,
        "key": info["key"],
        "days": info["days"],
        "account": account,
        "authorized_until": result.get("authorized_until"),
        "message": f"核销成功，账号「{account}」已叠加 {info['days']} 天",
    }


@app.post("/api/admin/card-keys/batch")
def api_admin_card_keys_batch(
    body: dict,
    token: str = Header(default="", alias="X-Auth-Token"),
):
    """批量生成授权卡密"""
    admin = _require_admin(token)
    # 非法入参（如 "abc"）应返回 400，而不是 500
    try:
        days = int((body or {}).get("days") or 30)
        count = int((body or {}).get("count") or 1)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="days 与 count 必须是整数")
    note = str((body or {}).get("note") or "")
    try:
        items = card_keys.create_card_keys_batch(days, count, note=note, created_by=admin)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"ok": True, "keys": items, "count": len(items)}


@app.post("/api/admin/keys/batch")
def api_admin_keys_batch(
    body: dict,
    token: str = Header(default="", alias="X-Auth-Token"),
):
    """批量生成注册密钥"""
    admin = _require_admin(token)
    count = int((body or {}).get("count") or 1)
    note = str((body or {}).get("note") or "")
    items = auth_mod.create_invite_keys_batch(admin, count=count, note=note)
    return {"ok": True, "keys": items, "count": len(items)}

@app.get("/api/admin/card-keys")
def api_admin_card_keys(
    token: str = Header(default="", alias="X-Auth-Token"),
):
    _require_admin(token)
    return {"ok": True, "keys": card_keys.list_card_keys()}


@app.post("/api/admin/card-keys")
def api_admin_create_card_key(
    body: dict,
    token: str = Header(default="", alias="X-Auth-Token"),
):
    _require_admin(token)
    username = _current_username(token)
    days = body.get("days", 30)
    note = body.get("note") or ""
    try:
        days = int(days)
        info = card_keys.create_card_key(days, note=note, created_by=username)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"ok": True, "key": info}


@app.delete("/api/admin/card-keys/{key}")
def api_admin_delete_card_key(
    key: str,
    token: str = Header(default="", alias="X-Auth-Token"),
):
    _require_admin(token)
    try:
        card_keys.delete_card_key(key)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"ok": True}


@app.get("/api/admin/auth-accounts")
def api_admin_auth_accounts(
    token: str = Header(default="", alias="X-Auth-Token"),
):
    """管理员：查看全部账号的授权状态。"""
    _require_admin(token)
    items = []
    for name in list_accounts():
        st = get_mode_status(name)
        rt = load_runtime(name)
        pri = int(load_profile(name).get("priority") or 1)
        pri = max(1, min(pri, 8))
        items.append({
            "account": name,
            "douyin_name": rt.get("douyin_name") or "",
            "douyin_id": rt.get("douyin_id") or "",
            "priority": pri,
            **st,
        })
    # 数字越大越优先（V8 在前）
    items.sort(key=lambda x: (-int(x.get("priority") or 1), x.get("account") or ""))
    return {"ok": True, "accounts": items}


@app.post("/api/mode/redeem")
def api_mode_redeem(
    body: dict,
    account: str | None = None,
    token: str = Header(default="", alias="X-Auth-Token"),
):
    """用户兑换授权卡密：在原有到期时间上叠加天数。"""
    _check_auth(token)
    username = _current_username(token)
    account = _get_account(account or (body or {}).get("account"), token)
    key = (body or {}).get("key") or ""
    try:
        info = card_keys.redeem_card_key(key, username, account)
    except ValueError as exp:
        raise HTTPException(status_code=400, detail=str(exp))
    result = add_authorized_days(account, info["days"])
    sync_mode_status(account)
    try:
        scheduler.apply_schedule(account)
    except Exception:
        pass
    return {
        "ok": True,
        "message": f"卡密兑换成功，已叠加 {info['days']} 天",
        "days": info["days"],
        **result,
    }


# ============================================================
# 日志
# ============================================================

@app.get("/api/logs")
def api_logs(
    account: str | None = None,
    n: int = 300,
    token: str = Header(
        default="",
        alias="X-Auth-Token",
    ),
):

    _check_auth(token)

    account = _get_account(account, token)

    n = max(
        10,
        min(
            int(n),
            600,
        ),
    )

    logs = recent_logs(
        account,
        n,
    )

    return {
        "account": account,
        "logs": "\n".join(logs),
    }


# ============================================================
# 启动
# ============================================================

if __name__ == "__main__":

    host = os.environ.get(
        "HOST",
        "0.0.0.0",
    )

    port = int(
        os.environ.get(
            "PORT",
            "8000",
        )
    )

    uvicorn.run(
        app,
        host=host,
        port=port,
        log_level="info",
        # 默认开启访问日志（IP / 大小 / 耗时，写 data/logs/access.log，按大小轮转）
        # 不需要时设 ACCESS_LOG=0 关闭；日志上限用 ACCESS_LOG_MAX_MB / ACCESS_LOG_BACKUPS 控制
        # keep-alive 30s，减少连接反复重建
        access_log=os.environ.get("ACCESS_LOG", "1") != "0",
        timeout_keep_alive=30,
    )