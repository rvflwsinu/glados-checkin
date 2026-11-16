# checkin_browser.py — 使用真实浏览器(Playwright)完成 GLaDOS 签到
# 前端会自动处理 challenge + proof + Cloudflare Turnstile
import os
import datetime
import time
from playwright.sync_api import sync_playwright

BASE = "https://glados.cloud"
CHECKIN_PAGE = f"{BASE}/console/checkin"
HEADLESS = os.getenv("HEADLESS", "0") == "1"


def parse_cookies(cookie_str):
    cookies = []
    for chunk in cookie_str.split(";"):
        chunk = chunk.strip()
        if "=" not in chunk:
            continue
        name, value = chunk.split("=", 1)
        cookies.append({
            "name": name.strip(),
            "value": value.strip(),
            "domain": ".glados.cloud",
            "path": "/",
            "secure": True,
            "httpOnly": False,
        })
    return cookies


def translate_result(data):
    code = data.get("code")
    message = data.get("message", "")
    if code == 0:
        if "Checkin! Got" in message:
            points = message.split("Got ")[1].split(" Points")[0]
            return f"签到成功，获得 {points} 积分 🎉"
        return f"签到成功: {message or data} 🎉"
    if message == "Checkin Repeats! Please Try Tomorrow":
        return "重复签到，请明天再试 🔁"
    if message == "Please Try Tomorrow":
        return "签到失败，请明天再试 🤖"
    return f"签到失败 (code={code}): {message or data} ❓"


def build_macos_ua(browser_version):
    """用真实浏览器版本构造 macOS Chrome UA，与登录设备保持一致"""
    major = browser_version.split(".")[0]
    return (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        f"AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{major}.0.0.0 Safari/537.36"
    )


def click_turnstile_checkbox(page, timeout_s=25):
    """检测 Turnstile 交互式复选框并点击。返回是否点击成功。
    页面加载后 widget 即渲染; 若 Cloudflare 要求交互, 复选框会出现在嵌套 iframe 中。"""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        for frame in page.frames:
            if "challenges.cloudflare.com" not in frame.url:
                continue
            try:
                box = frame.locator("input[type='checkbox']").first
                if box.count() > 0 and box.is_visible() and box.is_enabled():
                    box.click(timeout=3000)
                    print("[debug] 已点击 Turnstile 复选框")
                    return True
            except Exception:
                pass
        page.wait_for_timeout(500)
    return False


def checkin_one(email, cookie, user_agent):
    print(f"\n===== 账号 {email} =====")
    challenge_info = {}
    checkin_results = []
    page_errors = []

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=HEADLESS,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage",
            ],
        )
        context = browser.new_context(
            user_agent=user_agent or build_macos_ua(browser.version),
            locale="zh-CN",
            timezone_id="Asia/Shanghai",
            viewport={"width": 1366, "height": 900},
        )
        # 隐藏 webdriver 标记
        context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
        )
        context.add_cookies(parse_cookies(cookie))
        page = context.new_page()

        def on_response(response):
            url = response.url
            if url.endswith("/api/user/checkin/challenge"):
                try:
                    challenge_info.update(response.json())
                except Exception:
                    pass
            elif "/api/user/checkin" in url and "challenge" not in url:
                try:
                    checkin_results.append(response.json())
                except Exception:
                    checkin_results.append({"_http": response.status})

        def on_request(request):
            url = request.url
            if "/api/user/checkin" in url and "challenge" not in url:
                try:
                    import json as _json
                    body = _json.loads(request.post_data or "{}")
                    # 脱敏: 只显示令牌前 8 位
                    if body.get("turnstile"):
                        body["turnstile"] = body["turnstile"][:8] + "..."
                    print(f"[debug] checkin 请求体: {body}")
                except Exception:
                    print(f"[debug] checkin 请求体无法解析: {request.post_data!r}")
            if "challenges.cloudflare.com" in url:
                print(f"[debug] turnstile 相关请求: {url[:100]}")

        page.on("response", on_response)
        page.on("request", on_request)
        page.on("pageerror", lambda e: page_errors.append(str(e)))

        try:
            page.goto(CHECKIN_PAGE, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(4000)  # 等待前端加载挑战
        except Exception as e:
            print(f"打开签到页面失败: {e}")
            page.screenshot(path=f"debug-{email}.png", full_page=True)
            browser.close()
            return f"<b>{email}</b>: 打开页面失败 ❌"

        if challenge_info:
            need_ts = bool(challenge_info.get("turnstile"))
            print(f"[debug] challenge code={challenge_info.get('code')}, "
                  f"turnstile={'需要' if need_ts else '不需要'}")
            if need_ts:
                # 页面加载后 widget 已开始运行; 尝试提前点击复选框
                click_turnstile_checkbox(page, timeout_s=8)

        # 找签到按钮
        btn = None
        for name in ["签到", "Checkin", "簽到"]:
            loc = page.get_by_role("button", name=name)
            if loc.count() > 0:
                btn = loc.first
                break
        if btn is None:
            loc = page.locator("button:has-text('签到'), button:has-text('Checkin')")
            if loc.count() > 0:
                btn = loc.first

        if btn is None:
            print("未找到签到按钮，可能是 cookie 失效或页面结构变化")
            print(f"[debug] 页面标题: {page.title()}")
            page.screenshot(path=f"debug-{email}.png", full_page=True)
            browser.close()
            return f"<b>{email}</b>: 未找到签到按钮（cookie 可能失效）❌"

        try:
            if not btn.is_visible() or not btn.is_enabled():
                print("签到按钮不可点击，可能今日已完成")
                browser.close()
                return f"<b>{email}</b>: 今日已签到 ✅"
            btn.click(timeout=10000)
        except Exception as e:
            print(f"点击签到按钮失败: {e}")
            page.screenshot(path=f"debug-{email}.png", full_page=True)
            browser.close()
            return f"<b>{email}</b>: 点击签到按钮失败 ❌"

        # 等待签到 API 返回；期间若出现 Turnstile 复选框则立即点击。
        # 前端在需要交互时最多等待 90s 才提交。
        deadline = time.time() + 100
        ts_clicked = False
        while time.time() < deadline and not checkin_results:
            if not ts_clicked and challenge_info.get("turnstile"):
                if click_turnstile_checkbox(page, timeout_s=3):
                    ts_clicked = True
            page.wait_for_timeout(500)
        if not ts_clicked and challenge_info.get("turnstile"):
            print("[debug] 等待期间未点击到 Turnstile 复选框")

        # 多等一会儿让结算消息渲染
        page.wait_for_timeout(2000)
        if not checkin_results:
            print("[warn] 90 秒内未捕获到签到 API 响应")
            print(f"[debug] 页面可见文本: {page.locator('body').inner_text()[:500]}")
        # 每次都截图，便于排查 Turnstile 状态
        try:
            page.screenshot(path=f"debug-{email}.png", full_page=True)
            print("[debug] 已保存截图 debug-" + email + ".png")
        except Exception as e:
            print(f"[debug] 截图失败: {e}")

        browser.close()

    if checkin_results:
        result = translate_result(checkin_results[-1])
    else:
        result = "未获取到签到结果（可能被 Turnstile 拦截） ❌"
    print(f"结果: {result}")
    return f"<b>{email}</b>: {result}"


def send_telegram(messages, bot_token, chat_id):
    if not bot_token or not chat_id:
        print("未配置 Telegram，跳过通知")
        return
    import requests
    beijing = datetime.datetime.utcnow() + datetime.timedelta(hours=8)
    text = (f"🕒 当前时间: {beijing.strftime('%Y-%m-%d %H:%M')}\n\n"
            "🔔 GLaDOS 浏览器签到结果:\n" + "\n".join(messages))
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{bot_token}/sendMessage",
            data={"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
            timeout=15,
        )
        r.raise_for_status()
        print("Telegram 通知发送成功")
    except Exception as e:
        print(f"发送 Telegram 消息失败: {e}")


def main():
    accounts = []
    i = 1
    while True:
        email = os.getenv(f"GLADOS_EMAIL_{i}")
        cookie = os.getenv(f"GLADOS_COOKIE_{i}")
        if not email or not cookie:
            break
        accounts.append((email, cookie))
        i += 1

    if not accounts:
        print("未找到账号信息，请检查环境变量")
        return

    user_agent = os.getenv("GLADOS_UA") or None  # 默认在浏览器启动后按 macOS 构造
    retry_markers = ("Automated check-in", "Cloudflare verification failed",
                     "未获取到签到结果", "点击签到按钮失败", "打开页面失败")
    results = []
    for email, cookie in accounts:
        r = checkin_one(email, cookie, user_agent)
        if any(m in r for m in retry_markers):
            print(f"[warn] {email} 首次尝试失败，30 秒后重试一次...")
            time.sleep(30)
            r = checkin_one(email, cookie, user_agent)
        results.append(r)

    send_telegram(results, os.getenv("TG_BOT_TOKEN"), os.getenv("TG_CHAT_ID"))


if __name__ == "__main__":
    main()
