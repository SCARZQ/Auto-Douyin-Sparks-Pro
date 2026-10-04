#!/usr/bin/env python3
"""同步 / 校验 6 个入口 HTML 的模板一致性。

【为什么需要这个脚本】
Dujiao 项目的前端是「多入口 + 共享 JS」结构：
  index.html / login.html / register.html / forgot.html / app.html / admin.html
这 6 个文件**装的是同一份 Vue 模板**（约 181KB），只有 <title> 和
<meta description> 不同。运行时由 JS 里的路由决定渲染哪一屏。

所以：**改模板必须同步全部 6 个文件**。
如果只改了其中一个（比如只改 admin.html），那么用户从首页登录后
用 SPA 导航到后台时，用的仍是「首次加载那个文件」的旧模板 —— 新功能不显示。

用法：
    python sync_entries.py check    # 只检查，不一致则退出码 1
    python sync_entries.py sync     # 以 admin.html 为基准同步全部
"""
import pathlib
import re
import sys

STATIC = pathlib.Path(__file__).resolve().parent.parent / "static"
ENTRIES = ["index.html", "login.html", "register.html",
           "forgot.html", "app.html", "admin.html"]
CANON = "admin.html"


def read(n):
    return (STATIC / n).read_text(encoding="utf-8")


def meta_of(t):
    mt = re.search(r"<title>(.*?)</title>", t, re.S)
    md = re.search(r'<meta name="description" content="(.*?)"', t, re.S)
    return (mt.group(1) if mt else ""), (md.group(1) if md else "")


def body(t):
    """去掉 title/meta 后的正文，用于比较模板是否一致。"""
    t = re.sub(r"<title>.*?</title>", "", t, flags=re.S)
    t = re.sub(r'<meta name="description"[^>]*>', "", t)
    return t


def check():
    texts = {n: read(n) for n in ENTRIES if (STATIC / n).exists()}
    ref = body(texts[CANON])
    bad = [n for n, t in texts.items() if body(t) != ref]
    if bad:
        print("!! 模板不一致，以下文件与 %s 不同：" % CANON)
        for n in bad:
            print("     %s" % n)
        print()
        print("   运行 `python sync_entries.py sync` 修复")
        return 1
    print("OK  6 个入口模板一致")
    return 0


def sync():
    canon = read(CANON)
    metas = {n: meta_of(read(n)) for n in ENTRIES if (STATIC / n).exists()}
    n_changed = 0
    for n, (title, desc) in metas.items():
        t = canon
        t = re.sub(r"<title>.*?</title>", "<title>%s</title>" % title,
                   t, count=1, flags=re.S)
        if desc:
            t = re.sub(r'<meta name="description" content="[^"]*" />',
                       '<meta name="description" content="%s" />' % desc,
                       t, count=1)
        if t != read(n):
            (STATIC / n).write_text(t, encoding="utf-8")
            print("  更新 %s" % n)
            n_changed += 1
    print("完成，%d 个文件有变化" % n_changed)
    return 0


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "check"
    if cmd == "sync":
        sys.exit(sync())
    sys.exit(check())
