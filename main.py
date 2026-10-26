import asyncio
import aiohttp
import logging
import re
import ssl
import math
import time

from datetime import datetime, timezone
from urllib.parse import urlparse, parse_qs

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup
)

from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    CallbackQueryHandler,
    filters
)

# ====================================
# LOGGING
# ====================================

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)

logger = logging.getLogger(__name__)

# ====================================
# CONSTANTS
# ====================================

ITEMS_PER_PAGE = 8

# ====================================
# HELPER FUNCTIONS
# ====================================

def extract_account_info(link: str):
    try:
        parsed = urlparse(link)
        if not parsed.hostname:
            return None
        if parsed.port:
            domain = f"{parsed.scheme}://{parsed.hostname}:{parsed.port}"
        else:
            domain = f"{parsed.scheme}://{parsed.hostname}"
        qs = parse_qs(parsed.query)
        username = qs.get("username", [None])[0]
        password = qs.get("password", [None])[0]
        if not username or not password:
            return None
        return domain, username, password
    except:
        return None

def build_get_link(domain, username, password):
    if not domain.startswith("http"):
        domain = "http://" + domain
    return f"{domain}/get.php?username={username}&password={password}&type=m3u_plus", domain

async def check_account(session, domain, username, password):
    try:
        url = f"{domain}/player_api.php?username={username}&password={password}"
        async with session.get(url, ssl=False, timeout=15) as resp:
            if resp.status != 200:
                return None
            data = await resp.json(content_type=None)
            user_info = data.get("user_info", {})
            status = str(user_info.get("status", "")).lower()
            if user_info and status in ["active", "enabled"]:
                exp = str(user_info.get("exp_date", "0"))
                if exp.isdigit() and exp != "0":
                    exp_date = datetime.fromtimestamp(int(exp), tz=timezone.utc).strftime("%Y-%m-%d")
                else:
                    exp_date = "غير معروف"
                return {
                    "status": status.capitalize(),
                    "exp_date": exp_date,
                    "max_connections": user_info.get("max_connections", "غير معروف")
                }
            return None
    except asyncio.TimeoutError:
        return None
    except:
        return None

async def get_data(session, url):
    try:
        async with session.get(url, ssl=False, timeout=15) as resp:
            if resp.status == 200:
                return await resp.json(content_type=None)
            return []
    except asyncio.TimeoutError:
        return []
    except:
        return []

async def get_real_stream_url(session, domain, username, password, item_id, stream_type="live"):
    """
    تجيب رابط البث الحقيقي من API السيرفر
    ترجع m3u8 اذا متوفر، اذا لا ترجع ts، اذا لا ترجع mp4/mkv
    """
    try:
        # 1. نجيب بيانات العنصر من API
        if stream_type == "live":
            api_url = f"{domain}/player_api.php?username={username}&password={password}&action=get_live_streams"
        elif stream_type == "vod":
            api_url = f"{domain}/player_api.php?username={username}&password={password}&action=get_vod_streams"
        else:
            api_url = f"{domain}/player_api.php?username={username}&password={password}&action=get_series"
        
        async with session.get(api_url, ssl=False, timeout=15) as resp:
            if resp.status == 200:
                data = await resp.json(content_type=None)
                
                # ندور على العنصر المطلوب
                for item in data:
                    if str(item.get("stream_id", "")) == str(item_id):
                        # نبحث عن الامتداد بكل الحقول الممكنة
                        ext = ""
                        
                        # الحقول اللي ممكن تحتوي الامتداد
                        for field in ["container_extension", "stream_extension", "extension", "ext", "format", "stream_format"]:
                            val = item.get(field, "")
                            if val and val not in ["", "null", "None"]:
                                ext = val.lower().strip()
                                break
                        
                        # إذا ما لقينا ext، نشوف اسم الملف
                        if not ext:
                            stream_name = item.get("name", "")
                            if "." in stream_name:
                                ext = stream_name.split(".")[-1].lower()
                        
                        # نرجع الرابط حسب النوع والامتداد
                        if stream_type == "live":
                            if ext in ["m3u8", "hls"]:
                                return f"{domain}/live/{username}/{password}/{item_id}.m3u8"
                            elif ext in ["mpd", "dash"]:
                                return f"{domain}/live/{username}/{password}/{item_id}.mpd"
                            else:
                                # للبث المباشر، نجرب m3u8 اولاً لان اغلب السيرفرات تدعمه
                                return f"{domain}/live/{username}/{password}/{item_id}.m3u8"
                        else:
                            if ext:
                                return f"{domain}/{stream_type}/{username}/{password}/{item_id}.{ext}"
                            else:
                                return f"{domain}/{stream_type}/{username}/{password}/{item_id}.mp4"
            
            # 2. إذا ما لكينا البيانات، نجرب الرابط المباشر m3u8
            if stream_type == "live":
                test_url = f"{domain}/live/{username}/{password}/{item_id}.m3u8"
                try:
                    async with session.head(test_url, ssl=False, timeout=5) as test:
                        if test.status == 200:
                            return test_url
                except:
                    pass
                
                # نجرب ts
                test_url = f"{domain}/live/{username}/{password}/{item_id}.ts"
                return test_url
            else:
                test_url = f"{domain}/{stream_type}/{username}/{password}/{item_id}.mp4"
                return test_url
                
    except:
        # إذا كله فشل، نرجع الرابط الافتراضي
        if stream_type == "live":
            return f"{domain}/live/{username}/{password}/{item_id}.m3u8"
        else:
            return f"{domain}/{stream_type}/{username}/{password}/{item_id}.mp4"

def format_success(n, domain, username, password, info, link):
    return f"""📌 الحساب {n}

✅ الحساب يعمل بنجاح

🌐 الدومين: `{domain}`
👤 اليوزر: `{username}`
🔑 الباسورد: `{password}`
⏳ تاريخ الانتهاء: `{info['exp_date']}`
🔗 عدد الاتصالات: `{info['max_connections']}`
📊 الحالة: `{info['status']}`
🔗 رابط الـGET:
`{link}`"""

def format_summary(ok, bad):
    return f"""📊 نتيجة الفحص
══════════════════
✅ الناجحة: {ok}
❌ الفاشلة: {bad}
══════════════════"""

def extract_urls(text):
    lines = text.splitlines()
    valid = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        match = re.search(r'https?://[^\s]+', line)
        if not match:
            continue
        url = match.group(0).replace("\\", "").replace('"', "").replace("'", "")
        if "get.php" in url and "username=" in url and "password=" in url:
            valid.append(url)
    return list(dict.fromkeys(valid))

def main_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📺 الباقات", callback_data="btn_live")],
        [InlineKeyboardButton("🎬 افلام", callback_data="btn_vod"),
         InlineKeyboardButton("🎞 مسلسلات", callback_data="btn_series")]
    ])

def build_paginated_keyboard(items, prefix, page=1):
    total_pages = max(1, math.ceil(len(items) / ITEMS_PER_PAGE))
    page = max(1, min(page, total_pages))
    
    start = (page - 1) * ITEMS_PER_PAGE
    end = start + ITEMS_PER_PAGE
    page_items = items[start:end]
    
    keyboard = []
    
    for i, item in enumerate(page_items):
        item_name = str(item.get("name", item.get("category_name", "غير معروف")))[:50]
        keyboard.append([InlineKeyboardButton(item_name, callback_data=f"{prefix}_{start + i}")])
    
    nav_row = []
    if page > 1:
        nav_row.append(InlineKeyboardButton("⬅️", callback_data=f"{prefix}_pg_{page-1}"))
    nav_row.append(InlineKeyboardButton(f"{page}/{total_pages}", callback_data="noop"))
    if page < total_pages:
        nav_row.append(InlineKeyboardButton("➡️", callback_data=f"{prefix}_pg_{page+1}"))
    if nav_row:
        keyboard.append(nav_row)
    
    if prefix in ["items_live", "items_vod", "items_series"]:
        back = "cats_live" if prefix == "items_live" else ("cats_vod" if prefix == "items_vod" else "cats_series")
        keyboard.append([InlineKeyboardButton("🔙 رجوع للفئات", callback_data=back)])
    
    keyboard.append([InlineKeyboardButton("🏠 القائمة الرئيسية", callback_data="btn_main")])
    
    return InlineKeyboardMarkup(keyboard), page, total_pages

# ====================================
# BUTTON HANDLER
# ====================================

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data
    
    if data == "noop":
        return
    
    # نتحقق من صلاحية الاستعلام
    try:
        await query.answer()
    except:
        pass
    
    account = context.user_data.get("account", {})
    domain = account.get("domain", "")
    username = account.get("username", "")
    password = account.get("password", "")
    
    if not domain:
        try:
            await query.edit_message_text("❌ انتهت الجلسة. أرسل رابط جديد.")
        except:
            pass
        return
    
    ssl_ctx = ssl.create_default_context()
    ssl_ctx.check_hostname = False
    ssl_ctx.verify_mode = ssl.CERT_NONE
    connector = aiohttp.TCPConnector(ssl=ssl_ctx)
    
    async with aiohttp.ClientSession(connector=connector) as session:
        
        try:
            # ── الباقات ──
            if data == "btn_live":
                url = f"{domain}/player_api.php?username={username}&password={password}&action=get_live_categories"
                cats = await get_data(session, url)
                if not cats:
                    await query.edit_message_text("❌ لا توجد باقات")
                    return
                context.user_data["cats_live"] = cats
                keyboard, p, t = build_paginated_keyboard(cats, "cats_live")
                await query.edit_message_text(f"📺 الباقات (صفحة {p}/{t}):", reply_markup=keyboard)
            
            elif data.startswith("cats_live_pg_"):
                page = int(data.split("_pg_")[1])
                cats = context.user_data.get("cats_live", [])
                keyboard, p, t = build_paginated_keyboard(cats, "cats_live", page)
                await query.edit_message_text(f"📺 الباقات (صفحة {p}/{t}):", reply_markup=keyboard)
            
            elif data.startswith("cats_live_"):
                idx = int(data.split("_")[-1])
                cats = context.user_data.get("cats_live", [])
                if idx >= len(cats): return
                cat = cats[idx]
                cat_id = cat.get("category_id")
                cat_name = cat.get("category_name", "باقة")
                
                url = f"{domain}/player_api.php?username={username}&password={password}&action=get_live_streams&category_id={cat_id}"
                items = await get_data(session, url)
                if not items:
                    await query.edit_message_text(f"❌ لا توجد قنوات في {cat_name}")
                    return
                
                context.user_data["items_live"] = items
                keyboard, p, t = build_paginated_keyboard(items, "items_live")
                await query.edit_message_text(f"📺 قنوات {cat_name} (صفحة {p}/{t}):", reply_markup=keyboard)
            
            elif data.startswith("items_live_pg_"):
                page = int(data.split("_pg_")[1])
                items = context.user_data.get("items_live", [])
                keyboard, p, t = build_paginated_keyboard(items, "items_live", page)
                await query.edit_message_text(f"📺 القنوات (صفحة {p}/{t}):", reply_markup=keyboard)
            
            elif data.startswith("items_live_"):
                idx = int(data.split("_")[-1])
                items = context.user_data.get("items_live", [])
                if idx >= len(items): return
                item = items[idx]
                item_id = item.get("stream_id", "0")
                item_name = item.get("name", "غير معروف")
                
                # جلب الرابط الحقيقي
                stream_link = await get_real_stream_url(session, domain, username, password, item_id, "live")
                
                text = f"""📺 **تفاصيل القناة**

📌 **الاسم:** {item_name}
🆔 **ID:** `{item_id}`
📂 **النوع:** بث مباشر

🔗 **رابط المشاهدة:**
`{stream_link}`

🔗 **رابط M3U:**
`{domain}/get.php?username={username}&password={password}&type=m3u_plus`

👤 **User Agent:**
`VLC/3.0.20 LibVLC/3.0.20`

🌐 **الدومين:** `{domain}`"""
                keyboard = InlineKeyboardMarkup([
                    [InlineKeyboardButton("🔙 رجوع", callback_data="items_live_pg_1")],
                    [InlineKeyboardButton("🏠 الرئيسية", callback_data="btn_main")]
                ])
                await query.edit_message_text(text, reply_markup=keyboard, parse_mode="Markdown")
            
            # ── الأفلام ──
            elif data == "btn_vod":
                url = f"{domain}/player_api.php?username={username}&password={password}&action=get_vod_categories"
                cats = await get_data(session, url)
                if not cats:
                    await query.edit_message_text("❌ لا توجد أفلام")
                    return
                context.user_data["cats_vod"] = cats
                keyboard, p, t = build_paginated_keyboard(cats, "cats_vod")
                await query.edit_message_text(f"🎬 فئات الأفلام (صفحة {p}/{t}):", reply_markup=keyboard)
            
            elif data.startswith("cats_vod_pg_"):
                page = int(data.split("_pg_")[1])
                cats = context.user_data.get("cats_vod", [])
                keyboard, p, t = build_paginated_keyboard(cats, "cats_vod", page)
                await query.edit_message_text(f"🎬 فئات الأفلام (صفحة {p}/{t}):", reply_markup=keyboard)
            
            elif data.startswith("cats_vod_"):
                idx = int(data.split("_")[-1])
                cats = context.user_data.get("cats_vod", [])
                if idx >= len(cats): return
                cat = cats[idx]
                cat_id = cat.get("category_id")
                cat_name = cat.get("category_name", "فئة")
                
                url = f"{domain}/player_api.php?username={username}&password={password}&action=get_vod_streams&category_id={cat_id}"
                items = await get_data(session, url)
                if not items:
                    await query.edit_message_text(f"❌ لا توجد أفلام في {cat_name}")
                    return
                
                context.user_data["items_vod"] = items
                keyboard, p, t = build_paginated_keyboard(items, "items_vod")
                await query.edit_message_text(f"🎬 أفلام {cat_name} (صفحة {p}/{t}):", reply_markup=keyboard)
            
            elif data.startswith("items_vod_pg_"):
                page = int(data.split("_pg_")[1])
                items = context.user_data.get("items_vod", [])
                keyboard, p, t = build_paginated_keyboard(items, "items_vod", page)
                await query.edit_message_text(f"🎬 الأفلام (صفحة {p}/{t}):", reply_markup=keyboard)
            
            elif data.startswith("items_vod_"):
                idx = int(data.split("_")[-1])
                items = context.user_data.get("items_vod", [])
                if idx >= len(items): return
                item = items[idx]
                item_id = item.get("stream_id", "0")
                item_name = item.get("name", "غير معروف")
                
                stream_link = await get_real_stream_url(session, domain, username, password, item_id, "vod")
                
                text = f"""🎬 **تفاصيل الفيلم**

📌 **الاسم:** {item_name}
🆔 **ID:** `{item_id}`

🔗 **الرابط:**
`{stream_link}`

🌐 `{domain}`"""
                keyboard = InlineKeyboardMarkup([
                    [InlineKeyboardButton("🔙 رجوع", callback_data="items_vod_pg_1")],
                    [InlineKeyboardButton("🏠 الرئيسية", callback_data="btn_main")]
                ])
                await query.edit_message_text(text, reply_markup=keyboard, parse_mode="Markdown")
            
            # ── المسلسلات ──
            elif data == "btn_series":
                url = f"{domain}/player_api.php?username={username}&password={password}&action=get_series_categories"
                cats = await get_data(session, url)
                if not cats:
                    await query.edit_message_text("❌ لا توجد مسلسلات")
                    return
                context.user_data["cats_series"] = cats
                keyboard, p, t = build_paginated_keyboard(cats, "cats_series")
                await query.edit_message_text(f"🎞 فئات المسلسلات (صفحة {p}/{t}):", reply_markup=keyboard)
            
            elif data.startswith("cats_series_pg_"):
                page = int(data.split("_pg_")[1])
                cats = context.user_data.get("cats_series", [])
                keyboard, p, t = build_paginated_keyboard(cats, "cats_series", page)
                await query.edit_message_text(f"🎞 فئات المسلسلات (صفحة {p}/{t}):", reply_markup=keyboard)
            
            elif data.startswith("cats_series_"):
                idx = int(data.split("_")[-1])
                cats = context.user_data.get("cats_series", [])
                if idx >= len(cats): return
                cat = cats[idx]
                cat_id = cat.get("category_id")
                cat_name = cat.get("category_name", "فئة")
                
                url = f"{domain}/player_api.php?username={username}&password={password}&action=get_series&category_id={cat_id}"
                items = await get_data(session, url)
                if not items:
                    await query.edit_message_text(f"❌ لا توجد مسلسلات في {cat_name}")
                    return
                
                context.user_data["items_series"] = items
                keyboard, p, t = build_paginated_keyboard(items, "items_series")
                await query.edit_message_text(f"🎞 مسلسلات {cat_name} (صفحة {p}/{t}):", reply_markup=keyboard)
            
            elif data.startswith("items_series_pg_"):
                page = int(data.split("_pg_")[1])
                items = context.user_data.get("items_series", [])
                keyboard, p, t = build_paginated_keyboard(items, "items_series", page)
                await query.edit_message_text(f"🎞 المسلسلات (صفحة {p}/{t}):", reply_markup=keyboard)
            
            elif data.startswith("items_series_"):
                idx = int(data.split("_")[-1])
                items = context.user_data.get("items_series", [])
                if idx >= len(items): return
                item = items[idx]
                item_id = item.get("stream_id", "0")
                item_name = item.get("name", "غير معروف")
                
                stream_link = await get_real_stream_url(session, domain, username, password, item_id, "series")
                
                text = f"""🎞 **تفاصيل المسلسل**

📌 **الاسم:** {item_name}
🆔 **ID:** `{item_id}`

🔗 **الرابط:**
`{stream_link}`

🌐 `{domain}`"""
                keyboard = InlineKeyboardMarkup([
                    [InlineKeyboardButton("🔙 رجوع", callback_data="items_series_pg_1")],
                    [InlineKeyboardButton("🏠 الرئيسية", callback_data="btn_main")]
                ])
                await query.edit_message_text(text, reply_markup=keyboard, parse_mode="Markdown")
            
            # ── الرجوع للرئيسية ──
            elif data == "btn_main":
                info = await check_account(session, domain, username, password)
                if info:
                    link = f"{domain}/get.php?username={username}&password={password}&type=m3u_plus"
                    text = format_success(1, domain, username, password, info, link)
                else:
                    text = f"✅ الحساب\n🌐 `{domain}`"
                await query.edit_message_text(text, reply_markup=main_keyboard(), parse_mode="Markdown")
        
        except Exception as e:
            if "Message is not modified" not in str(e) and "Query is too old" not in str(e):
                logger.error(f"Error: {e}")

# ====================================
# PROCESS LINKS
# ====================================

async def process_links(links, update, context):
    if not links:
        await update.message.reply_text("❌ ماكو روابط")
        return
    
    progress_msg = await update.message.reply_text(f"⏳ جاري فحص {len(links)} حساب...\n\n0/{len(links)} مكتمل")
    
    ok = 0
    bad = 0
    start_time = time.time()
    
    ssl_ctx = ssl.create_default_context()
    ssl_ctx.check_hostname = False
    ssl_ctx.verify_mode = ssl.CERT_NONE
    
    connector = aiohttp.TCPConnector(ssl=ssl_ctx, limit=20)
    
    async with aiohttp.ClientSession(connector=connector) as session:
        for i, link in enumerate(links, 1):
            data = extract_account_info(link)
            if not data:
                bad += 1
                await update.message.reply_text(f"❌ فشل الحساب {i}\nرابط غير صالح")
                continue
            
            domain, username, password = data
            result = await check_account(session, domain, username, password)
            
            if result:
                ok += 1
                msg = format_success(ok, domain, username, password, result, link)
                context.user_data["account"] = {
                    "domain": domain,
                    "username": username,
                    "password": password
                }
                await update.message.reply_text(msg, reply_markup=main_keyboard(), parse_mode="Markdown")
            else:
                bad += 1
                await update.message.reply_text(f"❌ فشل الحساب {i}\n{domain}")
            
            # تحديث رسالة التقدم
            try:
                elapsed = time.time() - start_time
                avg_time = elapsed / i
                remaining = avg_time * (len(links) - i)
                await progress_msg.edit_text(
                    f"⏳ جاري فحص {len(links)} حساب...\n\n"
                    f"{i}/{len(links)} مكتمل\n"
                    f"✅ {ok} | ❌ {bad}\n"
                    f"⏱ الوقت المتبقي: {int(remaining)} ثانية"
                )
            except:
                pass
    
    # النتيجة النهائية
    elapsed_total = time.time() - start_time
    await progress_msg.edit_text(
        f"{format_summary(ok, bad)}\n\n"
        f"⏱ الوقت الكلي: {int(elapsed_total)} ثانية"
    )

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📡 مرحباً بك في بوت فحص حسابات Xtream\n\n"
        "📌 طرق الإدخال:\n"
        "1️⃣ رابط مباشر\n"
        "2️⃣ ثلاث أسطر (دومين، يوزر، باسورد)\n"
        "3️⃣ ملف TXT\n\n"
        "🔰 المميزات:\n"
        "• فحص الحسابات\n"
        "• عرض الباقات والقنوات\n"
        "• عرض الأفلام والمسلسلات\n"
        "• استخراج روابط المشاهدة"
    )

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    links = extract_urls(text)
    
    if links:
        await process_links(links, update, context)
        return
    
    lines = [l.strip() for l in text.split("\n") if l.strip()]
    if len(lines) == 3:
        domain, user, pwd = lines
        link, _ = build_get_link(domain, user, pwd)
        await process_links([link], update, context)
        return
    
    await update.message.reply_text("❌ بيانات غير صحيحة\n\nتأكد من إدخال:\n1️⃣ رابط get.php\n2️⃣ أو ثلاث أسطر (دومين، يوزر، باسورد)")

async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        file = await context.bot.get_file(update.message.document.file_id)
        content = await file.download_as_bytearray()
        text = content.decode("utf-8", errors="ignore")
        links = extract_urls(text)
        
        if not links:
            await update.message.reply_text("❌ لم يتم العثور على روابط صالحة في الملف")
            return
        
        await update.message.reply_text(f"📁 تم العثور على {len(links)} رابط في الملف")
        await process_links(links, update, context)
    except Exception as e:
        logger.error(f"Error reading file: {e}")
        await update.message.reply_text("❌ حدث خطأ في قراءة الملف")

def main():
    TOKEN = "8911731662:AAEEj-zds64GmPdRCACtOpJ5Hna1jPVExcw"
    
    app = (
        Application.builder()
        .token(TOKEN)
        .read_timeout(30)
        .write_timeout(30)
        .connect_timeout(15)
        .pool_timeout(15)
        .build()
    )
    
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    
    print("✅ Bot Running...")
    app.run_polling()

if __name__ == "__main__":
    main()