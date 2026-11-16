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


def _dump_turnstile_debug(page):
    """打印所有 frame 及可能的复选框元素，用于定位选择器问题"""
    print("[debug] frame 结构:")
    for f in page.frames:
        print(f"  frame: {f.url[:100]}")
        try:
            inputs = f.locator("input").all()
            for inp in inputs[:5]:
                try:
                    t = inp.get_attribute("type")
                    cls = inp.get_attribute("class") or ""
                    vis = inp.is_visible()
                    print(f"    input type={t} class={cls[:30]} visible={vis}")
                except Exception:
                    pass
        except Exception:
            pass
    try:
        page.screenshot(path="debug-turnstile-state.png")
        print("[debug] 已保存 debug-turnstile-state.png")
    except Exception:
        pass


def find_white_widget_box(page):
    """截图并寻找 Turnstile 白底 widget 框 (暗色页面中唯一大块白色区域)。
    返回 (cx, cy) 复选框中心点(页面CSS坐标) 或 None。"""
    try:
        from PIL import Image
        import io
        png = page.screenshot(full_page=False)
        img = Image.open(io.BytesIO(png)).convert("RGB")
        w, h = img.size
        px = img.load()
        # 白色掩码 (widget 背景 ~ #fafafa)
        mask = set()
        step = 2  # 降采样加速
        for y in range(0, h, step):
            for x in range(0, w, step):
                r, g, b = px[x, y]
                # widget 背景 ~ #fafafa; "Done for today" 按钮偏灰(<=236), 排除
                if r > 240 and g > 240 and b > 238:
                    mask.add((x // step, y // step))
        if not mask:
            return None
        # 最大连通块 (4连通 BFS)
        best = None
        seen = set()
        for p in list(mask):
            if p in seen:
                continue
            stack = [p]
            seen.add(p)
            comp = []
            while stack:
                c = stack.pop()
                comp.append(c)
                cx, cy = c
                for n in ((cx+1, cy), (cx-1, cy), (cx, cy+1), (cx, cy-1)):
                    if n in mask and n not in seen:
                        seen.add(n)
                        stack.append(n)
            if best is None or len(comp) > len(best):
                best = comp
        xs = [c[0] for c in best]
        ys = [c[1] for c in best]
        bw = (max(xs) - min(xs) + 1) * step
        bh = (max(ys) - min(ys) + 1) * step
        # Turnstile 交互框约 300x65; 放宽以覆盖收起态
        if 120 <= bw <= 450 and 35 <= bh <= 120:
            x0, y0 = min(xs) * step, min(ys) * step
            print(f"[debug] CV定位到 widget 白框 {bw}x{bh} @({x0},{y0})", flush=True)
            return x0 + 26, y0 + bh / 2
        print(f"[debug] CV最大白块 {bw}x{bh} 不符合 widget 尺寸", flush=True)
    except Exception as e:
        print(f"[debug] CV定位异常: {e}", flush=True)
    return None


def click_turnstile_checkbox(page, timeout_s=25):
    """检测 Turnstile 交互式复选框并点击。返回是否点击成功。
    策略: 1) CV 定位白色 widget 框并坐标点击(最可靠)
          2) 在嵌套 iframe 中找 input[type=checkbox]"""
    deadline = time.time() + timeout_s
    clicked = False
    while time.time() < deadline and not clicked:
        # 策略1: CV 坐标定位
        pt = find_white_widget_box(page)
        if pt:
            page.mouse.click(pt[0], pt[1], delay=80)
            print("[debug] 已按CV坐标点击 Turnstile 复选框", flush=True)
            clicked = True
            break
        # 策略2: 元素定位
        for frame in page.frames:
            if "challenges.cloudflare.com" not in frame.url:
                continue
            try:
                box = frame.locator("input[type='checkbox']").first
                if box.count() > 0 and box.is_visible() and box.is_enabled():
                    box.click(timeout=3000)
                    print("[debug] 已点击 Turnstile 复选框(元素定位)", flush=True)
                    clicked = True
                    break
            except Exception:
                pass
        if clicked:
            break
        page.wait_for_timeout(1000)
    return clicked


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
                # 该 IP 被判为高风险时, Cloudflare 需要交互验证。
                # 实测复选框约 90-120s 才出现(先跑长时间后台验证);
                # 挑战 nonce ttl=300s, 需在 5 分钟内完成。
                print("[debug] 等待 Turnstile 复选框出现(最多150s)...")
                t0 = time.time()
                if click_turnstile_checkbox(page, timeout_s=150):
                    print(f"[debug] 复选框于 {int(time.time()-t0)}s 时出现，"
                          "等待令牌验证完成 (15s)...")
                    page.wait_for_timeout(15000)
                else:
                    print("[debug] 150s 内复选框未出现，将直接点击签到")

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
