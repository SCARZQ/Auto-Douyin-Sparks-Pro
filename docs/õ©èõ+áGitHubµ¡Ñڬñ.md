# 上传到 GitHub 步骤

## 一、先确认没有敏感文件

本项目根目录执行：

```bash
git status --ignored
```

**必须看到** `data/` 在 ignored 列表里。如果 `data/` 出现在待提交列表，立刻停止，检查 `.gitignore`。

关键原则：
- `data/` 里有抖音登录态（`state.json`），泄露 = 账号被顶
- `data/email_config.json` 有 SMTP 密码
- `.env` 可能有 `AUTH_TOKEN`

本项目自带的 `.gitignore` 已排除这些，但**请自己再核对一遍**。

---

## 二、创建仓库并上传

### 方式 A：网页上传（最简单，不用装 Git）

1. GitHub 右上角 **+** → **New repository**
2. 填写：
   - Repository name: `Auto-Douyin-Sparks-Pro`
   - 可见性：**Private**（强烈建议先私有）
   - **不要**勾选 "Add a README file"
3. 创建后点 **uploading an existing file**
4. 把解压出的 `Auto-Douyin-Sparks-Pro` 文件夹里**所有内容**拖进去
   - ⚠️ 拖的是**文件夹里面的内容**，不是整个文件夹
   - ⚠️ `data/` 不要拖进去
5. Commit

### 方式 B：命令行（推荐，后续好维护）

```bash
cd Auto-Douyin-Sparks-Pro

git init
git add .
git commit -m "feat: Auto-Douyin-Sparks-Pro 初始版本

- 多用户 + 授权卡密 + 邮件通知 + AI 文案
- 前端多入口（宣传/登录/注册/找回密码/控制台/后台）
- 压缩混淆 + gzip 预压缩，首屏 2057KB -> 503KB
- 粒子背景性能优化、好友列表渐进渲染"

git branch -M main
git remote add origin https://github.com/<你的用户名>/Auto-Douyin-Sparks-Pro.git
git push -u origin main
```

推送前最后确认一次：

```bash
git ls-files | grep -i "data/\|\.env$"
```

**这条命令必须没有任何输出。** 有输出就说明敏感文件被追踪了。

---

## 三、如果误传了敏感文件

```bash
# 1. 从 Git 移除（保留本地文件）
git rm -r --cached data/
git rm --cached .env
git commit -m "chore: 移除敏感文件"

# 2. 如果已经 push 过，历史里还有，需要清历史
#    （用 git filter-repo 或 BFG，或直接删库重传）
```

**重要**：删掉文件再提交，**历史记录里仍然存在**，别人 clone 旧 commit 还是能拿到。
如果 `state.json` 曾经上传过，**立刻去抖音重新登录一次**让旧登录态失效。

---

## 四、建议的仓库设置

| 设置 | 位置 | 建议 |
|---|---|---|
| 可见性 | Settings → General | 先用 **Private** 跑通再考虑公开 |
| 密钥扫描 | Settings → Code security | 开启 **Secret scanning** |
| 依赖告警 | 同上 | 开启 **Dependabot alerts** |
| 分支保护 | Settings → Branches | 公开后建议开启 |

---

## 五、开源前再想一遍

这个项目会**自动向抖音发私信**，公开后可能：

- 被批量用于营销，加剧平台风控
- 被平台注意到，影响所有使用者的账号

原版 README 已写了「仅限个人自用」，但**公开代码等于放弃这个约束**。

如果只是自己用，**保持 Private** 是最稳妥的选择。
