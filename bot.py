import os, re, asyncio, subprocess
from datetime import datetime
from urllib.parse import urlparse
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters

try:
    import aiohttp
    AIOHTTP_OK = True
except ImportError:
    AIOHTTP_OK = False

BOT_TOKEN = os.getenv("BOT_TOKEN")
MAX_STREAMS = 9
streams = {}
sessions = {}

# صيغ معروفة
HLS_EXT   = (".m3u8",)
DASH_EXT  = (".mpd",)
MP4_EXT   = (".mp4", ".m4v", ".mov")
TS_EXT    = (".ts",)
MKV_EXT   = (".mkv", ".webm", ".flv")
AUDIO_EXT = (".mp3", ".aac", ".m4a", ".ogg")
PAGE_EXT  = (".html", ".htm", ".php", ".asp", ".aspx")

DEFAULT_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
              "AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/124.0 Safari/537.36")

LOG_DIR = "/tmp"
os.makedirs(LOG_DIR, exist_ok=True)


def ffmpeg_ok():
    try:
        return subprocess.run(["ffmpeg", "-version"],
                              stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL,
                              timeout=5).returncode == 0
    except Exception:
        return False


def detect_type(url: str) -> str:
    path = urlparse(url).path.lower()
    if path.endswith(HLS_EXT):   return "hls"
    if path.endswith(DASH_EXT):  return "dash"
    if path.endswith(MP4_EXT):   return "mp4"
    if path.endswith(TS_EXT):    return "ts"
    if path.endswith(MKV_EXT):   return "file"
    if path.endswith(AUDIO_EXT): return "audio"
    if path.endswith(PAGE_EXT):  return "page"
    return "auto"


def make_headers(url: str) -> str:
    """Referer/Origin مشتقان من الرابط نفسه."""
    p = urlparse(url)
    base = f"{p.scheme}://{p.netloc}/"
    return f"Referer: {base}\r\nOrigin: {p.scheme}://{p.netloc}\r\n"


async def probe_stream(url: str) -> tuple[bool, str]:
    """يفحص أن الرابط يعيد فيديو/manifest وليس HTML/JS."""
    if not AIOHTTP_OK:
        return True, "aiohttp غير مثبت — تم تخطي الفحص"
    p = urlparse(url)
    headers = {
        "User-Agent": DEFAULT_UA,
        "Referer": f"{p.scheme}://{p.netloc}/",
        "Accept": "*/*",
    }
    try:
        timeout = aiohttp.ClientTimeout(total=12)
        async with aiohttp.ClientSession(timeout=timeout) as s:
            async with s.get(url, headers=headers, allow_redirects=True) as r:
                ct = (r.headers.get("Content-Type") or "").lower()
                final = str(r.url)
                final_path = urlparse(final).path.lower()

                ok_ct = ("mpegurl", "dash+xml", "video/", "audio/",
                         "octet-stream", "mp2t", "application/x-mpegurl")
                if any(k in ct for k in ok_ct):
                    return True, ct
                if final_path.endswith((".m3u8", ".mpd")):
                    return True, ct or "manifest"
                if "javascript" in ct or final_path.endswith(".js"):
                    return False, f"المصدر يعيد JavaScript بدل فيديو ({ct or 'js'})"
                if "html" in ct:
                    return False, f"المصدر يعيد صفحة HTML بدل فيديو ({ct})"
                return False, f"Content-Type غير مدعوم: {ct or 'unknown'}"
    except asyncio.TimeoutError:
        return False, "انتهت مهلة الاتصال بالمصدر"
    except Exception as e:
        return False, f"فشل الاتصال: {e}"


def build_input_args(url: str, kind: str):
    args = ["-hide_banner", "-loglevel", "warning"]
    hdrs = make_headers(url)
    args += ["-user_agent", DEFAULT_UA, "-headers", hdrs]

    common_net = [
        "-reconnect", "1",
        "-reconnect_streamed", "1",
        "-reconnect_delay_max", "10",
        "-rw_timeout", "15000000",
    ]

    if kind == "hls":
        args += common_net + [
            "-http_persistent", "1",
            "-seg_max_retry", "5",
            "-max_reload", "3",
            "-protocol_whitelist", "file,http,https,tcp,tls,crypto",
            "-i", url,
        ]
    elif kind == "dash":
        args += common_net + [
            "-protocol_whitelist", "file,http,https,tcp,tls,crypto",
            "-i", url,
        ]
    elif kind == "ts":
        args += common_net + ["-f", "mpegts", "-i", url]
    elif kind in ("mp4", "file", "audio", "page", "auto"):
        args += common_net + ["-i", url]
    else:
        args += common_net + ["-i", url]

    return args


def start_ffmpeg(source, key):
    kind = detect_type(source)
    output = f"rtmps://live-api-s.facebook.com:443/rtmp/{key}"

    cmd = ["ffmpeg"] + build_input_args(source, kind) + [
        # فيديو
        "-c:v", "libx264",
        "-preset", "veryfast",
        "-tune", "zerolatency",
        "-pix_fmt", "yuv420p",
        "-profile:v", "main",
        "-level", "4.0",
        "-r", "30",
        "-g", "60",
        "-keyint_min", "60",
        "-sc_threshold", "0",
        "-vsync", "vfr",
        # صوت
        "-c:a", "aac",
        "-b:a", "128k",
        "-ar", "44100",
        "-ac", "2",
        # إخراج
        "-f", "flv",
        output,
    ]

    log_path = os.path.join(LOG_DIR, f"ffmpeg_{key[:12]}.log")
    try:
        log_file = open(log_path, "ab")
    except Exception:
        log_file = subprocess.DEVNULL

    p = subprocess.Popen(cmd, stdout=log_file, stderr=log_file)
    return p, log_path


def tail_log(path: str, n: int = 1500) -> str:
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - n))
            return f.read().decode("utf-8", "ignore")
    except Exception:
        return ""


def duration(started):
    s = int((datetime.now() - started).total_seconds())
    return f"{s//3600:02d}:{(s%3600)//60:02d}:{s%60:02d}"


async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("استخدم: /start 1 إلى /start 9")
        return
    try:
        slot = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ الرقم يجب أن يكون من 1 إلى 9.")
        return
    if not 1 <= slot <= 9:
        await update.message.reply_text("❌ الرقم يجب أن يكون من 1 إلى 9.")
        return
    if slot in streams:
        await update.message.reply_text(f"⚠️ البث {slot} يعمل بالفعل. استخدم /stop {slot}")
        return
    sessions[update.effective_user.id] = {"slot": slot, "step": "key"}
    await update.message.reply_text(f"📡 بدء البث رقم {slot}\n\nأرسل Facebook Stream Key 🔑")


async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if uid not in sessions:
        return
    text = update.message.text.strip()
    s = sessions[uid]

    if s["step"] == "key":
        if not text:
            await update.message.reply_text("❌ Stream Key غير صالح.")
            return
        s["key"] = text
        s["step"] = "source"
        await update.message.reply_text(
            f"✅ تم استلام المفتاح للبث {s['slot']}.\n\n"
            f"أرسل رابط المصدر 🔗\n"
            f"مدعوم: m3u8 / mpd / mp4 / m3u / ts / html / m2ts ..."
        )
        return

    slot, key, source = s["slot"], s["key"], text
    kind = detect_type(source)

    # فحص المصدر قبل تشغيل ffmpeg
    await update.message.reply_text(f"🔍 جاري فحص المصدر ({kind})...")
    ok, info = await probe_stream(source)
    if not ok:
        sessions.pop(uid, None)
        await update.message.reply_text(
            f"⚠️ المصدر يعيد محتوى غير صالح للبث.\n\n"
            f"السبب: {info}\n\n"
            f"جرّب رابطًا آخر أو تأكد أن المصدر يبث فيديو وليس صفحة حماية."
        )
        return

    await update.message.reply_text(f"⏳ جاري تشغيل البث {slot}...\nالنوع: {kind}\nالفحص: {info}")

    try:
        p, log_path = start_ffmpeg(source, key)
        await asyncio.sleep(5)

        if p.poll() is not None:
            err = await asyncio.to_thread(tail_log, log_path)
            sessions.pop(uid, None)
            await update.message.reply_text(
                f"❌ فشل البث {slot}.\n\n{err or 'FFmpeg توقف مباشرة بدون سجل.'}"
            )
            return

        streams[slot] = {
            "process": p,
            "started": datetime.now(),
            "source": source,
            "kind": kind,
            "user_id": uid,
            "log": log_path,
        }
        sessions.pop(uid, None)
        await update.message.reply_text(
            f"🟢 البث {slot} يعمل الآن ({kind}).\n\n"
            f"إيقاف: /stop {slot}\nالحالة: /live"
        )
    except Exception as e:
        sessions.pop(uid, None)
        await update.message.reply_text(f"❌ خطأ: {e}")


async def stop_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("استخدم /stop 1 إلى /stop 9")
        return
    try:
        slot = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ الرقم يجب أن يكون من 1 إلى 9.")
        return
    if not 1 <= slot <= 9:
        await update.message.reply_text("❌ الرقم يجب أن يكون من 1 إلى 9.")
        return
    item = streams.get(slot)
    if not item:
        await update.message.reply_text(f"🔴 البث {slot} غير مشغل.")
        return
    p = item["process"]
    try:
        p.terminate()
        p.wait(timeout=10)
    except Exception:
        try:
            p.kill()
        except Exception:
            pass
    streams.pop(slot, None)
    await update.message.reply_text(f"⛔ تم إيقاف البث {slot}.")


async def live_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    out = "📡 حالة البثوث 1-9\n\n"
    for slot in range(1, 10):
        item = streams.get(slot)
        if not item:
            out += f"{slot} 🔴 متوقف\n"
            continue
        if item["process"].poll() is not None:
            streams.pop(slot, None)
            out += f"{slot} 🔴 انتهى\n"
            continue
        kind = item.get("kind", "?")
        out += f"{slot} 🟢 Live ({kind}) — {duration(item['started'])}\n"
    await update.message.reply_text(out)


async def logs_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("استخدم: /logs 1 إلى /logs 9")
        return
    try:
        slot = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ الرقم يجب أن يكون من 1 إلى 9.")
        return
    item = streams.get(slot)
    if not item or "log" not in item:
        await update.message.reply_text(f"🔴 لا يوجد سجل للبث {slot}.")
        return
    err = await asyncio.to_thread(tail_log, item["log"], 3000)
    await update.message.reply_text(f"📄 آخر سجل للبث {slot}:\n\n{err or 'السجل فارغ.'}")


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "/start 1 إلى /start 9 — بدء بث\n"
        "/stop 1 إلى /stop 9 — إيقاف بث\n"
        "/live — الحالة\n"
        "/logs 1 إلى /logs 9 — سجل ffmpeg\n"
        "/help — المساعدة\n\n"
        "الصيغ المدعومة:\n"
        "m3u8 (HLS), mpd (DASH), mp4, m3u, ts, m2ts, mkv, webm, flv, mp3/aac, html"
    )


async def monitor():
    while True:
        for slot, item in list(streams.items()):
            if item["process"].poll() is not None:
                streams.pop(slot, None)
        await asyncio.sleep(10)


async def post_init(app):
    asyncio.create_task(monitor())


def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN environment variable is missing.")
    if not ffmpeg_ok():
        raise RuntimeError("FFmpeg is not installed.")
    if not AIOHTTP_OK:
        print("⚠️ aiohttp غير مثبت — سيتم تخطي فحص المصادر. ثبّته: pip install aiohttp")
    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("stop", stop_cmd))
    app.add_handler(CommandHandler("live", live_cmd))
    app.add_handler(CommandHandler("logs", logs_cmd))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))
    app.run_polling()


if __name__ == "__main__":
    main()
