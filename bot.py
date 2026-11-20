import os, re, asyncio, subprocess
from datetime import datetime
from urllib.parse import urlparse
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters

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


def ffmpeg_ok():
    try:
        return subprocess.run(["ffmpeg", "-version"],
                              stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL,
                              timeout=5).returncode == 0
    except Exception:
        return False


def detect_type(url: str) -> str:
    """يحدد نوع المصدر من الرابط."""
    path = urlparse(url).path.lower()
    if path.endswith(HLS_EXT):   return "hls"
    if path.endswith(DASH_EXT):  return "dash"
    if path.endswith(MP4_EXT):   return "mp4"
    if path.endswith(TS_EXT):    return "ts"
    if path.endswith(MKV_EXT):   return "file"
    if path.endswith(AUDIO_EXT): return "audio"
    if path.endswith(PAGE_EXT):  return "page"
    # افتراضي: خلّي ffmpeg يقرر
    return "auto"


def build_input_args(url: str, kind: str):
    """خيارات الإدخال المناسبة لكل صيغة."""
    args = ["-hide_banner", "-loglevel", "warning"]

    # headers موحّدة (تفيد كثير من سيرفرات HLS/DASH المحمية)
    args += [
        "-user_agent", DEFAULT_UA,
        "-headers", "Referer: https://www.facebook.com/\r\n",
    ]

    if kind == "hls":
        args += [
            "-reconnect", "1",
            "-reconnect_streamed", "1",
            "-reconnect_delay_max", "10",
            "-allowed_extensions", "ALL",
            "-protocol_whitelist", "file,http,https,tcp,tls,crypto",
            "-i", url,
        ]
    elif kind == "dash":
        args += [
            "-reconnect", "1",
            "-reconnect_streamed", "1",
            "-reconnect_delay_max", "10",
            "-protocol_whitelist", "file,http,https,tcp,tls,crypto",
            "-i", url,
        ]
    elif kind == "ts":
        args += [
            "-reconnect", "1",
            "-reconnect_streamed", "1",
            "-reconnect_delay_max", "10",
            "-f", "mpegts",
            "-i", url,
        ]
    elif kind in ("mp4", "file", "audio"):
        args += [
            "-reconnect", "1",
            "-reconnect_streamed", "1",
            "-reconnect_delay_max", "10",
            "-i", url,
        ]
    elif kind == "page":
        # صفحات HTML: نحاول استخراج بث مباشر منها
        args += [
            "-reconnect", "1",
            "-reconnect_streamed", "1",
            "-reconnect_delay_max", "10",
            "-i", url,
        ]
    else:  # auto
        args += [
            "-reconnect", "1",
            "-reconnect_streamed", "1",
            "-reconnect_delay_max", "10",
            "-i", url,
        ]
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
        "-r", "30",
        "-g", "60",
        "-keyint_min", "60",
        "-sc_threshold", "0",
        # صوت
        "-c:a", "aac",
        "-b:a", "128k",
        "-ar", "44100",
        "-ac", "2",
        # إعادة الترميز لتوحيد الصيغة مع FLV/RTMP
        "-f", "flv",
        output,
    ]
    return subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)


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
    await update.message.reply_text(f"⏳ جاري تشغيل البث {slot}...\nالنوع المكتشف: {kind}")

    try:
        p = start_ffmpeg(source, key)
        await asyncio.sleep(4)
        if p.poll() is not None:
            err = ""
            if p.stderr:
                try:
                    err = p.stderr.read().decode("utf-8", "ignore")[-1500:]
                except Exception:
                    pass
            sessions.pop(uid, None)
            await update.message.reply_text(f"❌ فشل البث {slot}.\n\n{err or 'FFmpeg توقف مباشرة.'}")
            return
        streams[slot] = {
            "process": p,
            "started": datetime.now(),
            "source": source,
            "kind": kind,
            "user_id": uid,
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


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "/start 1 إلى /start 9 — بدء بث\n"
        "/stop 1 إلى /stop 9 — إيقاف بث\n"
        "/live — الحالة\n"
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
    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("stop", stop_cmd))
    app.add_handler(CommandHandler("live", live_cmd))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))
    app.run_polling()


if __name__ == "__main__":
    main()
