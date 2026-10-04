"""Playwright 自动化：在抖音网页版私信页面给指定好友发送消息。

发送逻辑：
- 点击联系人后校验右侧会话确实切换；
- 列表点击失败时使用搜索框兜底；
- 检测登录失效、扫码登录、登录过期；
- 检测操作频繁、安全验证等限制；
- 发现登录异常立即停止；
- 发现疑似限流立即停止本轮；
- 发送失败且无法确认原因时，按安全策略视为疑似限流并停止；
- 不连续重试发送。
"""

from __future__ import annotations

import json
import logging
import random
import re
import time
from datetime import datetime
from pathlib import Path

import requests
from playwright.sync_api import sync_playwright

# AI 续火花文案默认系统提示词（与 core/config.py 的 DEFAULT_CONFIG 保持一致）
_DEFAULT_AI_PROMPT = (
    "你是一个友好的社交助手，请根据好友昵称生成一句简短自然续火花的中文问候，加上当前中国时间自动续火花，用于抖音私信续火花，24字以内，不要用引号，开放一点，头上加上抖音自动续火花"
)

def generate_ai_message(cfg: dict, friend_name: str = "") -> str | None:
    """调用 OpenAI 兼容接口生成续火花文案；失败返回 None。"""
    import urllib.request

    api_key = (cfg.get("ai_api_key") or "").strip()
    if not api_key:
        return None
    base = (cfg.get("ai_api_base") or "https://api.openai.com/v1").strip().rstrip("/")
    model = (cfg.get("ai_model") or "gpt-4o-mini").strip()
    system = (cfg.get("ai_system_prompt") or "").strip() or (
        _DEFAULT_AI_PROMPT
    )
    # 默认提示词要求「加上当前中国时间」，必须把真实时间传给模型，
    # 否则模型只能瞎编一个时间
    try:
        from zoneinfo import ZoneInfo
        now_text = datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d %H:%M")
    except Exception:
        now_text = datetime.now().strftime("%Y-%m-%d %H:%M")
    user = (
        f"好友昵称：{friend_name or '朋友'}。当前时间：{now_text}（北京时间）。"
        "请生成一句续火花问候。"
    )
    url = base + "/chat/completions"
    payload = json.dumps({
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": 0.9,
        "max_tokens": 60,
    }).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="ignore"))
        choices = data.get("choices") or []
        if not choices:
            return None
        content = (choices[0].get("message") or {}).get("content") or ""
        content = str(content).strip().strip('"').strip("'")
        content = " ".join(content.split())
        if len(content) > 40:
            content = content[:40]
        return content or None
    except Exception as exp:
        logging.warning("【AI】调用接口失败 model=%s url=%s err=%s", model, url, exp)
        return None


def test_ai_api(cfg: dict) -> dict:
    """测试 AI 接口是否连通，返回 {ok, message, sample, latency_ms, error}。"""
    import urllib.request
    import urllib.error
    import time as _time

    api_key = (cfg.get("ai_api_key") or "").strip()
    if not api_key:
        return {"ok": False, "error": "未填写 API Key"}
    base = (cfg.get("ai_api_base") or "https://api.openai.com/v1").strip().rstrip("/")
    model = (cfg.get("ai_model") or "gpt-4o-mini").strip()
    system = (cfg.get("ai_system_prompt") or "").strip() or (
        _DEFAULT_AI_PROMPT
    )
    url = base + "/chat/completions"
    payload = json.dumps({
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": "好友昵称：测试用户。请生成一句续火花问候。"},
        ],
        "temperature": 0.7,
        "max_tokens": 60,
    }).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        method="POST",
    )
    t0 = _time.time()
    try:
        with urllib.request.urlopen(req, timeout=25) as resp:
            raw = resp.read().decode("utf-8", errors="ignore")
            status = getattr(resp, "status", 200)
        latency = int((_time.time() - t0) * 1000)
        data = json.loads(raw)
        if data.get("error"):
            err = data["error"]
            if isinstance(err, dict):
                err = err.get("message") or str(err)
            return {"ok": False, "error": str(err), "latency_ms": latency, "status": status}
        choices = data.get("choices") or []
        if not choices:
            return {"ok": False, "error": "接口返回空 choices", "latency_ms": latency, "raw": raw[:300]}
        content = (choices[0].get("message") or {}).get("content") or ""
        content = " ".join(str(content).strip().strip('"').strip("'").split())
        return {
            "ok": True,
            "message": "接口连通正常",
            "sample": content[:80],
            "latency_ms": latency,
            "model": model,
            "url": url,
        }
    except urllib.error.HTTPError as exp:
        latency = int((_time.time() - t0) * 1000)
        body = ""
        try:
            body = exp.read().decode("utf-8", errors="ignore")[:400]
        except Exception:
            pass
        return {
            "ok": False,
            "error": f"HTTP {exp.code}: {body or exp.reason}",
            "latency_ms": latency,
            "url": url,
        }
    except Exception as exp:
        latency = int((_time.time() - t0) * 1000)
        return {"ok": False, "error": str(exp), "latency_ms": latency, "url": url}


from .config import state_path, load_config

logger = logging.getLogger("auto-douyin-sparks-pro")

CHAT_URL = "https://www.douyin.com/chat"

RATE_LIMIT_KEYWORDS = [
    "操作频繁",
    "操作太频繁",
    "发送过于频繁",
    "发送频繁",
    "请稍后再试",
    "稍后再试",
    "安全验证",
    "滑动验证",
    "验证码",
    "验证中心",
    "人机验证",
    "网络异常",
    "请勿频繁",
    "当前操作频繁",
    "操作存在风险",
    "账号异常",
]

LOGIN_TEXTS = [
    "扫码登录",
    "验证码登录",
    "登录后查看",
    "登录后即可",
    "请先登录",
    "立即登录",
    "登录后使用",
    "登录后发送",
]


def _now() -> str:
    return datetime.now().astimezone().isoformat(
        timespec="seconds"
    )


def _screenshot(page, account: str) -> None:
    try:
        path = (
            state_path(account).parent
            / "last_error.png"
        )

        page.screenshot(
            path=str(path),
            timeout=5000,
        )

        logger.info(
            "账号 %s：已保存页面截图：%s",
            account,
            path,
        )

    except Exception:
        pass


def check_login(page) -> tuple[bool, str]:
    """检查当前页面登录状态。"""

    try:
        url = page.url

        if (
            "login" in url.lower()
            or "passport" in url.lower()
        ):
            return (
                False,
                f"页面已跳转到登录页（{url}）",
            )

    except Exception:
        pass

    # --------------------------------------------------------
    # 检测二维码
    # --------------------------------------------------------

    try:

        qr = page.locator(
            "#animate_qrcode_container"
        )

        if (
            qr.count()
            and qr.first.is_visible()
        ):
            return (
                False,
                "页面出现扫码登录二维码，登录态已过期",
            )

    except Exception:
        pass

    # --------------------------------------------------------
    # 检测登录文字
    # --------------------------------------------------------

    for text in LOGIN_TEXTS:

        try:

            loc = page.get_by_text(
                text,
                exact=False,
            )

            count = min(
                loc.count(),
                5,
            )

            for i in range(count):

                try:

                    if loc.nth(i).is_visible():

                        return (
                            False,
                            f"页面出现登录提示「{text}」",
                        )

                except Exception:
                    continue

        except Exception:
            continue

    # --------------------------------------------------------
    # 检测 sessionid
    # --------------------------------------------------------

    try:

        cookies = page.context.cookies()

        if not any(
            c.get("name", "").startswith("sessionid")
            for c in cookies
        ):
            return (
                False,
                "未检测到 sessionid Cookie，登录状态可能已失效",
            )

    except Exception as e:

        return (
            False,
            f"读取 Cookie 失败：{e}",
        )

    return True, "ok"


def detect_rate_limit(page) -> str | None:
    """检测页面是否出现限流/安全验证提示。"""

    for kw in RATE_LIMIT_KEYWORDS:

        try:

            loc = page.get_by_text(
                kw,
                exact=False,
            )

            count = loc.count()

            for i in range(count):

                try:

                    if loc.nth(i).bounding_box():
                        return kw

                except Exception:
                    continue

        except Exception:
            continue

    return None


def _find_contact(page, name: str):

    try:

        exact = page.get_by_text(
            name,
            exact=True,
        )

        if exact.count():
            return exact.first

    except Exception:
        pass

    try:

        return (
            page.locator(
                ".conversationConversationItemtitle"
            )
            .filter(has_text=name)
            .first
        )

    except Exception:

        # 兜底也必须精确匹配：旧写法 page.locator("text=" + name) 是子串
        # 匹配，给「张三」发消息可能点中「张三丰」，消息会发给错误的人
        return page.get_by_text(
            name,
            exact=True,
        ).first


def verify_in_conversation(
    page,
    name: str,
) -> bool:

    for exact in (True, False):

        try:

            loc = page.get_by_text(
                name,
                exact=exact,
            )

            for i in range(loc.count()):

                try:
                    box = (
                        loc.nth(i)
                        .bounding_box()
                    )

                except Exception:
                    continue

                if not box:
                    continue

                x = box.get("x", 0)
                y = box.get("y", 0)

                if x > 300 and y < 120:
                    return True

        except Exception:
            continue

    return False


def search_and_open(
    page,
    name: str,
) -> bool:

    try:

        box = (
            page
            .get_by_placeholder(
                "搜索",
                exact=False,
            )
            .first
        )

        if box.count() == 0:
            return False

        box.click()
        box.fill(name)

        time.sleep(3)

        btn = (
            page
            .get_by_text(
                "发消息",
                exact=False,
            )
            .first
        )

        if btn.count():

            btn.click(force=True)

            time.sleep(3)

            return True

        candidate = page.get_by_text(
            name,
            exact=True,
        ).first

        if candidate.count() == 0:

            candidate = page.get_by_text(
                name,
                exact=False,
            ).first

        if candidate.count() == 0:
            return False

        candidate.click(
            force=True
        )

        time.sleep(3)

        btn = (
            page
            .get_by_text(
                "发消息",
                exact=False,
            )
            .first
        )

        if btn.count():

            btn.click(
                force=True
            )

            time.sleep(3)

        return True

    except Exception as e:

        logger.info(
            "搜索打开 %s 失败：%s",
            name,
            e,
        )

        return False


def _type_and_send(
    page,
    input_box,
    msg_text: str,
) -> bool:

    try:

        input_box.click()

        time.sleep(0.4)

        page.keyboard.press(
            "Control+A"
        )

        page.keyboard.press(
            "Delete"
        )

        time.sleep(0.3)

        page.keyboard.type(
            msg_text,
            delay=80,
        )

        time.sleep(0.8)

        cur = (
            input_box.inner_text()
            or ""
        )

        if msg_text not in cur:

            logger.warning(
                "文字未进入输入框，当前内容：%r",
                cur[:50],
            )

            return False

        page.keyboard.press(
            "Enter"
        )

        return True

    except Exception as e:

        logger.info(
            "输入/发送异常：%s",
            str(e)[:150],
        )

        return False


def _wait_input_cleared(
    input_box,
    msg_text: str,
    wait: float = 8,
) -> bool:

    deadline = (
        time.time()
        + wait
    )

    while time.time() < deadline:

        time.sleep(1)

        try:

            cur = (
                input_box.inner_text()
                or ""
            )

            if msg_text not in cur:
                return True

        except Exception:
            pass

    return False


def send_to_contact(
    page,
    name: str,
    msg_text: str,
    dry_run: bool,
) -> tuple[bool, str]:

    switched = False

    for _attempt in range(5):

        # 每次尝试前检查登录
        logged, why = check_login(page)

        if not logged:
            return False, why

        try:

            target = _find_contact(
                page,
                name,
            )

            if target.count():

                target.click(
                    force=True,
                    timeout=10000,
                )

                time.sleep(
                    random.uniform(
                        2,
                        4,
                    )
                )

                if verify_in_conversation(
                    page,
                    name,
                ):

                    switched = True
                    break

            else:

                try:

                    page.mouse.move(
                        200,
                        350,
                    )

                    page.mouse.wheel(
                        0,
                        600,
                    )

                except Exception:
                    pass

                time.sleep(1.5)

        except Exception as e:

            logger.info(
                "点击联系人 %s 异常：%s",
                name,
                str(e)[:120],
            )

        # 点击失败后再次检查页面状态
        logged, why = check_login(page)

        if not logged:
            return False, why

        limit = detect_rate_limit(page)

        if limit:
            return (
                False,
                f"检测到「{limit}」提示",
            )

        time.sleep(
            random.uniform(
                1,
                2,
            )
        )

    if not switched:

        if search_and_open(
            page,
            name,
        ):

            time.sleep(
                random.uniform(
                    1,
                    3,
                )
            )

            # 搜索后再次检查
            logged, why = check_login(page)

            if not logged:
                return False, why

            switched = verify_in_conversation(
                page,
                name,
            )

    if not switched:

        return (
            False,
            "未能切换到该好友会话",
        )

    # 会话打开以后再次检查
    logged, why = check_login(page)

    if not logged:
        return False, why

    limit = detect_rate_limit(page)

    if limit:

        return (
            False,
            f"检测到「{limit}」提示",
        )

    input_box = page.locator(
        'div[contenteditable="true"]'
    ).first

    try:

        if (
            input_box.count() == 0
            or input_box.bounding_box() is None
        ):

            return (
                False,
                "找不到聊天输入框",
            )

        input_box.wait_for(
            state="visible",
            timeout=8000,
        )

    except Exception:

        return (
            False,
            "找不到聊天输入框",
        )

    if dry_run:
        return True, "dry-run"

    try:

        # 发送前再次检查
        logged, why = check_login(page)

        if not logged:
            return False, why

        limit = detect_rate_limit(page)

        if limit:

            return (
                False,
                f"发送前检测到「{limit}」提示",
            )

        if not _type_and_send(
            page,
            input_box,
            msg_text,
        ):

            # 输入失败后立即判断登录/限流
            logged, why = check_login(page)

            if not logged:
                return False, why

            limit = detect_rate_limit(page)

            if limit:
                return (
                    False,
                    f"发送失败，检测到「{limit}」提示",
                )

            return (
                False,
                "文字未能输入到输入框",
            )

        if _wait_input_cleared(
            input_box,
            msg_text,
            wait=8,
        ):

            # 成功后也再检查一次
            logged, why = check_login(page)

            if not logged:
                return False, why

            limit = detect_rate_limit(page)

            if limit:
                return (
                    False,
                    f"发送后检测到「{limit}」提示",
                )

            return True, "ok"

        logger.warning(
            "未检测到消息发出，不自动连续重试：%s",
            name,
        )

        limit = detect_rate_limit(page)

        if limit:

            return (
                False,
                f"发送后检测到「{limit}」提示",
            )

        logged, why = check_login(page)

        if not logged:
            return False, why

        return (
            False,
            "发送后输入框未清空，消息可能未发出",
        )

    except Exception as e:

        logger.info(
            "向 %s 发送异常：%s",
            name,
            e,
        )

        logged, why = check_login(page)

        if not logged:
            return False, why

        limit = detect_rate_limit(page)

        if limit:
            return (
                False,
                f"发送异常并检测到「{limit}」提示",
            )

        return (
            False,
            f"发送异常：{e}",
        )


def _launch_page(account: str):

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

    context = browser.new_context(
        storage_state=str(
            state_path(account)
        ),
        viewport={
            "width": 1366,
            "height": 768,
        },
    )

    page = context.new_page()

    return (
        p,
        browser,
        context,
        page,
    )


def fetch_chat_contacts(
    account: str,
) -> dict:

    result = {
        "at": _now(),
        "names": [],
        "error": None,
        "user_info": {},
    }

    state = state_path(account)

    if not state.exists():

        result["error"] = (
            f"账号 {account} 尚未上传登录态 state.json"
        )

        return result

    p = None
    browser = None

    try:

        p, browser, _context, page = (
            _launch_page(account)
        )

        goto_ok = False

        for attempt in range(3):

            try:

                page.goto(
                    CHAT_URL,
                    timeout=90000,
                    wait_until="domcontentloaded",
                )

                goto_ok = True
                break

            except Exception as e:

                logger.info(
                    "账号 %s：获取联系人第 %s 次打开页面失败：%s",
                    account,
                    attempt + 1,
                    str(e)[:100],
                )

                time.sleep(5)

        if not goto_ok:

            result["error"] = (
                "无法打开抖音私信页面"
            )

            return result

        page.wait_for_timeout(
            10000
        )

        # 关键登录检查
        logged, why = check_login(page)

        if not logged:

            result["error"] = why

            _screenshot(
                page,
                account,
            )

            logger.warning(
                "账号 %s：获取联系人时登录异常：%s",
                account,
                why,
            )

            return result

        # 只取会话标题节点；清洗「昨天 10:41」等时间后缀；按昵称去重
        extract_js = r"""
        () => {
            const out = [];
            const seen = new Set();

            // 仅使用会话标题 class，避免误抓副标题/时间
            const titleNodes = document.querySelectorAll(
                '.conversationConversationItemtitle'
            );

            // 去掉末尾时间：昨天/今天/刚刚/周x/日期 + 可选时分
            function cleanName(raw) {
                let s = (raw || '').replace(/\s+/g, ' ').trim();
                if (!s) return '';
                s = s
                    .replace(/\s*(刚刚|昨天|今天|前天|星期[一二三四五六日天]|周[一二三四五六日天])\s*(\d{1,2}:\d{2})?\s*$/g, '')
                    .replace(/\s*\d{1,2}月\d{1,2}日(\s*\d{1,2}:\d{2})?\s*$/g, '')
                    .replace(/\s*\d{4}[-/]\d{1,2}[-/]\d{1,2}(\s*\d{1,2}:\d{2})?\s*$/g, '')
                    .replace(/\s*\d{1,2}:\d{2}\s*$/g, '')
                    .trim();
                return s;
            }

            titleNodes.forEach(t => {
                // 只用标题节点自身文本，不要 parent 的 innerText（会带上时间）
                let raw = '';
                try {
                    raw = (t.innerText || t.textContent || '').trim();
                } catch (e) {
                    raw = (t.textContent || '').trim();
                }
                const name = cleanName(raw);
                if (!name || name.length > 40) return;
                if (seen.has(name)) return;
                seen.add(name);

                let card = t.parentElement;
                for (let i = 0; i < 6 && card; i++) {
                    const cls = String(card.className || '');
                    if (/conversation|ConversationItem/i.test(cls)) break;
                    card = card.parentElement;
                }
                if (!card) card = t.parentElement || t;

                let streak = '';
                let streakActive = false;
                try {
                    // 优先用官方火花文字节点（保留中一般为橙色）
                    const streakEl = card.querySelector('.commonStreaknormalText');
                    if (streakEl) {
                        streak = (streakEl.textContent || '').trim();
                    }
                    // 兜底：其它带 Streak 的节点
                    if (!streak) {
                        const alt = card.querySelector('[class*="Streak"] [class*="Text"], [class*="streak"]');
                        if (alt) streak = (alt.textContent || '').trim();
                    }
                    if (streak) {
                        // 有火花天数默认视为「保留中」；仅明确已熄灭文案才标灰
                        const deadText = /已断|已熄|未点亮|已过期|已失效|已中断/;
                        streakActive = !deadText.test(streak);
                        // class 明确带 gray/expire 时也视为熄灭
                        if (streakEl) {
                            const cls = String(streakEl.className || '').toLowerCase();
                            if (/gray|grey|expire|broken|inactive|off|disabled|muted/.test(cls)) {
                                streakActive = false;
                            }
                        }
                    }
                } catch (e) {}

                // 抖音号：列表里通常没有公开号，尽量从属性取；取不到留空
                let douyinId = '';
                try {
                    const attrs = [
                        card.getAttribute('data-uid'),
                        card.getAttribute('data-userid'),
                        card.getAttribute('data-unique-id'),
                    ];
                    for (const a of attrs) {
                        if (a && /^[A-Za-z0-9_.\-]{2,40}$/.test(a)) {
                            douyinId = a;
                            break;
                        }
                    }
                } catch (e) {}

                out.push({
                    name: name,
                    douyin_name: name,
                    douyin_id: douyinId,
                    streak: streak,
                    streak_active: streakActive,
                });
            });

            return out;
        }
        """

        collected: list[dict] = []
        seen_names: set[str] = set()

        def _clean_friend_name(raw: str) -> str:
            s = " ".join(str(raw or "").split()).strip()
            if not s:
                return ""
            import re as _re
            s = _re.sub(
                r"\s*(刚刚|昨天|今天|前天|星期[一二三四五六日天]|周[一二三四五六日天])\s*(\d{1,2}:\d{2})?\s*$",
                "",
                s,
            )
            s = _re.sub(r"\s*\d{1,2}月\d{1,2}日(\s*\d{1,2}:\d{2})?\s*$", "", s)
            s = _re.sub(r"\s*\d{4}[-/]\d{1,2}[-/]\d{1,2}(\s*\d{1,2}:\d{2})?\s*$", "", s)
            s = _re.sub(r"\s*\d{1,2}:\d{2}\s*$", "", s)
            return s.strip()

        for _attempt in range(3):

            try:

                page.wait_for_selector(
                    ".conversationConversationItemtitle",
                    timeout=45000,
                )

            except Exception:
                pass

            stable = 0

            for _ in range(20):

                # 获取过程中也检查登录
                logged, why = check_login(page)

                if not logged:

                    result["error"] = why

                    _screenshot(
                        page,
                        account,
                    )

                    logger.warning(
                        "账号 %s：读取联系人过程中登录失效：%s",
                        account,
                        why,
                    )

                    return result

                data = (
                    page.evaluate(
                        extract_js
                    )
                    or []
                )

                new_count = 0
                for x in data:
                    if not isinstance(x, dict):
                        continue
                    name = _clean_friend_name(x.get("name") or x.get("douyin_name") or "")
                    if not name or name in seen_names:
                        continue
                    seen_names.add(name)
                    _streak = (x.get("streak") or "").strip()
                    # 有火花文案默认保留中；仅明确 false 才熄灭
                    _active = x.get("streak_active")
                    if _active is None:
                        _active = bool(_streak)
                    collected.append({
                        "name": name,
                        "douyin_name": name,
                        "douyin_id": (x.get("douyin_id") or "").strip(),
                        "streak": _streak,
                        "streak_active": bool(_active) if _streak else False,
                    })
                    new_count += 1

                if new_count:

                    stable = 0

                else:

                    stable += 1

                    if stable >= 2:
                        break

                try:

                    page.mouse.move(
                        200,
                        350,
                    )

                    page.mouse.wheel(
                        0,
                        800,
                    )

                except Exception:
                    pass

                page.wait_for_timeout(
                    1200
                )

            if collected:
                break

        result["names"] = collected
        result["contacts"] = collected

        # ====================================================
        # 提取当前登录账号用户信息（昵称 + 抖音号）
        # ====================================================
        try:
            user_info = get_user_info_from_cookie(account)
            result["user_info"] = user_info
            if "error" not in user_info:
                logger.info(
                    "账号 %s：提取用户信息 - 昵称=%s, 抖音号=%s",
                    account,
                    user_info.get("nickname", ""),
                    user_info.get("unique_id", ""),
                )
            else:
                logger.warning("账号 %s：提取用户信息失败：%s", account, user_info.get("error"))
        except Exception as e:
            logger.warning("账号 %s：提取用户信息失败：%s", account, e)
            result["user_info"] = {}

        logger.info(
            "账号 %s：已读取聊天列表联系人 %s 个",
            account,
            len(collected),
        )

        return result

    except Exception as e:

        logger.error(
            "账号 %s：获取联系人异常：%s",
            account,
            e,
        )

        result["error"] = (
            f"获取联系人异常：{e}"
        )

        return result

    finally:

        if browser:

            try:
                browser.close()
            except Exception:
                pass

        if p:

            try:
                p.stop()
            except Exception:
                pass


def run_send(
    account: str,
    dry_run: bool = False,
    only_names: list[str] | None = None,
) -> dict:

    # 获取账号专用 logger
    account_logger = logging.getLogger(f"auto-douyin-sparks-pro.{account}")

    cfg = load_config(account)

    friends = cfg.get("friends") or []

    if only_names is not None:
        friends = [f for f in friends if f in only_names]

    messages = cfg.get("messages") or ["🔥"]

    # ====================================================
    # 修复 max_friends_per_run 读取逻辑
    # ====================================================
    max_n_raw = cfg.get("max_friends_per_run", 20)

    if max_n_raw is None or max_n_raw == "":
        max_n = 20
    else:
        try:
            max_n = int(max_n_raw)
        except (TypeError, ValueError):
            max_n = 20

    if max_n <= 0:
        targets = friends
        max_n_display = "不限制 (全部发送)"
    else:
        targets = friends[:max_n]
        max_n_display = f"{max_n} 人"

    gap_min = max(1, int(cfg.get("send_gap_min", 6) or 6))
    gap_max = max(gap_min, int(cfg.get("send_gap_max", 12) or 12))

    # ====================================================
    # 检查是否有上次未完成的任务（断点续发）
    # ====================================================
    from core.runtime import load_runtime, update_runtime

    rt = load_runtime(account)
    stop_progress = rt.get("_stop_progress", {})

    if stop_progress and stop_progress.get("total", 0) > 0:
        saved_ok = stop_progress.get("ok", [])
        saved_failed = stop_progress.get("failed", [])
        saved_sent = stop_progress.get("sent", 0)

        targets = [f for f in targets if f not in saved_ok]

        account_logger.info("=" * 50)
        account_logger.info("【断点续发】检测到上次未完成的任务")
        account_logger.info("  上次已发送: %s 人", saved_sent)
        account_logger.info("  上次成功: %s 人", len(saved_ok))
        account_logger.info("  上次失败: %s 人", len(saved_failed))
        account_logger.info("  本次继续发送: %s 人", len(targets))
        account_logger.info("=" * 50)

        update_runtime(account, _stop_progress=None)

        result = {
            "at": _now(),
            "account": account,
            "dry_run": bool(dry_run),
            "ok": saved_ok.copy(),
            "failed": saved_failed.copy(),
            "logged_out": False,
            "rate_limited": False,
            "stopped": False,
            "stop_reason": None,
            "_resumed": True,
            "_resumed_count": len(saved_ok),
        }
    else:
        result = {
            "at": _now(),
            "account": account,
            "dry_run": bool(dry_run),
            "ok": [],
            "failed": [],
            "logged_out": False,
            "rate_limited": False,
            "stopped": False,
            "stop_reason": None,
        }

    state = state_path(account)

    if not state.exists():
        reason = "尚未上传登录态 state.json"
        result["failed"].append({"name": "_system", "reason": reason})
        result["logged_out"] = True
        result["stopped"] = True
        result["stop_reason"] = reason
        return result

    if not targets:
        if result.get("ok") and not result.get("failed"):
            account_logger.info("✅ 所有好友已发送完成，无需重复发送")
        else:
            account_logger.info("未配置任何好友，跳过发送")
        return result

    # ====================================================
    # 打印定时设置参数到日志
    # ====================================================
    account_logger.info("=" * 50)
    account_logger.info("【定时设置参数】")
    account_logger.info(f"  每天发送时间: {cfg.get('schedule_time', '21:00')}")
    account_logger.info(f"  时间抖动窗口: {cfg.get('jitter_minutes', 30)} 分钟")
    account_logger.info(f"  好友间隔: {gap_min} ~ {gap_max} 秒")
    account_logger.info(f"  每次最多发送人数: {max_n_display}")
    account_logger.info(f"  本次实际发送人数: {len(targets)} 人")
    account_logger.info(f"  干跑模式: {'是' if dry_run else '否'}")
    account_logger.info(f"  AI 文案: {'开启' if cfg.get('ai_enabled') else '关闭'}")
    if cfg.get("ai_enabled"):
        account_logger.info(f"  AI 模型: {cfg.get('ai_model') or 'gpt-4o-mini'}")
        base = (cfg.get("ai_api_base") or "").strip()
        if base:
            account_logger.info(f"  AI 接口: {base}")
    account_logger.info("=" * 50)

    p = None
    browser = None

    # 进度记录
    total_targets = len(targets)
    initial_ok_count = len(result["ok"])
    initial_failed_count = len(result["failed"])

    try:

        p, browser, _context, page = _launch_page(account)

        # ----------------------------------------------------
        # 打开聊天页面
        # ----------------------------------------------------

        goto_ok = False
        for attempt in range(3):
            try:
                page.goto(CHAT_URL, timeout=60000, wait_until="domcontentloaded")
                goto_ok = True
                break
            except Exception as e:
                account_logger.info("第 %s 次打开页面失败：%s", attempt + 1, str(e)[:100])
                time.sleep(5)

        if not goto_ok:
            reason = "无法打开抖音私信页面"
            result["failed"].append({"name": "_system", "reason": reason})
            result["stopped"] = True
            result["stop_reason"] = reason
            return result

        time.sleep(8)

        # ----------------------------------------------------
        # 首次登录检查
        # ----------------------------------------------------

        logged, why = check_login(page)
        if not logged:
            result["logged_out"] = True
            result["stopped"] = True
            result["stop_reason"] = why
            result["failed"].append({"name": "_system", "reason": why})
            _screenshot(page, account)
            account_logger.warning("登录状态异常：%s，停止任务", why)
            return result

        account_logger.info("待发送好友 %s 人，dry_run=%s", len(targets), dry_run)

        # ----------------------------------------------------
        # 开始发送（从第一条消息开始计时）
        # ----------------------------------------------------
        send_batch_start = time.time()
        result["started_at"] = _now()

        # 保存进度到 runtime（用于强制停止时获取进度）
        from core.runtime import update_runtime

        progress = {
            "ok": result["ok"].copy(),
            "failed": result["failed"].copy(),
            "total": total_targets + initial_ok_count + initial_failed_count,
            "sent": len(result["ok"]) + len(result["failed"]),
        }
        update_runtime(account, _progress=progress)

        for index, name in enumerate(targets, start=1):

            # 强制停止检查
            try:
                rt_now = load_runtime(account)
                if rt_now.get("_force_stop"):
                    result["stopped"] = True
                    result["stop_reason"] = "用户手动强制停止"
                    account_logger.warning("检测到强制停止信号，中断发送循环")
                    break
            except Exception:
                pass

            # 每个好友发送之前再次检查登录
            logged, why = check_login(page)
            if not logged:
                result["logged_out"] = True
                result["stopped"] = True
                result["stop_reason"] = why
                result["failed"].append({"name": "_system", "reason": why})
                _screenshot(page, account)
                account_logger.warning("发送过程中登录失效：%s，立即停止", why)
                break

            # 每次发送之前检查限流
            limit = detect_rate_limit(page)
            if limit:
                result["rate_limited"] = True
                result["stopped"] = True
                result["stop_reason"] = f"检测到「{limit}」提示"
                account_logger.warning("检测到限制「%s」，立即停止", limit)
                break

            msg = None
            if cfg.get("ai_enabled"):
                account_logger.info("【AI】正在为「%s」生成续火花文案…", name)
                msg = generate_ai_message(cfg, friend_name=str(name))
                if msg:
                    account_logger.info("【AI】生成成功「%s」→ %s", name, msg)
                else:
                    account_logger.warning("【AI】生成失败「%s」，改用消息模板", name)
            if not msg:
                msg = random.choice(messages)
                if cfg.get("ai_enabled"):
                    account_logger.info("【模板】「%s」→ %s", name, msg)

            account_logger.info("【发送】给「%s」的消息：%s", name, msg)
            send_start = time.time()

            # 每位好友最多尝试 3 次；连续多名好友都失败则视为疑似风控
            ok = False
            why = ""
            max_attempts = 3
            for attempt in range(1, max_attempts + 1):
                ok, why = send_to_contact(page, name, msg, dry_run)
                if ok:
                    break

                account_logger.warning(
                    "❌ %s 第 %s/%s 次发送失败：%s",
                    name,
                    attempt,
                    max_attempts,
                    why,
                )

                # 登录失效 → 立即停止
                logged, login_reason = check_login(page)
                if not logged:
                    result["logged_out"] = True
                    result["stopped"] = True
                    result["stop_reason"] = login_reason
                    result["failed"].append({"name": name, "reason": why})
                    result["failed"].append({"name": "_system", "reason": login_reason})
                    _screenshot(page, account)
                    account_logger.warning("发送失败后发现登录异常：%s", login_reason)
                    break

                # 验证码 / 操作频繁等 → 立即停止
                limit = detect_rate_limit(page)
                if limit or any(
                    k in (why or "")
                    for k in (
                        "操作频繁",
                        "安全验证",
                        "验证码",
                        "滑动验证",
                        "人机验证",
                        "验证中心",
                        "账号异常",
                    )
                ):
                    result["rate_limited"] = True
                    result["stopped"] = True
                    result["stop_reason"] = (
                        f"检测到「{limit or why}」提示，疑似被风控或遇到验证码"
                    )
                    result["failed"].append({"name": name, "reason": why})
                    account_logger.warning(
                        "疑似触发限制「%s」，停止本轮",
                        limit or why,
                    )
                    break

                if attempt < max_attempts:
                    wait_retry = random.uniform(2.0, 4.0)
                    account_logger.info(
                        "⏳ %.1f 秒后重试「%s」…",
                        wait_retry,
                        name,
                    )
                    time.sleep(wait_retry)
                    # 重试时可换一条文案
                    msg = None
                    if cfg.get("ai_enabled"):
                        account_logger.info("【AI】重试生成「%s」文案…", name)
                        msg = generate_ai_message(cfg, friend_name=str(name))
                        if msg:
                            account_logger.info("【AI】重试成功「%s」→ %s", name, msg)
                        else:
                            account_logger.warning("【AI】重试仍失败「%s」，改用模板", name)
                    if not msg:
                        msg = random.choice(messages)

            if result.get("stopped"):
                break

            elapsed = time.time() - send_start

            if ok:
                result["ok"].append(name)
                result["_consecutive_fail"] = 0
                if dry_run:
                    account_logger.info("✅ %s（干跑）", name)
                else:
                    account_logger.info(
                        "✅ %s（发送成功，耗时 %.1f 秒）",
                        name,
                        elapsed,
                    )
            else:
                result["failed"].append({"name": name, "reason": why})
                consecutive = int(result.get("_consecutive_fail") or 0) + 1
                result["_consecutive_fail"] = consecutive
                account_logger.warning(
                    "❌ %s 连续 %s 次均失败，跳过该好友（连续失败好友数：%s）",
                    name,
                    max_attempts,
                    consecutive,
                )

                # 连续 2 个好友都发失败 → 疑似风控，停止后续
                if consecutive >= 2:
                    result["rate_limited"] = True
                    result["stopped"] = True
                    result["stop_reason"] = (
                        f"连续多名好友发送失败（最近失败：「{name}」：{why}），"
                        f"疑似被风控或遇到验证码，任务已停止"
                    )
                    account_logger.warning(
                        "连续 %s 个好友发送失败，疑似触发抖音限制，停止后续发送",
                        consecutive,
                    )
                    break

            # 更新进度
            progress = {
                "ok": result["ok"].copy(),
                "failed": result["failed"].copy(),
                "total": total_targets + initial_ok_count + initial_failed_count,
                "sent": len(result["ok"]) + len(result["failed"]),
            }
            update_runtime(account, _progress=progress)

            # 发送成功以后再次检查
            if not dry_run:
                logged, why = check_login(page)
                if not logged:
                    result["logged_out"] = True
                    result["stopped"] = True
                    result["stop_reason"] = why
                    result["failed"].append({"name": "_system", "reason": why})
                    _screenshot(page, account)
                    account_logger.warning("发送后发现登录异常：%s", why)
                    break

                limit = detect_rate_limit(page)
                if limit:
                    result["rate_limited"] = True
                    result["stopped"] = True
                    result["stop_reason"] = f"检测到「{limit}」提示"
                    account_logger.warning("发送后检测到限制「%s」，停止本轮", limit)
                    break

            if index < len(targets):
                wait_time = random.uniform(gap_min, gap_max)
                account_logger.info("⏳ 等待 %.1f 秒后发送下一个...", wait_time)
                # 可中断等待：每 0.5 秒检查强制停止
                waited = 0.0
                while waited < wait_time:
                    try:
                        if load_runtime(account).get("_force_stop"):
                            result["stopped"] = True
                            result["stop_reason"] = "用户手动强制停止"
                            account_logger.warning("等待期间收到强制停止信号")
                            break
                    except Exception:
                        pass
                    step = min(0.5, wait_time - waited)
                    time.sleep(step)
                    waited += step
                if result.get("stopped") and result.get("stop_reason") == "用户手动强制停止":
                    break

        # 若中途停止：保存断点，供下次手动/自动续发
        if result.get("stopped"):
            ok_names = [n for n in result["ok"] if isinstance(n, str)]
            failed_items = [
                f for f in result["failed"]
                if isinstance(f, dict) and f.get("name") not in ("_system", None)
            ]
            sent_count = len(ok_names) + len(failed_items)
            total_count = total_targets + initial_ok_count + initial_failed_count
            update_runtime(
                account,
                _progress=None,
                _force_stop=False,
                _stop_progress={
                    "ok": ok_names,
                    "failed": failed_items,
                    "total": total_count,
                    "sent": sent_count,
                },
            )
            account_logger.info(
                "已保存断点进度：成功 %s / 失败 %s，下次手动开始将跳过已成功好友",
                len(ok_names),
                len(failed_items),
            )
        else:
            # 全部完成：清除断点
            update_runtime(account, _progress=None, _stop_progress=None, _force_stop=False)

        # 发送阶段用时（从开始发第一条到结束）
        try:
            duration_sec = max(0.0, time.time() - send_batch_start)
        except Exception:
            duration_sec = 0.0
        result["duration_seconds"] = round(duration_sec, 1)
        result["finished_at"] = _now()
        # 可读时长
        mins, secs = divmod(int(duration_sec), 60)
        hours, mins = divmod(mins, 60)
        if hours:
            result["duration_text"] = f"{hours}小时{mins}分{secs}秒"
        elif mins:
            result["duration_text"] = f"{mins}分{secs}秒"
        else:
            result["duration_text"] = f"{secs}秒"

        # 打印最终结果
        account_logger.info("=" * 50)
        account_logger.info("【任务完成】" if not result.get("stopped") else "【任务已停止】")
        account_logger.info("  总发送: %s 人", len(result["ok"]) + len(result["failed"]))
        account_logger.info("  成功: %s 人", len(result["ok"]))
        account_logger.info("  失败: %s 人", len(result["failed"]))
        account_logger.info("  用时: %s（从发消息开始到结束）", result.get("duration_text", "-"))
        if result.get("stopped"):
            account_logger.info("  停止原因: %s", result.get("stop_reason") or "")
        if result.get("_resumed"):
            account_logger.info("  （已恢复上次中断的任务，续发 %s 人）", len(targets))
        account_logger.info("=" * 50)

        return result

    except Exception as e:
        account_logger.error("运行异常：%s", e)
        result["failed"].append({"name": "_system", "reason": f"运行异常：{e}"})
        result["stopped"] = True
        result["stop_reason"] = str(e)
        try:
            if "send_batch_start" in locals():
                duration_sec = max(0.0, time.time() - send_batch_start)
                result["duration_seconds"] = round(duration_sec, 1)
                mins, secs = divmod(int(duration_sec), 60)
                hours, mins = divmod(mins, 60)
                if hours:
                    result["duration_text"] = f"{hours}小时{mins}分{secs}秒"
                elif mins:
                    result["duration_text"] = f"{mins}分{secs}秒"
                else:
                    result["duration_text"] = f"{secs}秒"
                account_logger.info("  用时: %s", result["duration_text"])
        except Exception:
            pass
        try:
            ok_names = [n for n in result.get("ok", []) if isinstance(n, str)]
            failed_items = [
                f for f in result.get("failed", [])
                if isinstance(f, dict) and f.get("name") not in ("_system", None)
            ]
            update_runtime(
                account,
                _force_stop=False,
                _stop_progress={
                    "ok": ok_names,
                    "failed": failed_items,
                    "total": len(ok_names) + len(failed_items) + len(targets),
                    "sent": len(ok_names) + len(failed_items),
                },
            )
        except Exception:
            pass
        return result

    finally:
        if browser:
            try:
                browser.close()
            except Exception:
                pass
        if p:
            try:
                p.stop()
            except Exception:
                pass


# ============================================================
# 用户信息提取（登录后自动获取昵称和抖音号）
# ============================================================

def extract_user_info_from_page(page) -> dict:
    """
    从当前抖音页面直接提取用户信息（昵称和抖音号）
    不发起额外网络请求，纯页面提取
    """
    result = {
        "nickname": "",
        "unique_id": "",
        "sec_uid": "",
        "avatar": "",
    }
    
    try:
        # 方法1：从页面 JavaScript 数据提取（最准确）
        data = page.evaluate("""
            () => {
                try {
                    // 从 window.__INITIAL_STATE__ 提取
                    if (window.__INITIAL_STATE__ && window.__INITIAL_STATE__.user) {
                        return {
                            nickname: window.__INITIAL_STATE__.user.nickname || '',
                            unique_id: window.__INITIAL_STATE__.user.unique_id || '',
                            sec_uid: window.__INITIAL_STATE__.user.sec_uid || '',
                        };
                    }
                    // 从 window.__INITIAL_PROPS__ 提取
                    if (window.__INITIAL_PROPS__ && window.__INITIAL_PROPS__.user) {
                        return {
                            nickname: window.__INITIAL_PROPS__.user.nickname || '',
                            unique_id: window.__INITIAL_PROPS__.user.unique_id || '',
                            sec_uid: window.__INITIAL_PROPS__.user.sec_uid || '',
                        };
                    }
                    // 尝试从 DOM 提取
                    const nicknameEl = document.querySelector('.user-info .nickname, .user-info .name, [data-e2e="user-info"] .name, .douyin-user-info .name');
                    if (nicknameEl) {
                        return { nickname: nicknameEl.innerText.trim() };
                    }
                    // 尝试提取抖音号
                    const uidEl = document.querySelector('[data-e2e="user-id"], .user-id, .short-id');
                    if (uidEl) {
                        return { unique_id: uidEl.innerText.trim() };
                    }
                    return null;
                } catch(e) {
                    return null;
                }
            }
        """)
        if data:
            result["nickname"] = data.get("nickname") or result["nickname"]
            result["unique_id"] = data.get("unique_id") or result["unique_id"]
            result["sec_uid"] = data.get("sec_uid") or result["sec_uid"]
    except Exception:
        pass
    
    # 方法2：从页面元素提取昵称（备选）
    if not result["nickname"]:
        try:
            nickname_selectors = [
                '.user-info .nickname',
                '.user-info .name',
                '[data-e2e="user-info"] .name',
                '.douyin-user-info .name',
                '.userInfo .nickname',
                'a[href*="/user/"]',
            ]
            for selector in nickname_selectors:
                try:
                    el = page.locator(selector).first
                    if el.count() and el.is_visible():
                        text = el.inner_text().strip()
                        if text and len(text) < 50 and text not in ["抖音", "登录", "注册"]:
                            result["nickname"] = text
                            break
                except Exception:
                    pass
        except Exception:
            pass
    
    # 方法3：从页面 URL 提取 sec_uid
    if not result["sec_uid"]:
        try:
            url = page.url
            match = re.search(r'/user/([a-zA-Z0-9_-]+)', url)
            if match:
                result["sec_uid"] = match.group(1)
        except Exception:
            pass
    
    # 方法4：从页面文本解析「抖音号：xxx」
    if not result["unique_id"]:
        try:
            text = page.evaluate(
                r"""() => {
                    const body = document.body ? document.body.innerText : '';
                    const m = body.match(/抖音号[：:\s]*([A-Za-z0-9_.\-]{2,40})/);
                    return m ? m[1] : '';
                }"""
            )
            if text:
                result["unique_id"] = str(text).strip()
        except Exception:
            pass

    # 真正的公开抖音号优先由 Cookie API（unique_id）补全，不在此用 uid_tt 冒充

    # 如果昵称还是空的，尝试用 _extract_douyin_name
    if not result["nickname"]:
        try:
            result["nickname"] = _extract_douyin_name(page) or ""
        except Exception:
            pass

    return result


def _extract_douyin_name(page) -> str | None:
    """从页面提取抖音昵称"""
    candidates: list[str] = []

    selectors = [
        'a[href*="/user/"]',
        '[data-e2e="user-info"]',
        '[data-e2e*="user-info"]',
        '[class*="user-info"]',
    ]

    for selector in selectors:
        try:
            loc = page.locator(selector)
            count = min(loc.count(), 20)
            for index in range(count):
                try:
                    item = loc.nth(index)
                    if not item.is_visible():
                        continue
                    text = item.inner_text().strip()
                    if text:
                        candidates.append(text)
                except Exception:
                    continue
        except Exception:
            continue

    ignore = {"抖音", "登录", "注册", "首页", "推荐", "关注", "朋友", "消息", "我"}

    for value in candidates:
        value = " ".join(value.split()).strip()
        if not value:
            continue
        if value in ignore:
            continue
        if len(value) > 50:
            continue
        return value

    return None


# ============================================================
# 从 Cookie 提取用户信息（不依赖第三方库）
# ============================================================

# 刷新 Cookie / 校验登录态时常用的 Cookie 字段
_COOKIE_KEYS = [
    "sessionid",
    "sessionid_ss",
    "sid_tt",
    "sid_guard",
    "uid_tt",
    "uid_tt_ss",
    "ttwid",
    "passport_csrf_token",
    "passport_csrf_token_default",
    "odin_tt",
    "msToken",
    "s_v_web_id",
]


def _load_state_json(state_file: Path) -> dict:
    """读取 state.json，兼容 UTF-8 BOM。"""
    # utf-8-sig 可同时处理有无 BOM 的文件
    with open(state_file, "r", encoding="utf-8-sig") as f:
        return json.load(f)


def _extract_cookie_dict(state_data: dict) -> dict:
    cookie_dict = {}
    for cookie in state_data.get("cookies", []):
        name = cookie.get("name")
        if name and name in _COOKIE_KEYS:
            value = cookie.get("value")
            if value:
                cookie_dict[name] = value
    return cookie_dict


def _get_user_info_via_api(cookie_dict: dict) -> dict:
    """
    通过抖音 web API 获取当前登录用户信息。
    正确接口为 /aweme/v1/web/user/profile/self/（不需要 sec_user_id）。
    """
    if "sessionid" not in cookie_dict:
        return {"error": "Cookie 中缺少 sessionid，登录态已失效"}

    cookie_str = "; ".join(f"{k}={v}" for k, v in cookie_dict.items())
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/122.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Cookie": cookie_str,
        "Referer": "https://www.douyin.com/",
        "Origin": "https://www.douyin.com",
    }

    # 正确端点：self，而不是旧的 /user/profile/（会返回 UserId不合法）
    url = "https://www.douyin.com/aweme/v1/web/user/profile/self/"
    params = {
        "device_platform": "webapp",
        "aid": "6383",
        "channel": "channel_pc_web",
        "publish_video_strategy_type": "2",
        "source": "channel_pc_web",
        "pc_client_type": "1",
        "version_code": "170400",
        "version_name": "17.4.0",
        "cookie_enabled": "true",
        "platform": "PC",
        "downlink": "10",
    }

    try:
        resp = requests.get(url, headers=headers, params=params, timeout=15)
        content_type = (resp.headers.get("Content-Type") or "").lower()
        text = resp.text or ""
        if "application/json" not in content_type and not text.lstrip().startswith("{"):
            if any(k in text for k in ("登录", "login", "扫码", "验证")):
                return {"error": "Cookie 已失效，请重新登录"}
            return {"error": f"API 返回非 JSON（HTTP {resp.status_code}），可能被风控"}

        data = resp.json()
        status = data.get("status_code")
        if status not in (0, "0", None):
            msg = data.get("status_msg") or data.get("message") or "未知错误"
            if any(
                k in str(msg)
                for k in ("登录", "未登录", "过期", "失效", "invalid", "expired")
            ):
                return {"error": f"登录已过期：{msg}"}
            return {"error": f"API 返回错误：{msg}"}

        user_info = data.get("user") or data.get("user_info") or {}
        if not user_info:
            return {"error": "未获取到用户信息"}

        avatar = ""
        for key in ("avatar_thumb", "avatar_medium", "avatar_larger", "avatar_url"):
            av = user_info.get(key)
            if isinstance(av, dict):
                urls = av.get("url_list") or []
                if urls:
                    avatar = urls[0]
                    break
            elif isinstance(av, str) and av.startswith("http"):
                avatar = av
                break

        return {
            "nickname": user_info.get("nickname") or "",
            "unique_id": user_info.get("unique_id")
            or user_info.get("short_id")
            or "",
            "sec_uid": user_info.get("sec_uid") or "",
            "avatar": avatar,
        }
    except requests.exceptions.Timeout:
        return {"error": "请求超时"}
    except requests.exceptions.ConnectionError:
        return {"error": "网络连接失败"}
    except json.JSONDecodeError:
        return {"error": "API 返回非 JSON，可能 Cookie 已失效"}
    except Exception as e:
        return {"error": f"请求异常：{e}"}


def _get_user_info_via_browser(account: str) -> dict:
    """
    使用 Playwright + 已有 storage_state 打开抖音页面，校验登录并提取用户信息。
    比纯 HTTP API 更稳定（不依赖 a_bogus 等签名参数）。
    """
    state = state_path(account)
    if not state.exists():
        return {"error": "state.json 不存在，请先登录"}

    p = browser = context = page = None
    try:
        p, browser, context, page = _launch_page(account)
        page.goto(CHAT_URL, wait_until="domcontentloaded", timeout=45000)
        page.wait_for_timeout(2500)

        ok, reason = check_login(page)
        if not ok:
            return {"error": reason or "登录态已失效"}

        info = extract_user_info_from_page(page)
        if not info.get("nickname") and not info.get("sec_uid"):
            try:
                page.goto(
                    "https://www.douyin.com/",
                    wait_until="domcontentloaded",
                    timeout=30000,
                )
                page.wait_for_timeout(2000)
                info = extract_user_info_from_page(page)
            except Exception:
                pass

        if (
            not info.get("nickname")
            and not info.get("sec_uid")
            and not info.get("unique_id")
        ):
            return {"error": "登录态有效，但未能解析到用户信息"}

        return {
            "nickname": info.get("nickname") or "",
            "unique_id": info.get("unique_id") or "",
            "sec_uid": info.get("sec_uid") or "",
            "avatar": info.get("avatar") or "",
        }
    except Exception as e:
        return {"error": f"浏览器校验失败：{e}"}
    finally:
        if browser:
            try:
                browser.close()
            except Exception:
                pass
        if p:
            try:
                p.stop()
            except Exception:
                pass


def get_user_info_from_cookie(account: str) -> dict:
    """
    从 state.json 校验登录态并提取用户信息。

    策略：
    1. 先用 Cookie 请求 /aweme/v1/web/user/profile/self/（快、无头）
    2. API 失败时回退到 Playwright 打开聊天页校验（与「获取联系人」同一套逻辑，更稳）

    返回: {"nickname": "...", "unique_id": "...", "sec_uid": "..."}
          或 {"error": "..."}
    """
    state_file = state_path(account)
    if not state_file.exists():
        return {"error": "state.json 不存在，请先登录"}

    try:
        state_data = _load_state_json(state_file)
    except Exception as e:
        return {"error": f"读取 state.json 失败：{e}"}

    cookie_dict = _extract_cookie_dict(state_data)
    if "sessionid" not in cookie_dict:
        return {"error": "Cookie 中缺少 sessionid，登录态已失效"}

    # 1) HTTP API
    api_result = _get_user_info_via_api(cookie_dict)
    if "error" not in api_result:
        return api_result

    api_error = api_result.get("error", "")
    logger.info(
        "账号 %s：profile/self API 失败（%s），回退到浏览器校验",
        account,
        api_error,
    )

    # 明确缺少 sessionid 时不必再开浏览器
    if "缺少 sessionid" in api_error:
        return api_result

    # 2) Playwright 回退
    browser_result = _get_user_info_via_browser(account)
    if "error" not in browser_result:
        return browser_result

    # 两边都失败：优先返回更贴近登录态的错误
    return {
        "error": browser_result.get("error") or api_error or "刷新 Cookie 失败",
    }
