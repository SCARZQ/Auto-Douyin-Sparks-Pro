# Auto-Douyin-Sparks-Pro

**版本：v1.6.9**

抖音「续火花」自托管自动化助手（增强版 / Pro）。

基于 **Python + FastAPI + Playwright + Vue3**，多用户、授权卡密、邮件通知、AI 文案、独立管理后台一应俱全。部署在你自己的服务器上，每日定时给指定好友发私信，维持聊天火花 🔥。

> ⚠️ **仅限本人账号、少量好友、个人自用。** 自动化发私信可能违反平台规则，存在风控/限流/封号风险，使用后果自负。请勿用于营销、批量或对外服务。

仓库：<https://github.com/SCARZQ/Auto-Douyin-Sparks-Pro>

---

## 📖 文档

| 文档 | 内容 |
|---|---|
| **[完整使用说明](docs/完整使用说明.md)** | 部署 + 使用 + 常见问题（**先看这个**） |
| [前端构建说明](docs/前端构建说明.md) | 改前端代码后如何重新构建 |
| [上传 GitHub 步骤](docs/上传GitHub步骤.md) | 含敏感文件误传的补救办法 |

**新手快速路径**：

```bash
# 服务器一键部署（Debian / Ubuntu）
# 可选镜像源：MIRROR=aliyun / tuna / official（官方），或运行时按菜单选择
sudo MIRROR=aliyun bash deploy/deploy.sh

# 跑完后打开 http://服务器IP:8000，用 admin / admin 登录
# ⚠️ 登录后第一件事：改密码
```

也可用本仓库根目录的服务管理脚本：

```bash
bash manage.sh          # 交互菜单
bash manage.sh start    # 后台启动
bash manage.sh stop     # 停止
bash manage.sh restart  # 重启
bash manage.sh status   # 状态
bash manage.sh logs     # 跟踪日志
```

部署脚本会自动：切换 apt/pip/Playwright 镜像源（可选）、安装 Chromium 及其**系统依赖**、安装 **Xvfb 虚拟显示**（无图形界面服务器上网页登录可用）、配置 swap 与时区。

**无图形界面服务器登录抖音**：进入用户端 → **凭证** → 「手机扫码登录」。服务器浏览器打开抖音登录页并把二维码显示在网页上，手机抖音扫码即可；如出现安全验证，页面会展示服务器浏览器截图（`GET /api/login/qr`）。

详细步骤见 [完整使用说明 · 部署部分](docs/完整使用说明.md#第一部分--部署)。

---

## ✨ 功能特性

### 核心续火花
- 网页管理：登录态上传 / 网页扫码登录、勾选好友、定时、手动发送、干跑测试
- 好友列表：自动读取聊天好友与火花状态（**保留中**显示为灰色）
- 定时发送：时间抖动 + 随机文案 + 好友间隔，模拟真人节奏
- 发送保护：会话校验、失败重试（最多 3 次）、限流/验证码检测即停
- 补发：发送失败 / 并行占满 / 服务重启后可自动安排补发（最多 3 次）
- 多账号：每个抖音号独立配置、独立日志、独立登录态
- 全站并行数量可调（管理后台「续火花管理」）

### 多用户与授权
- 统一登录入口 `/login`：管理员进入 **`/admin`**，普通用户进入 **`/home`**
- 默认管理员 `admin` / `admin`（务必改密）
- 注册需管理员发放的**注册密钥**（支持批量生成）
- 注册字段：注册码、账号、密码、**邮箱必填**，QQ / 手机号二选一
- **授权模式 / 独立模式**，支持卡密续期（3/7/15 天等，可批量生成、筛选已用/未用）
- 账号等级 V1～V8（原优先级），管理员可调
- 管理员可查看用户、绑定抖音号、设置到期时间

### 邮件与日志
- 任务完成 / 异常 / 验证码 / 账号失效可发邮件
- 每日定时发送日志（时间可配置）；授权到期可提前提醒
- 日志按账号、按天保存：`data/accounts/<账号>/logs/YYYY-MM-DD.log`
- 优美纯文本 / HTML 邮件模板（非黑底控制台样式）
- 管理员可配置 SMTP、测试发件；用户可设置收件邮箱

### AI 续火花文案（Pro）
- 支持 OpenAI 兼容接口（官方或中转，地址需含 `/v1`）
- 可配置：API 地址、Key、模型、系统提示词
- **默认系统提示词**（可改）：根据好友昵称生成短问候，带当前中国时间，24 字以内，开头可加「抖音自动续火花」
- **好友与消息 / 定时**相关页：测试 API 连通、测试提示词（结果只在本页显示）
- 正式发送失败时自动回退消息模板，并写入运行日志

### 管理与统计
- **独立管理后台** `/admin`（与用户主页分离）：续火花管理、用户中心、卡密、邮箱、站点、数据统计
- 全站数据：访问量、用户数、运行次数、续火花成功等
- 访问 IP 记录与归属地（兼容 Cloudflare / 反代头）
- 站点：公告弹窗、联系客服链接、上传登录态教程链接、登录方式开关（网页登录 / 上传 state）
- 服务器访问日志：`data/logs/access.log`（可轮转，后台可查看）

### 界面（v1.6.9）
- 移动端 App 风浅色卡片 UI
- 用户端底部导航六大板块：
  - **概览** · **凭证** · **好友与消息** · **定时** · **日志** · **个人中心**
- 凭证页：网页登录 / 上传登录态；「获取帮助」跳转后台配置的教程链接（仅一个帮助按钮）
- 桌面端底栏加高居中，避免被拉扁
- 宣传页 `/`：立即开始（注册）、登录账号；关页后需重新登录（sessionStorage）

---

## 📦 目录结构

```text
app.py                 FastAPI 入口（路由 + API），VERSION = 1.6.9
manage.sh              启停 / 日志 / 状态菜单
extract_cookie.py      本机提取登录态
requirements.txt
core/
  automation.py        Playwright 发送 / 好友列表 / AI 文案
  scheduler.py         定时、补发、日摘要
  config.py            账号配置 + 授权（licenses.json）
  runtime.py           状态与日志
  auth.py              用户登录 / 注册密钥 / 改密改名
  email_util.py        邮件
  card_keys.py         授权卡密
  site_settings.py     公告 / 客服 / 登录方式 / 帮助链接
  stats.py             全站统计 + 访问 IP
static/
  index.html           宣传落地页
  login.html           登录页
  register.html        注册页
  forgot.html          找回密码（邮箱验证码）
  app.html             用户控制台 /home
  admin.html           管理后台 /admin
  app.css              样式
  boot.js              启动引导与错误提示
  dist/
    app.min.js         生产用（压缩）
    app.raw.js         调试用
  vendor/              本地 Vue / Element Plus / axios
deploy/
  deploy.sh            Debian/Ubuntu 一键部署
  auto-douyin-sparks-pro.service
data/                  运行时生成（已 gitignore，勿提交）
docs/                  使用说明与构建说明
```

> 前端是「多入口 + 共享 JS」：多个 HTML 入口共用 `dist/app.min.js`。改完前端需重新构建，见 `docs/前端构建说明.md`。

---

## 🚀 快速开始

### 环境要求
- Python 3.10+
- 能安装 Playwright Chromium 的环境（Windows / Linux 均可）

### 1. 安装依赖

```bash
git clone https://github.com/SCARZQ/Auto-Douyin-Sparks-Pro.git
cd Auto-Douyin-Sparks-Pro
python3 -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
playwright install chromium
```

### 2. 本地启动

```bash
python app.py
# 浏览器打开 http://127.0.0.1:8000
```

默认管理员：**admin** / **admin**（登录后请立即改密）。

| 路径 | 说明 |
|------|------|
| `/` | 宣传页 |
| `/login` | 登录（管理员 → `/admin`，用户 → `/home`） |
| `/register` | 注册（需注册码） |
| `/home` | 用户主页 |
| `/admin` | 管理后台（仅管理员） |

### 3. 添加抖音号并登录

1. 进入 **概览 / 个人中心** 管理账号，或按界面提示添加抖音账号  
2. **凭证** → **网页扫码登录** 或 **上传 state.json**（旁有「获取帮助」）  
3. **好友与消息** → 获取好友列表 → 勾选要续火花的好友 → 保存  
4. **定时** → 设置发送时间 → 可选开启 AI 文案 → 保存  
5. **日志** 查看运行记录；**个人中心** 可改用户名、改密码、退出登录  

### 4. 本机导出 state（可选）

```bash
python extract_cookie.py
# 扫码登录后生成 state，在网页「凭证」中上传
```

### 5. 服务器部署（Debian / Ubuntu）

```bash
sudo bash deploy/deploy.sh
# 或
bash manage.sh start
```

也可用 Cloudflare Tunnel / frp 做内网穿透。

---

## ⚙️ 配置说明

| 配置 | 位置 | 说明 |
|------|------|------|
| 管理员密码 | 个人中心 / 改密 | 默认 admin/admin |
| 注册密钥 | 管理后台 → 用户中心 | 用户注册必填，可批量 |
| 授权 / 卡密 | 管理后台 → 卡密管理 | 控制是否可用续火花 |
| 并行数量 | 管理后台 → 续火花管理 | 同时跑几个账号 |
| 邮件 SMTP | 管理后台 → 邮箱设置 | 发件、通知开关、到期提醒 |
| 公告 / 客服 / 教程链接 | 管理后台 → 站点 | 用户可见 |
| 登录方式开关 | 管理后台 → 站点 | 网页登录 / 上传 state |
| AI API | 定时相关页（按账号） | base(`/v1`) / sk / 模型 / 提示词 |
| 运行数据 | `data/` | 账号、日志、统计，勿公开 |

环境变量见 `.env.example`（`PORT`、`ACCESS_LOG`、可选 `AUTH_TOKEN` 等）。

---

## 🤖 AI 文案使用

1. 打开 **定时**（或好友与消息中的 AI 相关配置）→ 填写 API 地址（需含 `/v1`）、Key、模型、系统提示词  
2. **测试 API 连通**：检查接口是否可用  
3. **测试提示词**：预览生成文案（仅本页显示，不写系统大日志）  
4. 开启 AI 文案并保存  

兼容 OpenAI 官方及各类中转。正式发送失败时自动使用消息模板兜底。

---

## ⚠️ 风险与建议

- 好友数量建议控制在少量，间隔不要过短  
- 登录态尽量在常用网络环境获取，减少异地风控  
- 出现验证码 / 操作频繁时停止本轮，次日再试  
- 不要把 `data/`、`.env` 提交到公开仓库  

---

## 🔒 安全须知（重要）

### 首次登录后立刻改密

默认管理员 **admin / admin**。上线前**必须**改密码，否则任何人访问你的地址都能进后台。

### data/ 目录等同账号密码

`data/accounts/<账号>/state.json` 是抖音登录态，**泄露即可被他人直接顶号**。

`.gitignore` 已排除 `data/`。请勿 `git add -f data`。

### 服务器访问日志

每次请求可记入 `data/logs/access.log`（IP · 方法 · 路径 · 状态码 · 大小 · 耗时 · UA）。

- 默认开启；`ACCESS_LOG=0` 关闭  
- 按大小轮转：`ACCESS_LOG_MAX_MB`、`ACCESS_LOG_BACKUPS`  
- 后台数据统计可查看；兼容 `CF-Connecting-IP` / `X-Real-IP` / `X-Forwarded-For`  

### 已修复的安全问题（v1.5.0）

| 问题 | 修复 |
|---|---|
| 静态资源 `.gz` 路径穿越 | 规范化路径并校验父目录 |
| `PUT /api/config` 越权改授权 | 非管理员忽略 mode / authorized 等字段 |
| 注册撞目录账号劫持 | 目录归属校验 |
| 忘记密码不吊销会话 | 重置后吊销该用户全部会话 |
| JSON 非原子写 / 损坏当空 | 原子写 + `.corrupt` 备份 |
| runtime 并发丢失 | RLock |
| 登录失效仍跑定时 | failed / expired 拦截 |
| 客服链接 `javascript:` | 统一 `_clean_url()` |

### 已修复的安全与可靠性问题（v1.6.5）

| 问题 | 修复 |
|---|---|
| 忘记密码无验证即可重置 | **邮件验证码**（限频、次数上限；无 SMTP 则禁用） |
| 管理员重置密码不吊销会话 | 与自助改密一致，吊销会话 |
| 补发逻辑未接线 | `schedule_retry` + 启动 `catchup` |
| 授权到期时区错乱 | 统一 Asia/Shanghai |
| AI 提示要时间却未传时间 | user 消息写入北京时间 |
| 联系人子串误匹配 | 精确匹配 |
| 版本号不一致 | 统一 `APP_VERSION` |

### v1.6.9 说明

- 版本号统一为 **1.6.9**
- 用户端底栏文案：**概览 / 凭证 / 好友与消息 / 定时 / 日志 / 个人中心**
- AI 默认系统提示词与配置、自动化模块对齐
- 「获取帮助」用户侧保留单一按钮；后台配置上传教程链接
- 管理员与用户路由分离：`/admin` 与 `/home`

### 仍需注意（设计取舍）

| 项 | 现状 | 建议 |
|---|---|---|
| 用户密码 | 哈希存储；管理端可能保留便于查看的明文副本 | 对外服务可改为只存哈希、后台不展示明文 |
| `AUTH_TOKEN` | 环境变量可作兼容管理员入口 | 不要设，或设为强随机值 |
| SMTP 密码 | `data/email_config.json` 明文 | 用应用专用密码 |
| 访问 IP 归属地 | 可能请求第三方查询 | 介意可关闭相关功能 |

### 前端源码

`static/dist/app.min.js` 已压缩，但前端无法真正隐藏密钥——**不要把 API Key 写进前端**。

---

## 📄 开源说明

本项目在原版 [Auto-Douyin-Sparks](https://github.com/SCARZQ/Auto-Douyin-Sparks) 思路上扩展为多用户 Pro 版，仅供学习与个人自用。

使用本软件即表示你已了解并自行承担相关平台与账号风险。

---

## 🗺️ 版本标签

**Auto-Douyin-Sparks-Pro v1.6.9** — 多用户 · 独立后台 · 授权卡密 · 邮件 · AI 文案 · 六栏移动端 UI · 扫码登录 · 一键部署
