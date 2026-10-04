"""多账号定时任务调度器 - 支持模式检查"""

from __future__ import annotations

import logging
import random
import threading
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger

from .config import (
    list_accounts,
    load_config,
    save_config,
    get_mode_status,
)

from .runtime import load_runtime, sync_mode_status


logger = logging.getLogger("auto-douyin-sparks-pro")

TZ = "Asia/Shanghai"
_TZINFO = ZoneInfo(TZ)

# 同一轮发送最多自动补发的次数（防止账号一直忙时无限排队）
MAX_RETRY_ATTEMPTS = 3

_scheduler: BackgroundScheduler | None = None
_run_func = None

_scheduler_lock = threading.Lock()


def _job_id(account: str) -> str:
    return f"daily_send_{account}"


def _retry_job_id(account: str) -> str:
    return f"retry_send_{account}"


def _account_blocked(account: str) -> bool:
    """判断账号是否处于禁止自动发送状态"""
    try:
        rt = load_runtime(account)
        status = str(rt.get("session_status", "unknown")).strip().lower()
        # 注意：真正的掉线状态是 "expired"（runtime 里写的就是它），
        # 只有登录线程抛异常时才会写 "failed"。两者都要拦截，
        # 否则登录已失效的账号每天仍会启动浏览器、跑失败、发紧急邮件。
        if status in ("failed", "expired"):
            logger.warning("账号 %s：当前处于异常状态（%s），跳过自动发送",
                           account, status)
            return True
        return False
    except Exception as e:
        logger.warning("账号 %s：读取运行状态失败，跳过本次自动任务：%s", account, e)
        return True


def _should_run(account: str) -> tuple[bool, str]:
    """
    检查账号是否应该执行自动发送。
    返回: (是否允许运行, 原因)
    """
    cfg = load_config(account)
    mode = cfg.get("mode", "independent")

    if mode == "independent":
        enabled = cfg.get("enabled", True)
        if not enabled:
            return False, "独立模式：已手动关闭"
        return True, "独立模式：已开启"

    elif mode == "authorized":
        authorized_until = cfg.get("authorized_until")
        if not authorized_until:
            return False, "授权模式：未设置到期时间"

        try:
            until = datetime.fromisoformat(str(authorized_until))
            # 到期时间统一折算到调度时区再比较：
            # 旧写法用 naive datetime.now() 比较，服务器不在上海时区时判断错位；
            # 若存的是带时区的 ISO 串，aware 与 naive 比较会抛 TypeError，
            # 被 except 吞掉后账号会被误判为「时间解析失败」而禁用。
            if until.tzinfo is None:
                until = until.replace(tzinfo=_TZINFO)
            else:
                until = until.astimezone(_TZINFO)
            if datetime.now(_TZINFO) > until:
                # 自动关闭
                save_config(account, {**cfg, "enabled": False})
                sync_mode_status(account)
                return False, f"授权模式：已过期 ({authorized_until})"

            # 检查是否被手动关闭
            enabled = cfg.get("enabled", True)
            if not enabled:
                return False, "授权模式：已被手动关闭"

            return True, f"授权模式：有效期至 {authorized_until}"
        except Exception as e:
            return False, f"授权模式：时间解析失败 {e}"

    return False, "未知模式"


def _daily_job(account: str) -> None:
    """某一个账号的每日任务。"""

    if not account:
        return

    # ====== 检查模式状态 ======
    should_run, reason = _should_run(account)
    if not should_run:
        logger.warning("账号 %s：跳过自动发送 - %s", account, reason)
        sync_mode_status(account)
        return

    # ====== 检查登录/限流状态 ======
    if _account_blocked(account):
        logger.warning("账号 %s：登录/限流状态异常，本次定时任务不执行", account)
        return

    try:
        cfg = load_config(account)

        jitter = max(0, int(cfg.get("jitter_minutes", 30) or 0))
        if jitter:
            delay = random.uniform(0, jitter * 60)
            logger.info("账号 %s：随机延迟 %.0f 秒后开始发送（抖动窗口 %s 分钟）", account, delay, jitter)
            time.sleep(delay)

        # 抖动后再次检查
        if _account_blocked(account):
            logger.warning("账号 %s：等待期间状态变为异常，取消本次发送", account)
            return

        if _run_func:
            _run_func(account)

    except Exception as e:
        # _run_func 同步阶段抛出的异常主要是并行额度满（409）等临时性失败，
        # 直接吞掉会导致当天漏发——安排一次补发（有次数上限，不会无限排队）
        logger.exception("账号 %s：定时任务执行异常：%s", account, e)
        try:
            schedule_retry(account, attempt=1)
        except Exception:
            logger.exception("账号 %s：定时失败后安排补发失败", account)


def configure(run_func, account: str) -> None:
    global _scheduler, _run_func
    _run_func = run_func

    with _scheduler_lock:
        if _scheduler is None:
            _scheduler = BackgroundScheduler(timezone=TZ)
            _scheduler.start()
            logger.info("多账号调度器已启动，时区=%s", TZ)

    if account:
        apply_schedule(account)


def configure_all(run_func) -> None:
    global _scheduler, _run_func
    _run_func = run_func

    with _scheduler_lock:
        if _scheduler is None:
            _scheduler = BackgroundScheduler(timezone=TZ)
            _scheduler.start()
            logger.info("多账号调度器已启动，时区=%s", TZ)

    accounts = list_accounts()
    for account in accounts:
        try:
            apply_schedule(account)
            sync_mode_status(account)
        except Exception as e:
            logger.exception("账号 %s：设置定时任务失败：%s", account, e)

    logger.info("多账号定时任务配置完成，共 %s 个账号", len(accounts))

    # 重启后检查今天有没有错过的定时发送，安排补发
    try:
        schedule_startup_catchup()
    except Exception:
        logger.exception("启动补发检查失败")


def apply_schedule(account: str | None = None) -> None:
    if _scheduler is None:
        logger.warning("调度器尚未启动，无法设置任务")
        return

    if not account:
        logger.warning("没有指定账号，无法设置任务")
        return

    account = str(account).strip()
    if not account:
        return

    cfg = load_config(account)
    schedule_time = str(cfg.get("schedule_time", "00:00")).strip()

    try:
        hh, mm = schedule_time.split(":")
        hh = int(hh)
        mm = int(mm)
        if not (0 <= hh <= 23 and 0 <= mm <= 59):
            raise ValueError
    except Exception:
        logger.warning("账号 %s：schedule_time=%r 无效，使用 00:00", account, schedule_time)
        hh = 0
        mm = 0

    job_id = _job_id(account)
    job = _scheduler.add_job(
        _daily_job,
        CronTrigger(hour=hh, minute=mm, timezone=TZ),
        args=[account],
        id=job_id,
        replace_existing=True,
        coalesce=True,
        max_instances=1,
        misfire_grace_time=3600,
    )

    # 把下次运行时间同步进 runtime，供后台「续火花管理」列表显示
    _sync_next_run(account, job)

    logger.info("账号 %s：每日任务已设置：%02d:%02d (%s)，job=%s", account, hh, mm, TZ, job_id)


def _sync_next_run(account: str, job=None) -> None:
    """把 APScheduler 的下次运行时间写入 runtime.json（失败不影响主流程）。"""
    try:
        from .runtime import update_runtime
        if job is None:
            if _scheduler is None:
                return
            job = _scheduler.get_job(_job_id(account))
        nxt = None
        if job is not None and getattr(job, "next_run_time", None):
            nxt = job.next_run_time.isoformat()
        update_runtime(account, next_run=nxt)
    except Exception:
        logger.debug("账号 %s：同步 next_run 失败", account, exc_info=True)


def sync_all_next_run() -> None:
    """全量同步一次下次运行时间（服务启动/配置变更后调用）。"""
    if _scheduler is None:
        return
    try:
        accounts = list_accounts()
    except Exception:
        return
    for acc in accounts:
        _sync_next_run(acc)


def remove_schedule(account: str) -> None:
    if _scheduler is None:
        return
    job_id = _job_id(account)
    try:
        if _scheduler.get_job(job_id):
            _scheduler.remove_job(job_id)
            logger.info("账号 %s：已删除每日任务", account)
    except Exception:
        logger.exception("账号 %s：删除每日任务失败", account)


def next_run_time(account: str | None = None) -> str | None:
    if _scheduler is None:
        return None
    if account:
        job = _scheduler.get_job(_job_id(account))
        if job and job.next_run_time:
            return job.next_run_time.isoformat()
        return None
    jobs = _scheduler.get_jobs()
    daily_jobs = [job for job in jobs if job.id.startswith("daily_send_") and job.next_run_time]
    if not daily_jobs:
        return None
    daily_jobs.sort(key=lambda job: job.next_run_time)
    return daily_jobs[0].next_run_time.isoformat()


def all_next_run_times() -> dict[str, str | None]:
    result = {}
    for account in list_accounts():
        result[account] = next_run_time(account)
    return result


def schedule_retry(
    account: str,
    delay_minutes: int | None = None,
    attempt: int = 1,
) -> bool:
    """为账号安排一次补发任务（发送失败 / 定时错过时调用）。

    使用 configure 注入的 _run_func 回调；补发真正执行前还会在
    _retry_job 里重新做模式与登录状态门控，因此提前创建是安全的。
    """
    if _scheduler is None or not account:
        return False
    if not _run_func:
        logger.warning("调度器尚未注入发送回调，无法创建补发任务")
        return False

    should_run, _ = _should_run(account)
    if not should_run:
        logger.info("账号 %s：当前模式不允许运行，不创建补发任务", account)
        return False

    if _account_blocked(account):
        logger.info("账号 %s：当前处于异常状态，不创建补发任务", account)
        return False

    if int(attempt) > MAX_RETRY_ATTEMPTS:
        logger.warning(
            "账号 %s：补发已达 %s 次上限，放弃本轮自动补发", account, MAX_RETRY_ATTEMPTS
        )
        return False

    job_id = _retry_job_id(account)
    if _scheduler.get_job(job_id):
        logger.info("账号 %s：已有补发任务，不重复安排", account)
        return False

    if delay_minutes is None:
        # 随机 20~50 分钟后补发，避免多账号在同一时刻扎堆重试
        delay_minutes = random.randint(20, 50)

    run_at = datetime.now(_TZINFO) + timedelta(minutes=max(1, int(delay_minutes)))
    _scheduler.add_job(
        _retry_job,
        DateTrigger(run_date=run_at),
        args=[account, int(attempt)],
        id=job_id,
        replace_existing=True,
        max_instances=1,
        misfire_grace_time=1800,
    )

    logger.info("账号 %s：已安排 %s 分钟后自动补发（第 %s 次）", account, delay_minutes, attempt)
    return True


def _retry_job(account: str, attempt: int = 1) -> None:
    """补发任务：执行前重新门控，避免状态变化后仍去启动浏览器。"""
    should_run, reason = _should_run(account)
    if not should_run:
        logger.info("账号 %s：补发前检查未通过 - %s", account, reason)
        return

    if _account_blocked(account):
        logger.info("账号 %s：补发前检查未通过（登录/限流状态异常），取消补发", account)
        return

    # 小幅抖动，错开同一批补发的账号
    time.sleep(random.uniform(0, 120))

    if _account_blocked(account):
        logger.info("账号 %s：补发等待期间状态变为异常，取消补发", account)
        return

    try:
        if _run_func:
            _run_func(account)
    except Exception as e:
        # 主要是全局并行额度满（409）等临时性失败：往后排一次补发
        logger.warning("账号 %s：补发执行失败：%s，尝试再次安排", account, e)
        try:
            schedule_retry(account, attempt=int(attempt) + 1)
        except Exception:
            logger.exception("账号 %s：安排下一次补发失败", account)


def schedule_startup_catchup() -> None:
    """服务（重）启动后检查：今天定时点已过、但今天还没成功发过的账号，尽快补发。

    调度任务存在内存里，进程重启会全部丢失；每日 Cron 最早也要到明天的
    触发点才会再跑。没有这段补发，服务在定时点之后宕机重启就会漏发一天。
    """
    if _scheduler is None:
        return

    now = datetime.now(_TZINFO)
    today = now.strftime("%Y-%m-%d")

    for account in list_accounts():
        try:
            # 今天的定时点还没到：交给每日任务，无需补发
            cfg = load_config(account)
            try:
                hh, mm = str(cfg.get("schedule_time", "00:00")).split(":")
                run_at = now.replace(hour=int(hh), minute=int(mm), second=0, microsecond=0)
            except Exception:
                run_at = now.replace(hour=0, minute=0, second=0, microsecond=0)
            if now < run_at:
                continue

            # 今天已经成功发送过（或最后一条记录是干跑/用户手动停止）就不再补发
            rt = load_runtime(account)
            last_at = str(rt.get("last_run_at") or "")
            if last_at[:10] == today:
                last = rt.get("last_run") if isinstance(rt.get("last_run"), dict) else {}
                if (last.get("ok") or []) or last.get("dry_run") or last.get("stopped"):
                    continue
                # 今天跑过但全部失败：也安排一次补发，把失败的好友再试一遍

            if schedule_retry(account, delay_minutes=random.randint(3, 15), attempt=1):
                logger.info(
                    "账号 %s：今天 %02d:%02d 的定时任务未成功执行（可能服务重启错过），已安排补发",
                    account, run_at.hour, run_at.minute,
                )
        except Exception:
            logger.exception("账号 %s：启动补发检查失败", account)


def cancel_retry(account: str) -> None:
    if _scheduler is None:
        return
    job_id = _retry_job_id(account)
    if _scheduler.get_job(job_id):
        _scheduler.remove_job(job_id)
        logger.info("账号 %s：已取消补发任务", account)


def list_jobs() -> list[dict]:
    if _scheduler is None:
        return []
    result = []
    for job in _scheduler.get_jobs():
        next_time = job.next_run_time.isoformat() if job.next_run_time else None
        result.append({"id": job.id, "next_run": next_time, "name": job.name})
    result.sort(key=lambda x: x["id"])
    return result


_digest_func = None


def _digest_job_id() -> str:
    return "daily_email_digest"


def _run_digest_job() -> None:
    global _digest_func
    if _digest_func:
        try:
            _digest_func()
        except Exception as e:
            logger.exception("每日邮件汇总任务异常：%s", e)


def apply_daily_digest_schedule(digest_func=None) -> None:
    """按邮件配置中的 daily_log_time 安排每日汇总（默认 12:00）。"""
    global _scheduler, _digest_func
    if digest_func is not None:
        _digest_func = digest_func
    if _scheduler is None:
        return
    try:
        from . import email_util
        cfg = email_util.load_email_config()
        t = str(cfg.get("daily_log_time") or "12:00").strip()
        try:
            hh, mm = t.split(":")
            hh_i, mm_i = int(hh), int(mm)
            if not (0 <= hh_i <= 23 and 0 <= mm_i <= 59):
                raise ValueError
        except Exception:
            hh_i, mm_i = 12, 0
        job_id = _digest_job_id()
        if not cfg.get("enabled") or not email_util.is_notify_enabled("daily_digest", cfg):
            if _scheduler.get_job(job_id):
                _scheduler.remove_job(job_id)
                logger.info("每日邮件汇总已关闭")
            return
        _scheduler.add_job(
            _run_digest_job,
            CronTrigger(hour=hh_i, minute=mm_i, timezone=TZ),
            id=job_id,
            replace_existing=True,
            max_instances=1,
            misfire_grace_time=3600,
        )
        logger.info("每日邮件汇总已安排在 %02d:%02d", hh_i, mm_i)
    except Exception as e:
        logger.exception("设置每日邮件汇总失败：%s", e)



def shutdown() -> None:
    global _scheduler, _run_func
    with _scheduler_lock:
        if _scheduler:
            try:
                _scheduler.shutdown(wait=False)
            except Exception:
                pass
        _scheduler = None
        _run_func = None
        global _digest_func
        _digest_func = None
    logger.info("多账号调度器已关闭")