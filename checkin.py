# checkin.py（已修复域名）
import requests
import datetime
import random
import os
import time
from dotenv import load_dotenv

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

def generate_headers(cookie):
    user_agents = [
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/144.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Linux; Android 10; SM-G9750) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/144.0.0.0 Mobile Safari/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/14.0.1 Safari/605.1.15",
    ]
    return {
        "Accept": "application/json, text/plain, */*",
        "Accept-Encoding": "gzip, deflate",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Content-Type": "application/json;charset=UTF-8",
        "Cookie": cookie,
        "Origin": "https://glados.cloud",          # ← 修复
        "Referer": "https://glados.cloud/",
        "User-Agent": random.choice(user_agents)
    }

def format_days(days_str):
    days = float(days_str)
    if days.is_integer():
        return str(int(days))
    return f"{days:.8f}".rstrip('0').rstrip('.')

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

def check_account_status(email, cookie):
    url = "https://glados.cloud/api/user/status"          # ← 修复
    headers = generate_headers(cookie)
    try:
        response = requests.get(url, headers=headers, timeout=15)
        response.raise_for_status()
        data = response.json()
        left_days = format_days(data['data']['leftDays'])
        return f"<b>{email}</b>: 剩余 {left_days} 天 🗓️"
    except Exception as e:
        return f"<b>{email}</b>: 获取状态失败 - {str(e)} ❌"

def print_response_debug(response):
    """请求失败时打印诊断信息，便于排查 Cloudflare 拦截等问题"""
    try:
        print(f"[debug] HTTP status: {response.status_code}")
        print(f"[debug] headers: {dict(response.headers)}")
        print(f"[debug] body[:500]: {response.text[:500]!r}")
    except Exception as e:
        print(f"[debug] 打印响应信息失败: {e}")

def sign(email, cookie):
    url = "https://glados.cloud/api/user/checkin"         # ← 修复
    headers = generate_headers(cookie)
    data = {"token": "glados.cloud"}                       # ← 修复
    response = None
    try:
        response = requests.post(url, headers=headers, json=data, timeout=15)
        response.raise_for_status()
        raw_message = response.json().get("message", "")
        translated_message = translate_message(raw_message)
    except (requests.RequestException, ValueError) as e:
        translated_message = f"请求失败: {e}"
        if response is not None:
            print_response_debug(response)
        else:
            print(f"[debug] 无响应对象，异常: {type(e).__name__}: {e}")

    beijing_time = datetime.datetime.utcnow() + datetime.timedelta(hours=8)
    print(f"{beijing_time.strftime('%Y-%m-%d %H:%M')} {email}: {translated_message}")
    return f"<b>{email}</b>: {translated_message}"

def multi_account_sign():
    load_dotenv()
    bot_token = os.getenv("TG_BOT_TOKEN")
    chat_id = os.getenv("TG_CHAT_ID")

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
        sign_messages.append(sign(email, cookie))
        status_messages.append(check_account_status(email, cookie))
        if len(accounts) > 1:
            time.sleep(random.randint(5, 15))

    send_notification(sign_messages, status_messages, bot_token, chat_id)

if __name__ == "__main__":
    multi_account_sign()
