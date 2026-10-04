# data 目录（运行时数据）

本目录在首次运行时会自动创建以下内容，**请勿提交到 Git**：

```
users.json          用户账号与密码
sessions.json       登录会话
licenses.json       授权天数（独立于账号，防误删）
card_keys.json      卡密
invite_keys.json    注册密钥
email_config.json   SMTP 配置（含密码）
site_settings.json  站点设置
visit_ips.json      访问 IP 记录
accounts/           各抖音账号的登录态与日志
  <账号>/
    state.json      抖音登录态（敏感！等同于账号密码）
    config.json     该账号的定时与好友配置
    runtime.json    运行状态
    logs/           按天日志
```

`state.json` 等同于账号登录凭证，泄露后可被他人直接顶号，务必确保 `data/` 不出现在任何公开仓库中。
