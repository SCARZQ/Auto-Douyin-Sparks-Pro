"""站点级设置：公告、联系客服等"""
from __future__ import annotations

import json
import threading
from pathlib import Path
from datetime import datetime

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
SETTINGS_PATH = DATA_DIR / "site_settings.json"

_lock = threading.Lock()

DEFAULT_HELP_CONTENT = '【快速上手 · 4 步】\n\n① 登录抖音\n   打开底部「凭证」→ 点「网页登录」扫码，\n   或点「上传抖音登录态」选择本机导出的 state.json。\n   看到绿色「已登录」即成功。\n\n② 获取并勾选好友\n   打开底部「好友」→ 点「获取聊天列表」等待读取 →\n   勾选需要续火花的好友 → 点「保存勾选与消息」。\n   注意：好友只能从列表勾选，不能手动输入。\n\n③ 设置发送时间\n   打开底部「定时」→ 设置每天发送时间（如 09:30）→\n   建议「时间抖动窗口」设为 5～15 分钟 → 点「保存定时设置」。\n\n④ 查看运行结果\n   「概览」看状态与时间，「日志」看每次发送的明细。\n\n\n【各页面说明】\n\n· 概览\n  显示抖音头像 / 昵称 / 抖音号、是否占用续火花、\n  上次与下次发送时间、授权到期日期。\n  「使用卡密」可自助续期；\n  按钮：立即发送 / 干跑测试 / 强制停止。\n\n· 凭证\n  管理抖音登录状态。登录态失效时来这里重新登录。\n\n· 好友（好友与消息）\n  读取聊天列表、勾选好友、设置消息模板、配置 AI 文案。\n\n· 定时\n  设置每天发送时间、抖动窗口、好友间隔、单次上限。\n\n· 日志\n  查看运行明细，可按天查看，支持自动刷新与发送到邮箱。\n\n· 我的（个人中心）\n  查看账号与等级、修改用户名 / 密码 / 收件邮箱、退出登录。\n\n\n【AI 文案怎么用（可选）】\n\n1. 在「好友」页打开「启用 AI 文案」\n2. 填 API 地址（到 /v1 为止，不要带 /chat/completions）\n3. 填 API Key（sk- 开头）\n4. 填模型（如 gpt-4o-mini）\n5. 写系统提示词\n6. 点「测试 API 连通」→「测试提示词」预览\n7. 没问题后点「保存勾选与消息」\n\nAI 调用失败会自动回退到消息模板，不会发不出去。\n\n\n【授权与续期】\n\n· 独立模式：无到期限制\n· 授权模式：到期后自动停用\n\n续期：拿到卡密后，在「概览 → 使用卡密」输入并兑换，\n天数会叠加到现有到期时间上。\n\n卡密开头数字 = 天数：\n  1… = 1 天      3… = 3 天      7… = 7 天\n  15… = 15 天    30… = 1 个月   90… = 3 个月\n  180… = 6 个月  365… = 1 年\n\n\n【常见问题】\n\nQ：提示「未登录」或「登录态已失效」？\nA：登录态过期。到「凭证」重新扫码登录或重新上传 state.json。\n\nQ：显示「当前账号尚未登录抖音，无法进行任务」？\nA：同上，先去「凭证」完成抖音登录。\n\nQ：显示验证码 / 操作频繁？\nA：系统检测到风控会自动停止本轮（保护机制）。\n   请次日再试，不要频繁重试。\n\nQ：发送时间不准、延迟了？\nA：正常现象。发送时间有随机抖动设计，加上网络因素，\n   实际可能比设定值晚几分钟到几十分钟。\n\nQ：好友列表是空的？\nA：点「获取聊天列表」重新读取；\n   若失败，检查抖音登录态是否有效。\n\nQ：收不到邮件？\nA：①「我的 → 收件邮箱」是否填写；\n   ② 管理员是否已配置发件邮箱。\n\nQ：怎么改登录账号名？\nA：到「我的 → 修改账号用户名」。\n   改名后账号目录会同步改名，不影响数据。\n\n\n【使用建议】\n\n· 仅限本人账号、少量好友自用，请勿用于营销或对外服务\n· 好友数量控制在少量，发送间隔不要太短\n· 登录态尽量在常用网络环境获取，减少异地风控\n· 出现验证码 / 操作频繁时停止本轮，次日再试\n· 不要把 data 目录或登录态文件分享给任何人\n\n\n【风险提示】\n\n本系统为第三方辅助工具，与抖音官方无任何关联。\n自动化操作可能触发平台风控，存在限流、功能限制甚至封号风险，\n因使用本系统产生的账号风险由使用者自行承担。\n请遵守抖音平台的服务协议与社区规范。\n'


DEFAULT = {
    "spark_runtime": {
        "max_parallel": 1,  # 全站同时续火花账号数，1=串行
    },
    # 外观：背景动态效果（多种款式，管理员可选）
    "appearance": {
        "bg_effect": "flow",      # none | flow | constellation | waves | aurora | grid | beams
        "bg_speed": 1.0,          # 0.3 ~ 2.5 倍速
        "bg_opacity": 1.0,        # 0.2 ~ 1.0 强度
        "bg_color": "",           # 自定义主色（空=跟随主题蓝）
        "bg_dark": False,         # 暗色底（虚化背景用）
        # ===== 首页（控制台）界面方案，管理员可在后台选择 =====
        "home_ui": "classic",     # classic | glass | split | focus | minimal | soft | outline | paper
        "home_accent": "",        # 首页强调色（空=跟随主题蓝）
        "home_radius": 14,        # 卡片圆角 px，0~24
        "home_density": "normal", # normal | compact  （信息密度）
        "home_anim": "fade",      # none | fade | slide | pop | reveal（滚动进场动画）
    },
    "invite_settings": {
        "default_auth_days": 1,  # 注册码默认赠送授权天数
    },
    "login_methods": {
        "web_login": True,      # 网页扫码登录抖音
        "upload_state": True,   # 上传 state.json
        "tutorial_url": "",     # 上传登录态教程链接（管理员可改）
    },
    # 扫码登录并行限制：同时最多几个账号在扫码登录
    "login_runtime": {
        "max_parallel": 2,
    },
    # 扫码前提示：建议本地登录（服务器 IP 登录易触发风控/掉登录）
    "login_advice": {
        "enabled": True,
        "title": "建议使用本地登录",
        "content": (
            "服务器 IP 登录的抖音账号更容易触发风控，登录态会频繁失效。\n"
            "建议在本机运行 extract_cookie.py 导出登录态后，在「凭证 → 登录态登录」上传（更稳定）。\n"
            "如仍要使用网页扫码，请点击下方按钮继续。"
        ),
        "confirm_btn": "我已确认，使用手机扫码登录",
        "local_btn": "使用本地登录",
    },
    "announcement": {
        "enabled": False,
        "title": "系统公告",
        "content": "",
        "level": "info",  # info | success | warning | error
        "updated_at": None,
        # ===== 高度自定义样式 =====
        "bg_color": "",          # 弹窗背景色（空=按 level 默认）
        "text_color": "",        # 正文颜色
        "title_color": "",       # 标题颜色
        "accent_color": "",      # 强调/按钮颜色
        "font_size": 14,         # 正文字号 px
        "title_size": 16,        # 标题字号 px
        "line_height": 1.75,     # 行高
        "align": "left",         # left | center
        "radius": 12,            # 圆角 px
        "width": 420,            # 弹窗宽度 px
        "btn_text": "我知道了",   # 确认按钮文字
        # 正文格式（富文本工具条）
        "bold": False,
        "italic": False,
        "underline": False,
        # 可点击链接
        "link": "",              # 公告里的跳转链接
        "link_text": "了解更多",  # 链接按钮文字
    },
    # 卡密购买入口（显示在「使用卡密」弹窗里）
    "card_shop": {
        "url": "",                  # 发卡网地址，留空则不显示
        "text": "去购买卡密",        # 按钮文字
        "tip": "",                  # 按钮上方的小提示
        "show_in_redeem": True,     # 是否在「使用卡密」弹窗显示
    },
    # 使用帮助（管理员可在站点管理里编辑）
    "help": {
        "enabled": True,
        "title": "使用帮助",
        "content": DEFAULT_HELP_CONTENT,
        "link": "",
        "link_text": "查看详细教程",
        "bg_color": "",
        "text_color": "",
        "font_size": 14,
        "width": 560,
        "bold": False,
        "italic": False,
        "underline": False,
    },
    "register_help": {
        "enabled": False,
        "title": "获取注册码",
        "tip": "为防止滥用、保障服务器稳定运行，注册码需向管理员获取。",
        "content": "",
        "link": "",              # 点击后打开的链接（新标签页）
        "btn_text": "获取注册码",
        "link_btn_text": "打开链接",
        "bg_color": "",
        "text_color": "",
        "accent_color": "",
        "font_size": 14,
        "width": 440,
    },
    "customer_service": {
        "enabled": True,
        "title": "联系客服",
        "qq": "",
        "wechat": "",
        "telegram": "",
        "email": "",
        "phone": "",
        "extra": "",  # 补充说明
        "link": "",   # 整体跳转链接（可选）
        # 每个联系方式可单独配一个跳转链接，并独立开关
        "links": {
            "qq":       {"url": "", "enabled": True, "text": "打开链接"},
            "wechat":   {"url": "", "enabled": True, "text": "打开链接"},
            "telegram": {"url": "", "enabled": True, "text": "打开链接"},
            "email":    {"url": "", "enabled": True, "text": "打开链接"},
            "phone":    {"url": "", "enabled": True, "text": "打开链接"},
        },
    },
}


def _ensure() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)


def _clamp_int(v, lo, hi, default):
    try:
        return max(lo, min(int(v), hi))
    except (TypeError, ValueError):
        return default


def _clamp_float(v, lo, hi, default):
    try:
        return max(lo, min(float(v), hi))
    except (TypeError, ValueError):
        return default


def _clean_url(v: str) -> str:
    """只允许 http/https 链接，其它一律清空（防 javascript: 等注入）。"""
    s = str(v or "").strip()
    if not s:
        return ""
    low = s.lower()
    if low.startswith("http://") or low.startswith("https://"):
        return s[:500]
    return ""


def _clean_hex(v: str, default: str = "") -> str:
    """只允许空串或 #RGB / #RRGGBB，防止注入。"""
    s = str(v or "").strip()
    if not s:
        return default
    import re as _re
    if _re.fullmatch(r"#[0-9a-fA-F]{3}", s) or _re.fullmatch(r"#[0-9a-fA-F]{6}", s):
        return s.lower()
    return default


def _sanitize_announcement(a: dict) -> dict:
    a["enabled"] = bool(a.get("enabled", False))
    a["title"] = str(a.get("title") or "系统公告")[:60]
    a["content"] = str(a.get("content") or "")
    if a.get("level") not in ("info", "success", "warning", "error"):
        a["level"] = "info"
    a["font_size"] = _clamp_int(a.get("font_size"), 10, 40, 14)
    a["title_size"] = _clamp_int(a.get("title_size"), 12, 48, 16)
    a["line_height"] = _clamp_float(a.get("line_height"), 1.0, 3.0, 1.75)
    a["radius"] = _clamp_int(a.get("radius"), 0, 40, 12)
    a["width"] = _clamp_int(a.get("width"), 300, 900, 420)
    if a.get("align") not in ("left", "center", "right"):
        a["align"] = "left"
    a["bg_color"] = _clean_hex(a.get("bg_color"))
    a["text_color"] = _clean_hex(a.get("text_color"))
    a["title_color"] = _clean_hex(a.get("title_color"))
    a["accent_color"] = _clean_hex(a.get("accent_color"))
    a["btn_text"] = str(a.get("btn_text") or "我知道了").strip()[:20]
    a["bold"] = bool(a.get("bold", False))
    a["italic"] = bool(a.get("italic", False))
    a["underline"] = bool(a.get("underline", False))
    a["link"] = _clean_url(a.get("link"))
    a["link_text"] = str(a.get("link_text") or "了解更多").strip()[:20]
    return a


def _sanitize_card_shop(c: dict) -> dict:
    c["url"] = _clean_url(c.get("url"))
    c["text"] = str(c.get("text") or "去购买卡密").strip()[:20]
    c["tip"] = str(c.get("tip") or "").strip()[:120]
    c["show_in_redeem"] = bool(c.get("show_in_redeem", True))
    return c


_CS_LINK_FIELDS = ("qq", "wechat", "telegram", "email", "phone")


def _sanitize_cs_links(raw) -> dict:
    """每个联系方式的跳转链接：地址必须过 _clean_url，并保留独立开关。"""
    out = {}
    raw = raw if isinstance(raw, dict) else {}
    for f in _CS_LINK_FIELDS:
        item = raw.get(f)
        item = item if isinstance(item, dict) else {}
        out[f] = {
            "url": _clean_url(item.get("url")),
            "enabled": bool(item.get("enabled", True)),
            "text": str(item.get("text") or "打开链接").strip()[:16] or "打开链接",
        }
    return out


def _sanitize_help(h: dict) -> dict:
    h["enabled"] = bool(h.get("enabled", True))
    h["title"] = str(h.get("title") or "使用帮助").strip()[:60]
    h["content"] = str(h.get("content") or "")[:8000]
    h["link"] = _clean_url(h.get("link"))
    h["link_text"] = str(h.get("link_text") or "查看详细教程").strip()[:20]
    h["font_size"] = _clamp_int(h.get("font_size"), 10, 30, 14)
    h["width"] = _clamp_int(h.get("width"), 300, 1000, 560)
    h["bg_color"] = _clean_hex(h.get("bg_color"))
    h["text_color"] = _clean_hex(h.get("text_color"))
    h["bold"] = bool(h.get("bold", False))
    h["italic"] = bool(h.get("italic", False))
    h["underline"] = bool(h.get("underline", False))
    return h


def _sanitize_register_help(h: dict) -> dict:
    h["enabled"] = bool(h.get("enabled", False))
    h["tip"] = str(h.get("tip") or "")[:500]
    h["content"] = str(h.get("content") or "")[:4000]
    h["link"] = _clean_url(h.get("link"))
    h["font_size"] = _clamp_int(h.get("font_size"), 10, 30, 14)
    h["width"] = _clamp_int(h.get("width"), 300, 900, 440)
    h["bg_color"] = _clean_hex(h.get("bg_color"))
    h["text_color"] = _clean_hex(h.get("text_color"))
    h["accent_color"] = _clean_hex(h.get("accent_color"))
    h["btn_text"] = str(h.get("btn_text") or "获取注册码").strip()[:20]
    h["link_btn_text"] = str(h.get("link_btn_text") or "打开链接").strip()[:20]
    h["title"] = str(h.get("title") or "获取注册码").strip()[:40]
    return h


def _sanitize_appearance(a: dict) -> dict:
    allowed = ("none", "flow", "constellation", "waves", "aurora", "grid", "particles", "beams")
    if a.get("bg_effect") not in allowed:
        a["bg_effect"] = "flow"
    # 只保留部署默认方案，其余外观模板已移除
    if a.get("home_ui") != "classic":
        a["home_ui"] = "classic"
    if a.get("home_density") not in ("normal", "compact"):
        a["home_density"] = "normal"
    if a.get("home_anim") not in ("none", "fade", "slide", "pop", "reveal"):
        a["home_anim"] = "fade"
    a["home_accent"] = _clean_hex(a.get("home_accent"))
    a["home_radius"] = _clamp_int(a.get("home_radius"), 0, 24, 14)
    a["bg_speed"] = _clamp_float(a.get("bg_speed"), 0.3, 2.5, 1.0)
    a["bg_opacity"] = _clamp_float(a.get("bg_opacity"), 0.2, 1.0, 1.0)
    a["bg_color"] = _clean_hex(a.get("bg_color"))
    a["bg_dark"] = bool(a.get("bg_dark", False))
    return a


def load() -> dict:
    _ensure()
    with _lock:
        if not SETTINGS_PATH.exists():
            return json.loads(json.dumps(DEFAULT))
        try:
            data = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
        except Exception:
            return json.loads(json.dumps(DEFAULT))
        out = json.loads(json.dumps(DEFAULT))

        if isinstance(data.get("appearance"), dict):
            out["appearance"].update({
                k: data["appearance"].get(k, v)
                for k, v in out["appearance"].items()
            })
        out["appearance"] = _sanitize_appearance(out["appearance"])

        if isinstance(data.get("announcement"), dict):
            out["announcement"].update({k: data["announcement"].get(k, v) for k, v in out["announcement"].items()})
        out["announcement"] = _sanitize_announcement(out["announcement"])

        if isinstance(data.get("help"), dict):
            out["help"].update({k: data["help"].get(k, v) for k, v in out["help"].items()})
        out["help"] = _sanitize_help(out["help"])

        if isinstance(data.get("card_shop"), dict):
            out["card_shop"].update({k: data["card_shop"].get(k, v)
                                     for k, v in out["card_shop"].items()})
        out["card_shop"] = _sanitize_card_shop(out["card_shop"])

        if isinstance(data.get("register_help"), dict):
            out["register_help"].update({k: data["register_help"].get(k, v) for k, v in out["register_help"].items()})
        out["register_help"] = _sanitize_register_help(out["register_help"])

        if isinstance(data.get("customer_service"), dict):
            out["customer_service"].update({k: data["customer_service"].get(k, v) for k, v in out["customer_service"].items()})
        if isinstance(data.get("login_runtime"), dict):
            try:
                mp = int(data["login_runtime"].get("max_parallel", 2))
            except (TypeError, ValueError):
                mp = 2
            out["login_runtime"]["max_parallel"] = max(1, min(mp, 10))
        if isinstance(data.get("login_advice"), dict):
            src = data["login_advice"]
            out["login_advice"]["enabled"] = bool(src.get("enabled", True))
            out["login_advice"]["title"] = str(src.get("title") or "")[:60]
            out["login_advice"]["content"] = str(src.get("content") or "")[:600]
            out["login_advice"]["confirm_btn"] = str(src.get("confirm_btn") or "")[:30]
            out["login_advice"]["local_btn"] = str(src.get("local_btn") or "")[:30]
        if isinstance(data.get("login_methods"), dict):
            src = data["login_methods"]
            out["login_methods"]["web_login"] = bool(src.get("web_login", True))
            out["login_methods"]["upload_state"] = bool(src.get("upload_state", True))
            out["login_methods"]["tutorial_url"] = str(src.get("tutorial_url") or "")
        if isinstance(data.get("spark_runtime"), dict):
            try:
                mp = int(data["spark_runtime"].get("max_parallel", 1))
            except (TypeError, ValueError):
                mp = 1
            out["spark_runtime"]["max_parallel"] = max(1, min(mp, 50))
        if isinstance(data.get("invite_settings"), dict):
            try:
                d = int(data["invite_settings"].get("default_auth_days", 1))
            except (TypeError, ValueError):
                d = 1
            out["invite_settings"]["default_auth_days"] = max(0, min(d, 3650))
        return out


def save(data: dict) -> dict:
    _ensure()
    current = load()

    if isinstance(data.get("appearance"), dict):
        current.setdefault("appearance", json.loads(json.dumps(DEFAULT["appearance"])))
        current["appearance"].update(data["appearance"])
    current["appearance"] = _sanitize_appearance(current.get("appearance") or {})

    if isinstance(data.get("announcement"), dict):
        current["announcement"].update(data["announcement"])
        current["announcement"]["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    current["announcement"] = _sanitize_announcement(current.get("announcement") or {})

    if isinstance(data.get("help"), dict):
        current.setdefault("help", json.loads(json.dumps(DEFAULT["help"])))
        current["help"].update(data["help"])
    current["help"] = _sanitize_help(current.get("help") or {})

    if isinstance(data.get("card_shop"), dict):
        current.setdefault("card_shop", json.loads(json.dumps(DEFAULT["card_shop"])))
        current["card_shop"].update(data["card_shop"])
    current["card_shop"] = _sanitize_card_shop(current.get("card_shop") or {})

    if isinstance(data.get("register_help"), dict):
        current.setdefault("register_help", json.loads(json.dumps(DEFAULT["register_help"])))
        current["register_help"].update(data["register_help"])
    current["register_help"] = _sanitize_register_help(current.get("register_help") or {})

    if isinstance(data.get("login_runtime"), dict):
        current.setdefault("login_runtime", json.loads(json.dumps(DEFAULT["login_runtime"])))
        current["login_runtime"].update(data["login_runtime"])
        try:
            mp = int(current["login_runtime"].get("max_parallel", 2))
        except (TypeError, ValueError):
            mp = 2
        current["login_runtime"]["max_parallel"] = max(1, min(mp, 10))
    if isinstance(data.get("login_advice"), dict):
        current.setdefault("login_advice", json.loads(json.dumps(DEFAULT["login_advice"])))
        current["login_advice"].update(data["login_advice"])
        la = current["login_advice"]
        la["enabled"] = bool(la.get("enabled", True))
        la["title"] = str(la.get("title") or "")[:60]
        la["content"] = str(la.get("content") or "")[:600]
        la["confirm_btn"] = str(la.get("confirm_btn") or "")[:30]
        la["local_btn"] = str(la.get("local_btn") or "")[:30]
    if isinstance(data.get("customer_service"), dict):
        current["customer_service"].update(data["customer_service"])
        current["customer_service"]["link"] = _clean_url(
            current["customer_service"].get("link"))
        current["customer_service"]["links"] = _sanitize_cs_links(
            current["customer_service"].get("links"))
    if isinstance(data.get("login_methods"), dict):
        lm = current.setdefault("login_methods", {"web_login": True, "upload_state": True, "tutorial_url": ""})
        if "web_login" in data["login_methods"]:
            lm["web_login"] = bool(data["login_methods"]["web_login"])
        if "upload_state" in data["login_methods"]:
            lm["upload_state"] = bool(data["login_methods"]["upload_state"])
        if "tutorial_url" in data["login_methods"]:
            lm["tutorial_url"] = str(data["login_methods"].get("tutorial_url") or "").strip()
    if isinstance(data.get("spark_runtime"), dict):
        sr = current.setdefault("spark_runtime", {"max_parallel": 1})
        if "max_parallel" in data["spark_runtime"]:
            try:
                sr["max_parallel"] = max(1, min(int(data["spark_runtime"]["max_parallel"]), 50))
            except (TypeError, ValueError):
                pass
    if isinstance(data.get("invite_settings"), dict):
        inv = current.setdefault("invite_settings", {"default_auth_days": 1})
        if "default_auth_days" in data["invite_settings"]:
            try:
                inv["default_auth_days"] = max(0, min(int(data["invite_settings"]["default_auth_days"]), 3650))
            except (TypeError, ValueError):
                pass
    with _lock:
        SETTINGS_PATH.write_text(
            json.dumps(current, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    return current


def public_view() -> dict:
    """登录用户可见的公开信息"""
    s = load()
    lm = s.get("login_methods") or {}
    return {
        "announcement": s["announcement"],
        "help": s.get("help") or {},
        "card_shop": s.get("card_shop") or {},
        "appearance": s.get("appearance") or {},
        "register_help": s.get("register_help") or {},
        "customer_service": {
            "enabled": bool(s["customer_service"].get("enabled")),
            "title": s["customer_service"].get("title") or "联系客服",
            "qq": s["customer_service"].get("qq") or "",
            "wechat": s["customer_service"].get("wechat") or "",
            "telegram": s["customer_service"].get("telegram") or "",
            "email": s["customer_service"].get("email") or "",
            "phone": s["customer_service"].get("phone") or "",
            "extra": s["customer_service"].get("extra") or "",
            "link": s["customer_service"].get("link") or "",
        "links": _sanitize_cs_links(s["customer_service"].get("links")),
        },
        "login_advice": s.get("login_advice") or {},
        "login_methods": {
            "web_login": bool(lm.get("web_login", True)),
            "upload_state": bool(lm.get("upload_state", True)),
            "tutorial_url": str(lm.get("tutorial_url") or ""),
        },
    }


def register_help_view() -> dict:
    """注册页可见（无需登录）：注册码获取引导 + 背景动效。"""
    s = load()
    h = dict(s.get("register_help") or {})
    a = s.get("announcement") or {}
    # 公告也一并给出，注册前也能展示（按需开启）
    h["_announcement"] = {
        "enabled": bool(a.get("enabled")),
        "title": a.get("title") or "",
        "content": a.get("content") or "",
        "level": a.get("level") or "info",
    }
    # 背景动效：未登录也要能渲染
    h["_appearance"] = s.get("appearance") or {}
    return h
