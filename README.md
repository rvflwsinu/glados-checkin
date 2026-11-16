# glados-checkin

GLaDOS 每日自动签到（GitHub Actions）。

## 背景

GLaDOS 于 2025-11 前后升级了反作弊机制，旧的"纯 HTTP 直接 POST"方式已永久失效：

1. 签到前需先 `GET /api/user/checkin/challenge` 获取 nonce；
2. 计算 `proof = SHA-256(nonce + ".glados-observe-v1")`；
3. 机房 IP（GitHub Actions）每次都会被要求 **Cloudflare Turnstile** 人机验证；
4. 服务端校验**登录设备指纹**（navigator.platform + UA），设备不匹配会返回
   `Automated check-in detected. Please sign in again to continue.`。

本仓库的浏览器版工作流使用 Playwright 在 **macOS runner** 上运行真实 Chromium，
自动完成 Turnstile 验证（OpenCV 定位复选框并点击）。

## Workflows

| 文件 | 说明 |
|---|---|
| `.github/workflows/glados-checkin-browser.yml` | **主工作流**：每天北京时间 9:30 浏览器签到，支持手动触发 |
| `.github/workflows/glados-checkin.yml` | 旧纯 HTTP 版，仅供调试，无定时 |

## Secrets 配置（Settings → Secrets and variables → Actions）

| Secret | 必填 | 说明 |
|---|---|---|
| `GLADOS_EMAIL_1` / `GLADOS_COOKIE_1` | ✅ | 账号1 邮箱 / 浏览器 Cookie（F12 → Application → Cookies，整串复制） |
| `GLADOS_EMAIL_2` / `GLADOS_COOKIE_2` | ⬜ | 账号2，以此类推 |
| `GLADOS_UA` | ⬜ | 自定义 UA。**默认按 macOS Chrome 构造，与 macOS runner 指纹一致，无需配置**；仅当你的登录设备不是 Mac 时才需要设置为该设备的浏览器 UA |
| `TG_BOT_TOKEN` / `TG_CHAT_ID` | ⬜ | Telegram 通知 |

注意：
- Cookie 过期（约一个月）后需重新登录 glados.cloud 获取，否则签到页打不开；
- 若你更换了登录设备（如 Windows），请同步更新 `GLADOS_UA` 并确认
  workflow 使用对应的 runner（Windows 登录 → 改用 `windows-latest`）。

## 本地调试

```bash
pip install playwright pillow
playwright install chromium
GLADOS_EMAIL_1=xxx GLADOS_COOKIE_1='koa:sess=...' python checkin_browser.py
```
