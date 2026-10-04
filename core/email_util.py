"""邮件通知：仅向用户自己的收件邮箱发送；支持等级与开关。"""

from __future__ import annotations

import html
import json
import logging
import smtplib
import ssl
import threading
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Any

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
CONFIG_PATH = DATA_DIR / "email_config.json"

_lock = threading.Lock()
logger = logging.getLogger("auto-douyin-sparks-pro.email")

# 通知种类（管理员可关闭某类）
NOTIFY_TYPES = {
    "on_complete": "任务完成（正常）",
    "on_error": "发送异常 / 失败（紧急）",
    "on_captcha": "验证码 / 风控（紧急）",
    "on_logout": "账号登录失效（紧急）",
    "daily_digest": "每日汇总日志（正常）",
    "on_auth_expiry": "授权到期提醒（正常）",
}

DEFAULT_CONFIG = {
    "enabled": False,
    "smtp_host": "",
    "smtp_port": 465,
    "use_ssl": True,
    "username": "",
    "password": "",
    "from_addr": "",
    # 不再使用全局默认收件人，仅按用户邮箱发送
    "daily_log_time": "12:00",  # 每日汇总发送时间
    "notify": {
        "on_complete": True,
        "on_error": True,
        "on_captcha": True,
        "on_logout": True,
        "daily_digest": True,
        "on_auth_expiry": True,
    },
    "auth_expiry_remind_days": 3,  # 授权剩余 N 天时发提醒
}


def _ensure() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)


def load_email_config() -> dict:
    _ensure()
    cfg = dict(DEFAULT_CONFIG)
    cfg["notify"] = dict(DEFAULT_CONFIG["notify"])
    if not CONFIG_PATH.exists():
        return cfg
    try:
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return cfg
        for k in ("enabled", "smtp_host", "username", "password", "from_addr", "daily_log_time"):
            if k in data and data[k] is not None:
                cfg[k] = data[k]
        if "auth_expiry_remind_days" in data:
            try:
                cfg["auth_expiry_remind_days"] = max(1, min(int(data["auth_expiry_remind_days"]), 90))
            except (TypeError, ValueError):
                pass
        if "smtp_port" in data:
            try:
                cfg["smtp_port"] = int(data["smtp_port"])
            except (TypeError, ValueError):
                pass
        if "use_ssl" in data:
            cfg["use_ssl"] = bool(data["use_ssl"])
        notify = data.get("notify")
        if isinstance(notify, dict):
            for k in cfg["notify"]:
                if k in notify:
                    cfg["notify"][k] = bool(notify[k])
        return cfg
    except Exception:
        return cfg


def save_email_config(data: dict) -> dict:
    cfg = load_email_config()
    if "enabled" in data:
        cfg["enabled"] = bool(data["enabled"])
    for key in ("smtp_host", "username", "password", "from_addr", "daily_log_time"):
        if key in data and data[key] is not None:
            cfg[key] = str(data[key]).strip()
    if "auth_expiry_remind_days" in data:
        try:
            cfg["auth_expiry_remind_days"] = max(1, min(int(data["auth_expiry_remind_days"]), 90))
        except (TypeError, ValueError):
            pass
    if "smtp_port" in data:
        try:
            cfg["smtp_port"] = int(data["smtp_port"])
        except (TypeError, ValueError):
            cfg["smtp_port"] = 465
    if "use_ssl" in data:
        cfg["use_ssl"] = bool(data["use_ssl"])
    if "notify" in data and isinstance(data["notify"], dict):
        for k in cfg["notify"]:
            if k in data["notify"]:
                cfg["notify"][k] = bool(data["notify"][k])
    # 校验时间
    t = str(cfg.get("daily_log_time") or "12:00").strip()
    try:
        hh, mm = t.split(":")
        hh_i, mm_i = int(hh), int(mm)
        if not (0 <= hh_i <= 23 and 0 <= mm_i <= 59):
            raise ValueError
        cfg["daily_log_time"] = f"{hh_i:02d}:{mm_i:02d}"
    except Exception:
        cfg["daily_log_time"] = "12:00"
    _ensure()
    with _lock:
        CONFIG_PATH.write_text(
            json.dumps(cfg, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    return cfg


def is_notify_enabled(kind: str, cfg: dict | None = None) -> bool:
    cfg = cfg or load_email_config()
    if not cfg.get("enabled"):
        return False
    notify = cfg.get("notify") or {}
    return bool(notify.get(kind, True))


def send_email(
    subject: str,
    body_text: str,
    to_addrs: list[str],
    body_html: str | None = None,
    cfg: dict | None = None,
) -> None:
    """必须显式传入收件人列表，不再使用全局默认收件人。"""
    cfg = cfg or load_email_config()
    host = (cfg.get("smtp_host") or "").strip()
    port = int(cfg.get("smtp_port") or 465)
    user = (cfg.get("username") or "").strip()
    password = cfg.get("password") or ""
    from_addr = (cfg.get("from_addr") or user or "").strip()
    recipients = [str(x).strip() for x in (to_addrs or []) if str(x).strip()]

    if not host:
        raise ValueError("未配置 SMTP 服务器（请管理员在后台设置）")
    if not from_addr:
        raise ValueError("未配置发件人邮箱")
    if not recipients:
        raise ValueError("未指定收件人邮箱")
    if not password:
        raise ValueError("未配置邮箱密码/授权码")

    msg = MIMEMultipart("alternative")
    msg["From"] = from_addr
    msg["To"] = ", ".join(recipients)
    msg["Subject"] = subject
    msg.attach(MIMEText(body_text, "plain", "utf-8"))
    if body_html:
        msg.attach(MIMEText(body_html, "html", "utf-8"))

    use_ssl = bool(cfg.get("use_ssl", True))
    try:
        if use_ssl:
            context = ssl.create_default_context()
            with smtplib.SMTP_SSL(host, port, timeout=30, context=context) as server:
                if user:
                    server.login(user, password)
                server.sendmail(from_addr, recipients, msg.as_string())
        else:
            with smtplib.SMTP(host, port, timeout=30) as server:
                server.ehlo()
                try:
                    server.starttls(context=ssl.create_default_context())
                    server.ehlo()
                except Exception:
                    pass
                if user:
                    server.login(user, password)
                server.sendmail(from_addr, recipients, msg.as_string())
    except smtplib.SMTPAuthenticationError as e:
        raise ValueError(f"邮箱认证失败：{e}") from e
    except Exception as e:
        raise ValueError(f"发送邮件失败：{e}") from e


def _wrap_html(level: str, title: str, content_html: str) -> str:
    """浅色文字版模板（无黑底代码块）。"""
    if level == "urgent":
        badge = "紧急"
        badge_bg = "#ef4444"
        header_bg = "linear-gradient(135deg,#ef4444 0%,#f59e0b 100%)"
    else:
        badge = "正常"
        badge_bg = "#22c55e"
        header_bg = "linear-gradient(135deg,#2563eb 0%,#4f46e5 100%)"

    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"></head>
<body style="margin:0;padding:0;background:#f7f8fa;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI','Microsoft YaHei',sans-serif;">
  <table width="100%" cellpadding="0" cellspacing="0" style="background:#f7f8fa;padding:28px 12px;">
    <tr><td align="center">
      <table width="600" cellpadding="0" cellspacing="0" style="max-width:600px;background:#ffffff;border-radius:12px;overflow:hidden;border:1px solid #ebeef5;">
        <tr>
          <td style="background:{header_bg};padding:24px 28px;color:#fff;">
            <div style="font-size:13px;opacity:0.95;margin-bottom:6px;">
              <span style="display:inline-block;padding:2px 10px;border-radius:12px;background:rgba(255,255,255,0.25);">{badge}</span>
              <span style="margin-left:8px;">云逸续火花助手</span>
            </div>
            <div style="font-size:20px;font-weight:700;">{html.escape(title)}</div>
          </td>
        </tr>
        <tr>
          <td style="padding:24px 28px;color:#303133;font-size:14px;line-height:1.75;">
            {content_html}
          </td>
        </tr>
        <tr>
          <td style="padding:14px 28px 22px;color:#909399;font-size:12px;border-top:1px solid #ebeef5;">
            本邮件由系统自动发送，请勿直接回复。
          </td>
        </tr>
      </table>
    </td></tr>
  </table>
</body></html>"""


def _account_meta(account: str, douyin_name: str = "", douyin_id: str = "") -> str:
    parts = [f"系统账号：{account}"]
    if douyin_name:
        parts.append(f"抖音昵称：{douyin_name}")
    if douyin_id:
        parts.append(f"抖音号：{douyin_id}")
    return " ｜ ".join(parts)


def _account_meta_html(account: str, douyin_name: str = "", douyin_id: str = "") -> str:
    rows = [f"<div>系统账号：<strong>{html.escape(account)}</strong></div>"]
    if douyin_name:
        rows.append(f"<div>抖音昵称：<strong>{html.escape(douyin_name)}</strong></div>")
    if douyin_id:
        rows.append(f"<div>抖音号：<strong>{html.escape(douyin_id)}</strong></div>")
    return (
        '<div style="background:#f5f7fa;border-radius:8px;padding:12px 14px;margin:0 0 16px;color:#606266;font-size:13px;">'
        + "".join(rows)
        + "</div>"
    )


def send_test_to_sender() -> str:
    cfg = load_email_config()
    from_addr = (cfg.get("from_addr") or cfg.get("username") or "").strip()
    if not from_addr:
        raise ValueError("请先配置发件人邮箱")
    subject = "✅ 云逸续火花助手 · 邮件配置测试成功"
    text = (
        "你好！\n\n这是一封测试邮件。\n"
        "如果你能看到这封信，说明管理员配置的 SMTP 发信正常。\n\n"
        f"发件时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
    )
    content = f"""
      <p>你好！</p>
      <p>这是一封<strong>测试邮件</strong>。能收到说明 SMTP 配置已生效。</p>
      <p style="color:#67c23a;font-weight:600;">邮件通道工作正常</p>
      <p style="color:#909399;font-size:13px;">发件时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</p>
    """
    send_email(
        subject,
        text,
        to_addrs=[from_addr],
        body_html=_wrap_html("normal", "邮件配置测试", content),
        cfg=cfg,
    )
    return from_addr


def classify_result(result: dict) -> tuple[str, str]:
    """返回 (notify_kind, level)。kind 用于开关；level 为 urgent/normal。"""
    if result.get("logged_out"):
        return "on_logout", "urgent"
    if result.get("rate_limited"):
        return "on_captcha", "urgent"
    failed = result.get("failed") or []
    for item in failed:
        if not isinstance(item, dict):
            continue
        reason = str(item.get("reason") or "")
        name = str(item.get("name") or "")
        low = reason.lower()
        if name == "_system" or any(
            k in reason for k in ("验证码", "风控", "限流", "captcha", "验证")
        ):
            return "on_captcha" if ("验证" in reason or "captcha" in low) else "on_error", "urgent"
        if any(k in reason for k in ("登录", "失效", "过期", "掉线")):
            return "on_logout", "urgent"
    ok = result.get("ok") or []
    if result.get("stopped") and not ok:
        return "on_error", "urgent"
    if failed and not ok:
        return "on_error", "urgent"
    if failed:
        return "on_error", "urgent"
    return "on_complete", "normal"


def build_run_summary_email(
    account: str,
    username: str,
    result: dict,
    log_text: str,
    date: str,
    douyin_name: str = "",
    douyin_id: str = "",
) -> tuple[str, str, str, str]:
    """返回 subject, plain, html, level。"""
    kind, level = classify_result(result)
    ok_list = [x for x in (result.get("ok") or []) if isinstance(x, str)]
    failed = result.get("failed") or []
    fail_items = [f for f in failed if isinstance(f, dict) and f.get("name") != "_system"]
    system_fails = [f for f in failed if isinstance(f, dict) and f.get("name") == "_system"]
    ok_n = len(ok_list)
    fail_n = len(fail_items)
    stop_reason = result.get("stop_reason") or ""
    dry = bool(result.get("dry_run"))

    if level == "urgent":
        if result.get("logged_out"):
            status = "账号登录失效"
        elif result.get("rate_limited"):
            status = "疑似验证码 / 风控"
        elif system_fails:
            status = "系统异常"
        else:
            status = "任务异常"
    else:
        status = "任务已完成"

    ok_names = "、".join(ok_list[:25]) if ok_list else "无"
    if len(ok_list) > 25:
        ok_names += f" 等共 {ok_n} 人"

    fail_lines = []
    for item in fail_items[:20]:
        fail_lines.append(f"· {item.get('name')}: {item.get('reason') or '未知原因'}")
    for item in system_fails[:5]:
        fail_lines.append(f"· 系统: {item.get('reason') or '未知原因'}")
    fail_text = "\n".join(fail_lines) if fail_lines else "无"

    # 日志摘要：纯文字，截断，不用黑底
    log_body = (log_text or "").strip()
    if len(log_body) > 3500:
        log_body = log_body[-3500:]
        log_body = "……（仅保留当日末尾日志）\n" + log_body
    if not log_body:
        log_body = "（当日暂无运行日志）"

    meta = _account_meta(account, douyin_name, douyin_id)
    subject = f"[{'紧急' if level == 'urgent' else '正常'}] 续火花 · {status} · {account} · {date}"

    plain_parts = [
        f"你好，{username}！",
        "",
        f"【{status}】",
        meta,
        f"日期：{date}",
        f"成功：{ok_n} 人　　失败：{fail_n} 人",
    ]
    if dry:
        plain_parts.append("说明：本次为干跑测试，未真实发送。")
    if stop_reason:
        plain_parts.append(f"停止原因：{stop_reason}")
    plain_parts.extend(
        [
            "",
            "成功好友：",
            ok_names,
            "",
            "失败详情：",
            fail_text,
            "",
            "—— 当日日志摘要 ——",
            log_body,
            "",
        ]
    )
    plain = "\n".join(plain_parts)

    log_html = html.escape(log_body).replace("\n", "<br>\n")
    fail_html = "<br>\n".join(html.escape(x) for x in fail_lines) if fail_lines else "无"
    status_color = "#f56c6c" if level == "urgent" else "#67c23a"

    content = (
        f"<p>你好，<strong>{html.escape(username)}</strong>！</p>"
        + _account_meta_html(account, douyin_name, douyin_id)
        + f'<p style="margin:0 0 12px;"><span style="color:{status_color};font-weight:700;font-size:16px;">{html.escape(status)}</span>'
        f'　　<span style="color:#606266;">成功 <strong style="color:#67c23a;">{ok_n}</strong>　失败 <strong style="color:#f56c6c;">{fail_n}</strong></span></p>'
        + (f'<p style="color:#e6a23c;">停止原因：{html.escape(stop_reason)}</p>' if stop_reason else "")
        + (f'<p style="color:#909399;">本次为干跑测试，未真实发送。</p>' if dry else "")
        + f'<p style="margin:16px 0 4px;color:#909399;font-size:12px;">成功好友</p>'
        f"<p>{html.escape(ok_names)}</p>"
        f'<p style="margin:16px 0 4px;color:#909399;font-size:12px;">失败详情</p>'
        f"<p>{fail_html}</p>"
        f'<p style="margin:20px 0 8px;color:#909399;font-size:12px;">当日日志摘要（{html.escape(date)}）</p>'
        f'<div style="background:#fafbfc;border:1px solid #e4e7ed;border-radius:8px;padding:14px 16px;'
        f'color:#606266;font-size:13px;line-height:1.65;white-space:pre-wrap;word-break:break-word;">'
        f"{log_html}</div>"
    )
    return subject, plain, _wrap_html(level, status, content), level


def send_run_report(
    account: str,
    username: str,
    to_email: str,
    result: dict,
    log_text: str,
    date: str | None = None,
    douyin_name: str = "",
    douyin_id: str = "",
    force_kind: str | None = None,
    force_send: bool = False,
) -> str | None:
    """
    发送任务报告。返回实际发送的 kind，未发送返回 None。
    force_send=True：手动立即发送完成后强制发信，忽略分类开关。
    """
    cfg = load_email_config()
    if not cfg.get("enabled"):
        return None
    if result.get("dry_run"):
        return None

    kind, _level = classify_result(result)
    if force_kind:
        kind = force_kind
    if not force_send and not is_notify_enabled(kind, cfg):
        logger.info("通知类型 %s 已关闭，跳过发送", kind)
        return None

    date = date or datetime.now().strftime("%Y-%m-%d")
    subject, plain, html_body, _ = build_run_summary_email(
        account, username, result, log_text, date, douyin_name, douyin_id
    )
    send_email(subject, plain, to_addrs=[to_email], body_html=html_body, cfg=cfg)
    return kind


def build_daily_digest(
    account: str,
    username: str,
    date: str,
    log_text: str,
    runtime: dict | None = None,
    douyin_name: str = "",
    douyin_id: str = "",
) -> tuple[str, str, str]:
    rt = runtime or {}
    last = rt.get("last_run") or {}
    ok_n = len(last.get("ok") or []) if isinstance(last, dict) else 0
    fail_n = len([x for x in (last.get("failed") or []) if isinstance(x, dict) and x.get("name") != "_system"]) if isinstance(last, dict) else 0
    session = rt.get("session_status") or "unknown"
    status_map = {
        "ok": "正常",
        "failed": "异常",
        "logged_out": "未登录",
        "rate_limited": "风控中",
        "unknown": "未知",
    }
    session_cn = status_map.get(str(session), str(session))

    log_body = (log_text or "").strip() or "（当日暂无运行日志）"
    if len(log_body) > 4000:
        log_body = "……（仅保留末尾）\n" + log_body[-4000:]

    meta = _account_meta(account, douyin_name, douyin_id)
    subject = f"[正常] 每日日志汇总 · {account} · {date}"
    plain = "\n".join(
        [
            f"你好，{username}！",
            "",
            "这是今日续火花日志汇总。",
            meta,
            f"日期：{date}",
            f"会话状态：{session_cn}",
            f"最近一次任务：成功 {ok_n} / 失败 {fail_n}",
            "",
            "—— 当日日志 ——",
            log_body,
            "",
        ]
    )
    content = (
        f"<p>你好，<strong>{html.escape(username)}</strong>！</p>"
        f"<p>这是<strong>今日续火花日志汇总</strong>。</p>"
        + _account_meta_html(account, douyin_name, douyin_id)
        + f'<p style="color:#606266;">会话状态：<strong>{html.escape(session_cn)}</strong>　　'
        f"最近一次：成功 <strong style=\"color:#67c23a;\">{ok_n}</strong> / "
        f"失败 <strong style=\"color:#f56c6c;\">{fail_n}</strong></p>"
        f'<p style="margin:18px 0 8px;color:#909399;font-size:12px;">当日日志（{html.escape(date)}）</p>'
        f'<div style="background:#fafbfc;border:1px solid #e4e7ed;border-radius:8px;padding:14px 16px;'
        f'color:#606266;font-size:13px;line-height:1.65;white-space:pre-wrap;word-break:break-word;">'
        f"{html.escape(log_body).replace(chr(10), '<br>')}</div>"
    )
    return subject, plain, _wrap_html("normal", "每日日志汇总", content)


def send_daily_digest(
    account: str,
    username: str,
    to_email: str,
    log_text: str,
    runtime: dict | None = None,
    douyin_name: str = "",
    douyin_id: str = "",
    date: str | None = None,
) -> None:
    cfg = load_email_config()
    if not is_notify_enabled("daily_digest", cfg):
        return
    date = date or datetime.now().strftime("%Y-%m-%d")
    subject, plain, html_body = build_daily_digest(
        account, username, date, log_text, runtime, douyin_name, douyin_id
    )
    send_email(subject, plain, to_addrs=[to_email], body_html=html_body, cfg=cfg)
