# checkin.py — 适配 GLaDOS 新版签到（challenge + proof + 可选 Turnstile）
import requests
import datetime
import hashlib
import random
import os
import time
from dotenv import load_dotenv

BASE_URL = "https://glados.cloud"
CHALLENGE_URL = f"{BASE_URL}/api/user/checkin/challenge"
CHECKIN_URL = f"{BASE_URL}/api/user/checkin"
STATUS_URL = f"{BASE_URL}/api/user/status"
PROOF_SALT = "glados-observe-v1"   # 取自 glados.cloud 前端 JS
MIN_CHALLENGE_DELAY = 2.6          # 前端要求挑战下发后至少 2.5s 才提交


def translate_message(raw_message):
    if raw_message == "Please Try Tomorrow":
        return "签到失败，请明天再试 🤖"
    elif "Checkin! Got" in raw_message:
        points = raw_message.split("Got ")[1].split(" Points")[0]
        return f"签到成功，获得{points}积分 🎉"
    elif raw_message == "Checkin Repeats! Please Try Tomorrow":
        return "重复签到，请明天再试 🔁"
    else:
        return f"未知的签到结果: {raw_message} ❓"


def generate_headers(cookie, user_agent):
    return {
        "Accept": "application/json, text/plain, */*",
        "Accept-Encoding": "gzip, deflate",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Content-Type": "application/json;charset=UTF-8",
        "Cookie": cookie,
        "Origin": BASE_URL,
        "Referer": f"{BASE_URL}/console/checkin",
        "User-Agent": user_agent,
    }


def print_response_debug(response):
    """请求失败时打印诊断信息，便于排查 Cloudflare 拦截等问题"""
    try:
        print(f"[debug] HTTP status: {response.status_code}")
        print(f"[debug] headers: {dict(response.headers)}")
        print(f"[debug] body[:500]: {response.text[:500]!r}")
    except Exception as e:
        print(f"[debug] 打印响应信息失败: {e}")


def get_challenge(cookie, headers):
    """第一步：获取签到挑战 nonce"""
    response = requests.get(CHALLENGE_URL, headers=headers, timeout=15)
    response.raise_for_status()
    data = response.json()
    if data.get("code") != 0:
        raise RuntimeError(f"获取挑战失败: {data}")
    return data


def compute_proof(nonce):
    """第二步：proof = SHA-256(nonce + '.' + 'glados-observe-v1')，与前端一致"""
    return hashlib.sha256(f"{nonce}.{PROOF_SALT}".encode()).hexdigest()


def sign(email, cookie, user_agent):
    headers = generate_headers(cookie, user_agent)
    response = None
    try:
        # 1. 取挑战
        challenge = get_challenge(cookie, headers)
        nonce = challenge["nonce"]
        need_turnstile = bool(challenge.get("turnstile"))
        challenge_time = time.time()
        print(f"[debug] nonce={nonce}, ttl={challenge.get('ttl')}, "
              f"turnstile={'需要' if need_turnstile else '不需要'}")

        # 2. 计算 proof
        proof = compute_proof(nonce)

        # 3. 前端要求挑战下发后至少 2.5s 再提交
        elapsed = time.time() - challenge_time
        if elapsed < MIN_CHALLENGE_DELAY:
            time.sleep(MIN_CHALLENGE_DELAY - elapsed)

        # 4. 提交签到
        #    若服务端要求 Turnstile，纯 requests 无法取得令牌，
        #    发送 tsState="timeout" 让服务端给出明确拒绝原因
        payload = {
            "token": "glados.cloud",
            "nonce": nonce,
            "proof": proof,
            "tsState": "timeout" if need_turnstile else "off",
        }
        if need_turnstile:
            print("[warn] 本次挑战要求 Cloudflare Turnstile 验证，"
                  "纯 HTTP 请求无法通过，尝试提交以确认服务端行为...")

        response = requests.post(CHECKIN_URL, headers=headers, json=payload, timeout=15)
        response.raise_for_status()
        result = response.json()
        raw_message = result.get("message", "")
        if raw_message:
            translated_message = translate_message(raw_message)
        else:
            translated_message = f"签到接口返回: {result}"
    except (requests.RequestException, ValueError) as e:
        translated_message = f"请求失败: {e}"
        if response is not None:
            print_response_debug(response)
        else:
            print(f"[debug] 无响应对象，异常: {type(e).__name__}: {e}")
    except Exception as e:
        translated_message = f"执行失败: {e}"

    beijing_time = datetime.datetime.utcnow() + datetime.timedelta(hours=8)
    print(f"{beijing_time.strftime('%Y-%m-%d %H:%M')} {email}: {translated_message}")
    return f"<b>{email}</b>: {translated_message}"


def format_days(days_str):
    days = float(days_str)
    if days.is_integer():
        return str(int(days))
    return f"{days:.8f}".rstrip('0').rstrip('.')


def check_account_status(email, cookie, user_agent):
    headers = generate_headers(cookie, user_agent)
    try:
        response = requests.get(STATUS_URL, headers=headers, timeout=15)
        response.raise_for_status()
        data = response.json()
        left_days = format_days(data['data']['leftDays'])
        return f"<b>{email}</b>: 剩余 {left_days} 天 🗓️"
    except Exception as e:
        return f"<b>{email}</b>: 获取状态失败 - {str(e)} ❌"


def send_notification(sign_messages, status_messages, bot_token, chat_id):
    if not bot_token or not chat_id:
        print("未配置 Telegram，跳过通知")
        return
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    sign_text = "🔔 GLaDOS 签到结果:\n" + "\n".join(sign_messages)
    status_text = "\n⏳ GLaDOS 账号状态:\n" + "\n".join(status_messages)
    beijing_time = datetime.datetime.utcnow() + datetime.timedelta(hours=8)
    current_time = beijing_time.strftime("%Y-%m-%d %H:%M")
    text = f"🕒 当前时间: {current_time}\n\n{sign_text}\n{status_text}\n\n✅ 签到任务完成"
    try:
        response = requests.post(url, data={"chat_id": chat_id, "text": text, "parse_mode": "HTML"})
        response.raise_for_status()
        print("Telegram 通知发送成功")
    except requests.RequestException as e:
        print(f"发送 Telegram 消息失败: {e}")


def multi_account_sign():
    load_dotenv()
    bot_token = os.getenv("TG_BOT_TOKEN")
    chat_id = os.getenv("TG_CHAT_ID")

    # 同一轮签到内所有账号使用同一个 UA。
    # 服务端会校验 device-mismatch：建议把它设为你登录 GLaDOS 时
    # 浏览器所用的 UA（GitHub Secret 中配置 GLADOS_UA），
    # 否则每次随机 UA 容易被判定为自动化签到。
    user_agent = os.getenv("GLADOS_UA") or (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    )

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

    sign_messages, status_messages = [], []
    for email, cookie in accounts:
        sign_messages.append(sign(email, cookie, user_agent))
        status_messages.append(check_account_status(email, cookie, user_agent))
        if len(accounts) > 1:
            time.sleep(random.randint(5, 15))

    send_notification(sign_messages, status_messages, bot_token, chat_id)


if __name__ == "__main__":
    multi_account_sign()
