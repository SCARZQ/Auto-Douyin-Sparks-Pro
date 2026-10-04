"""全局统计：访问量与平台累计数据"""
from __future__ import annotations

import os
import json
import threading
from datetime import datetime, date
from pathlib import Path
import logging

from .config import list_accounts, load_config, state_path
from .runtime import load_runtime
from . import auth as auth_mod

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
STATS_PATH = DATA_DIR / "site_stats.json"

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


def _load_visits() -> dict:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if not STATS_PATH.exists():
        return {"visits": 0, "updated_at": None}
    try:
        data = json.loads(STATS_PATH.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    return {"visits": 0, "updated_at": None}


def _save_visits(data: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    _atomic_write_text(
        STATS_PATH,
        json.dumps(data, ensure_ascii=False, indent=2),
    )


def bump_visit() -> int:
    with _lock:
        data = _load_visits()
        data["visits"] = int(data.get("visits") or 0) + 1
        data["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        try:
            _save_visits(data)
        except Exception:
            logging.exception("保存 site_stats.json 失败 path=%s", STATS_PATH)
            raise
        return int(data["visits"])


def get_visits() -> int:
    with _lock:
        return int(_load_visits().get("visits") or 0)


def _is_today(iso: str | None) -> bool:
    if not iso:
        return False
    try:
        s = str(iso).replace("Z", "+00:00")
        # date part first 10 chars
        d = s[:10]
        return d == date.today().isoformat()
    except Exception:
        return False


def collect_global_stats() -> dict:
    """汇总平台级数据（管理员可见）"""
    accounts = list_accounts()
    users = auth_mod.list_users_for_admin()

    douyin_login_ok = 0
    total_friends_contacts = 0
    selected_friends = 0
    total_runs = 0
    success_total = 0
    success_today = 0
    running_now = 0
    accounts_with_state = 0

    for name in accounts:
        rt = load_runtime(name)
        cfg = load_config(name)

        if state_path(name).exists():
            accounts_with_state += 1
        if rt.get("session_status") == "ok":
            douyin_login_ok += 1
        if rt.get("running"):
            running_now += 1

        contacts = rt.get("contacts") or []
        if isinstance(contacts, list):
            total_friends_contacts += len(contacts)

        friends = cfg.get("friends") or []
        if isinstance(friends, list):
            selected_friends += len([f for f in friends if str(f).strip()])

        history = rt.get("history") or []
        if not isinstance(history, list):
            history = []
        # also count last_run if not in history
        last = rt.get("last_run")
        runs = list(history)
        if isinstance(last, dict) and last:
            # avoid double count if already first history item
            if not runs or runs[0] is not last:
                # compare by 'at' field
                last_at = last.get("at")
                if not runs or (isinstance(runs[0], dict) and runs[0].get("at") != last_at):
                    runs = [last] + runs

        total_runs += len(runs)

        for run in runs:
            if not isinstance(run, dict):
                continue
            ok_list = run.get("ok") or []
            if not isinstance(ok_list, list):
                ok_list = []
            n_ok = len(ok_list)
            success_total += n_ok
            if _is_today(run.get("at")):
                success_today += n_ok

    return {
        "visits": get_visits(),
        "registered_users": len(users),
        "accounts_total": len(accounts),
        "douyin_login_ok": douyin_login_ok,
        "accounts_with_state": accounts_with_state,
        "selected_friends": selected_friends,
        "total_contacts": total_friends_contacts,
        "total_runs": total_runs,
        "success_total": success_total,
        "success_today": success_today,
        "running_now": running_now,
        "spark_accounts": sum(
            1 for name in accounts
            if (load_config(name).get("friends") or [])
        ),
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


# ---------- 访问 IP 记录 ----------
VISITS_IP_PATH = DATA_DIR / "visit_ips.json"
_MAX_IP_RECORDS = 500


def _load_ip_records() -> list:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if not VISITS_IP_PATH.exists():
        return []
    try:
        data = json.loads(VISITS_IP_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _save_ip_records(items: list) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    _atomic_write_text(
        VISITS_IP_PATH,
        json.dumps(items[:_MAX_IP_RECORDS], ensure_ascii=False, indent=2),
    )


def _is_private_ip(ip: str) -> bool:
    """判断是否内网 / 保留地址。"""
    ip = (ip or "").strip()
    if not ip or ip in ("127.0.0.1", "::1", "localhost", "unknown"):
        return True
    if ip.startswith(("10.", "192.168.", "169.254.", "127.")):
        return True
    if ip.startswith("172."):
        try:
            second = int(ip.split(".")[1])
            if 16 <= second <= 31:
                return True
        except Exception:
            pass
    if ip.startswith("::ffff:"):
        return _is_private_ip(ip[7:])
    return False


# 简单的内存缓存，避免同一个 IP 反复请求第三方接口
_REGION_CACHE: dict[str, str] = {}
_REGION_CACHE_MAX = 800


def _fetch_json(url: str, timeout: float = 4.0, encoding: str = "utf-8"):
    """取 JSON。

    编码处理很关键：国内接口（如 pconline 的 ipJson.jsp）返回的是 GBK，
    按 UTF-8 解会把中文变成乱码（"妖?" "幻?" 这种）。
    这里按「响应头 charset -> 调用方指定 -> utf-8 -> gbk -> utf-8 容错」依次尝试。
    """
    import urllib.request
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (compatible; auto-douyin-sparks-pro/1.0)",
        "Accept": "application/json, text/javascript, */*; q=0.01",
    })
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
        header_charset = None
        try:
            header_charset = resp.headers.get_content_charset()
        except Exception:
            header_charset = None

    last_err = None
    for enc in (header_charset, encoding, "utf-8", "gbk", "gb18030"):
        if not enc:
            continue
        try:
            return json.loads(raw.decode(enc))
        except Exception as e:      # 解码失败 或 JSON 解析失败
            last_err = e
            continue
    # 最后兜底：忽略非法字节
    try:
        return json.loads(raw.decode("utf-8", errors="ignore"))
    except Exception:
        raise last_err or ValueError("接口返回不是合法 JSON")


def _join_region(*parts) -> str:
    """把国家/省/市拼成「中国 广东 深圳」，去掉重复项。"""
    out = []
    for p in parts:
        p = str(p or "").strip()
        if p and p not in out:
            out.append(p)
    return " ".join(out)


def _lookup_ip_region(ip: str, force: bool = False) -> str:
    """IP 归属地查询（多源 + 交叉校验）。

    数据源按准确性排序：
      1) pconline  —— 国内库，中国 IP 最准（GBK 编码）
      2) ip-api    —— 中文返回，国际覆盖好
      3) ipapi.co  —— 备用
      4) ipwho.is  —— 备用
    前两个源结果一致就直接采用；不一致时优先国内源。
    返回「中国 广东 深圳」这种格式，失败返回「未知」。
    """
    ip = (ip or "").strip()
    if ip.startswith("::ffff:"):
        ip = ip[7:]

    if _is_private_ip(ip):
        return "内网/本地"

    if not force:
        cached = _REGION_CACHE.get(ip)
        if cached is not None:
            return cached

    results = []

    # ---- 1) pconline（国内库，最准；GBK 编码）----
    try:
        data = _fetch_json(
            f"https://whois.pconline.com.cn/ipJson.jsp?ip={ip}&json=true",
            encoding="gbk",
        )
        pro = (data.get("pro") or "").strip()
        city = (data.get("city") or "").strip()
        if pro:
            results.append(("pconline", _join_region(pro, city)))
    except Exception:
        pass

    # ---- 2) ip-api（中文）----
    try:
        data = _fetch_json(
            f"http://ip-api.com/json/{ip}"
            "?lang=zh-CN&fields=status,country,regionName,city,isp"
        )
        if data.get("status") == "success":
            results.append(("ip-api", _join_region(
                data.get("country"), data.get("regionName"), data.get("city"))))
    except Exception:
        pass

    # 两个源都有结果且一致 -> 直接采用，不再请求
    if len(results) >= 2 and results[0][1] == results[1][1] and results[0][1]:
        region = results[0][1]
    else:
        # ---- 3) ipapi.co ----
        if not results:
            try:
                data = _fetch_json(f"https://ipapi.co/{ip}/json/")
                if not data.get("error"):
                    results.append(("ipapi.co", _join_region(
                        data.get("country_name") or data.get("country"),
                        data.get("region"), data.get("city"))))
            except Exception:
                pass
        # ---- 4) ipwho.is ----
        if not results:
            try:
                data = _fetch_json(f"https://ipwho.is/{ip}")
                if data.get("success"):
                    results.append(("ipwho.is", _join_region(
                        data.get("country"), data.get("region"), data.get("city"))))
            except Exception:
                pass

        # 取第一个非空结果（国内源优先）
        region = ""
        for _src, val in results:
            if val and val != "未知":
                region = val
                break

    # 保留原始英文行便于排查
    region = (region or "").strip()

    # 兜底：乱码不要展示
    try:
        if region and ("\ufffd" in region or region.count("?") >= 2):
            region = ""
    except Exception:
        pass

    if not region:
        region = "未知"

    # 写缓存（未知不缓存，避免永远查不出来）
    try:
        if region and region != "未知":
            if len(_REGION_CACHE) >= _REGION_CACHE_MAX:
                _REGION_CACHE.clear()
            _REGION_CACHE[ip] = region
        else:
            _REGION_CACHE.pop(ip, None)
    except Exception:
        pass

    return region



def resolve_ip_region(ip: str, force: bool = True) -> str:
    """对外暴露的归属地查询。

    force 默认为 True：后台手动点「校正」时本来就该重新查，
    命中缓存会导致看起来「点了没反应」。
    """
    return _lookup_ip_region(ip, force=force)


def update_ip_region(ip: str, region: str) -> int:
    """把某个 IP 的归属地写回所有历史记录，返回更新的条数。"""
    ip = (ip or "").strip()
    if not ip or not region:
        return 0
    n = 0
    with _lock:
        items = _load_ip_records()
        for it in items:
            if isinstance(it, dict) and it.get("ip") == ip:
                if it.get("region") != region:
                    it["region"] = region
                    it["address"] = region
                    n += 1
        if n:
            _save_ip_records(items)
    return n


# 常见反代头，顺序即优先级
PROXY_IP_HEADERS = ("CF-Connecting-IP", "True-Client-IP", "X-Real-IP",
                    "X-Client-IP", "X-Forwarded-For")


def _is_proxy_header(headers) -> bool:
    """请求是否带了反代头（带了说明前面有 Nginx/Cloudflare）。"""
    try:
        for k in PROXY_IP_HEADERS:
            if headers.get(k):
                return True
    except Exception:
        pass
    return False


def client_ip_from_headers(headers, fallback: str = "") -> str:
    """从请求头里尽力还原真实客户端 IP（兼容 Cloudflare / Nginx 反代）。

    优先级：CF-Connecting-IP > True-Client-IP > X-Real-IP > X-Client-IP
            > X-Forwarded-For 第一段 > 直连地址
    """
    try:
        for key in ("CF-Connecting-IP", "True-Client-IP", "X-Real-IP", "X-Client-IP"):
            v = headers.get(key)
            if v:
                got = str(v).strip()
                if got:
                    return got
        xff = headers.get("X-Forwarded-For")
        if xff:
            first = str(xff).split(",")[0].strip()
            if first:
                return first
    except Exception:
        pass
    return (fallback or "").strip()


def list_ip_records() -> list:
    """对外暴露：读取全部访问 IP 记录。"""
    try:
        return _load_ip_records()
    except Exception:
        return []


def save_ip_records(items: list) -> None:
    """对外暴露：整体写回访问 IP 记录。"""
    with _lock:
        _save_ip_records(items)


def record_visit_ip(ip: str, user_agent: str = "", username: str = "",
                    via_proxy: bool = False) -> dict:
    ip = (ip or "").strip()
    if not ip:
        ip = "unknown"
    # 去掉 IPv6 映射前缀
    if ip.startswith("::ffff:"):
        ip = ip[7:]
    region = _lookup_ip_region(ip)
    rec = {
        "ip": ip,
        "region": region,
        # 便于排查：这台机器看到的是直连还是反代
        "via_proxy": bool(via_proxy),
        "address": region,  # 与 region 同义，前端「地址」列
        "username": (username or "").strip() or "访客",
        "user_agent": (user_agent or "")[:180],
        "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    with _lock:
        items = _load_ip_records()
        items.insert(0, rec)
        _save_ip_records(items)
    return rec


def list_visit_ips(limit: int = 100) -> list:
    with _lock:
        items = _load_ip_records()
    limit = max(1, min(int(limit or 100), 500))
    return items[:limit]


def visit_ip_summary(limit: int = 500) -> dict:
    """访问 IP 列表（不区分用户/访客，按最后访问时间倒序）。

    实时性：每次进来都重算聚合；对仍为「未知」的公网 IP 顺手补查一次，
    这样只要有新访问，列表里的归属地就会自动更新。
    """
    items = _load_ip_records()

    agg: dict[str, dict] = {}
    for it in items:
        if not isinstance(it, dict):
            continue
        ip = (it.get("ip") or "").strip() or "unknown"
        rec = agg.get(ip)
        t = it.get("time") or ""
        if rec is None:
            _uname = (it.get("username") or "").strip()
            agg[ip] = {
                "ip": ip,
                "is_guest": (not _uname) or _uname == "访客",
                "region": it.get("region") or "",
                "username": it.get("username") or "",
                "user_agent": it.get("user_agent") or "",
                "count": 1,
                "first_time": t,
                "last_time": t,
            }
        else:
            rec["count"] += 1
            if t and (not rec["last_time"] or t > rec["last_time"]):
                rec["last_time"] = t
                # 最近一次的归属地优先
                if it.get("region"):
                    rec["region"] = it["region"]
                if it.get("username"):
                    rec["username"] = it["username"]
            if t and (not rec["first_time"] or t < rec["first_time"]):
                rec["first_time"] = t

    rows = list(agg.values())

    # 补查仍为「未知」的公网 IP（每次最多 15 个，避免拖慢接口）
    fixed = 0
    for r in rows:
        if fixed >= 15:
            break
        if r["region"] and r["region"] != "未知":
            continue
        ip = r["ip"]
        if ip in ("unknown", "") or _is_private_ip(ip):
            continue
        try:
            region = _lookup_ip_region(ip, force=False)
        except Exception:
            continue
        if region and region != "未知":
            r["region"] = region
            fixed += 1
            try:
                update_ip_region(ip, region)
            except Exception:
                pass

    rows.sort(key=lambda x: x.get("last_time") or "", reverse=True)
    rows = rows[:limit]

    total_visits = sum(r["count"] for r in rows)
    # 兼容前端字段名：不再区分「用户/访客」入口，但统计口径保留
    logged = sum(1 for r in rows if (r.get("username") or "") not in ("", "访客"))
    return {
        "items": rows,
        "total_ips": len(rows),
        "total_visits": total_visits,
        "user_ips": logged,
        "guest_ips": len(rows) - logged,
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


