import os
import time
import logging
from flask import Flask, request, jsonify
import requests

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("videoconvibot")

app = Flask(__name__)

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CLOUDCONVERT_API_KEY = os.environ["CLOUDCONVERT_API_KEY"]
MAX_PER_SET = int(os.environ.get("MAX_STICKERS_PER_SET", "30"))
MAX_SETS = int(os.environ.get("MAX_SETS", "50"))

TG_API = f"https://api.telegram.org/bot{BOT_TOKEN}"
CC_API = "https://api.cloudconvert.com/v2"
CC_HEADERS = {"Authorization": f"Bearer {CLOUDCONVERT_API_KEY}"}


def tg(method, **params):
    r = requests.post(f"{TG_API}/{method}", json=params, timeout=30)
    return r.json()


def tg_get_file_path(file_id):
    r = requests.get(f"{TG_API}/getFile", params={"file_id": file_id}, timeout=30)
    data = r.json()
    if not data.get("ok"):
        raise RuntimeError(f"getFile failed: {data}")
    return data["result"]["file_path"]


def tg_download_file(file_path):
    url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_path}"
    r = requests.get(url, timeout=60)
    r.raise_for_status()
    return r.content


def set_name_for(chat_id, level):
    suffix = "" if level == 1 else f"_{level}"
    return f"sticker{chat_id}{suffix}_by_videoconvibot"


def get_sticker_set(name):
    r = requests.get(f"{TG_API}/getStickerSet", params={"name": name}, timeout=30)
    return r.json()


def find_target_level(chat_id):
    for level in range(1, MAX_SETS + 1):
        name = set_name_for(chat_id, level)
        data = get_sticker_set(name)
        if data.get("ok"):
            count = len(data["result"].get("stickers", []))
            if count < MAX_PER_SET:
                return level, True, count
            continue
        else:
            return level, False, 0
    return None, None, None


def convert_to_webm_sticker(video_bytes):
    job_payload = {
        "tasks": {
            "import-1": {"operation": "import/upload"},
            "convert-1": {
                "operation": "convert",
                "input": "import-1",
                "output_format": "webm",
                "video_codec": "vp9",
                "audio_codec": "none",
                "width": 512,
                "height": 512,
                "fit": "scale",
                "video_frame_rate": 30,
                "crf": 40,
            },
            "export-1": {"operation": "export/url", "input": "convert-1"},
        }
    }
    r = requests.post(f"{CC_API}/jobs", headers=CC_HEADERS, json=job_payload, timeout=30)
    r.raise_for_status()
    job = r.json()["data"]
    job_id = job["id"]

    upload_task = next(t for t in job["tasks"] if t["name"] == "import-1")
    upload_form = upload_task["result"]["form"]
    files = {"file": ("input.mp4", video_bytes)}
    requests.post(upload_form["url"], data=upload_form["parameters"], files=files, timeout=120)

    for _ in range(30):
        r = requests.get(f"{CC_API}/jobs/{job_id}", headers=CC_HEADERS, timeout=30)
        job = r.json()["data"]
        if job["status"] in ("finished", "error"):
            break
        time.sleep(2)

    if job["status"] != "finished":
        raise RuntimeError(f"CloudConvert job did not finish: {job['status']}")

    export_task = next(t for t in job["tasks"] if t["name"] == "export-1")
    file_url = export_task["result"]["files"][0]["url"]
    r = requests.get(file_url, timeout=60)
    r.raise_for_status()
    return r.content


def handle_start(chat_id):
    tg("sendMessage", chat_id=chat_id,
       text="مرحباً 👋\nابعث الفيديو الأول وبسوي لك مجموعة الستيكرات 🎥\n"
            "أي فيديو ثاني تبعثه بينضاف تلقائياً لنفس المجموعة "
            "(حد أقصى 30 لكل مجموعة، وبعدها نبدأ مجموعة جديدة تلقائياً)")


def handle_video(chat_id, file_id):
    level, exists, current_count = find_target_level(chat_id)
    if level is None:
        tg("sendMessage", chat_id=chat_id,
           text=f"وصلت للحد الأقصى المدعوم حالياً ({MAX_SETS} مجموعة) ✅\nتواصل معي لو تبي نزيد العدد أكثر")
        return

    name = set_name_for(chat_id, level)

    try:
        file_path = tg_get_file_path(file_id)
        video_bytes = tg_download_file(file_path)
        webm_bytes = convert_to_webm_sticker(video_bytes)
    except Exception as e:
        log.exception("conversion failed")
        tg("sendMessage", chat_id=chat_id, text=f"صار خطأ برفع/تحويل الفيديو 😕\nالسبب: {e}")
        return

    files = {"sticker": ("sticker.webm", webm_bytes, "video/webm")}
    r = requests.post(f"{TG_API}/uploadStickerFile", data={
        "user_id": chat_id, "sticker_format": "video"
    }, files=files, timeout=60)
    upload_result = r.json()
    if not upload_result.get("ok"):
        tg("sendMessage", chat_id=chat_id,
           text=f"صار خطأ برفع ملف الستيكر 😕\nالسبب: {upload_result.get('description')}")
        return
    sticker_file_id = upload_result["result"]["file_id"]

    sticker_obj = {"sticker": sticker_file_id, "format": "video", "emoji_list": ["\U0001F600"]}

    if exists:
        r = requests.post(f"{TG_API}/addStickerToSet", json={
            "user_id": chat_id, "name": name, "sticker": sticker_obj
        }, timeout=30)
        result = r.json()
        if not result.get("ok"):
            tg("sendMessage", chat_id=chat_id,
               text=f"صار خطأ بإضافة الستيكر 😕: {result.get('description')}")
            return
        new_count = current_count + 1
        status = "🎉 تم الوصول للحد الأقصى (30 ستيكر) ✅" if new_count >= MAX_PER_SET else f"تم ✅ ({new_count}/{MAX_PER_SET})"
        tg("sendMessage", chat_id=chat_id,
           text=f"{status} — المجموعة {level}\n\nرابط المجموعة:\nhttps://t.me/addstickers/{name}")
    else:
        r = requests.post(f"{TG_API}/createNewStickerSet", json={
            "user_id": chat_id, "name": name, "title": f"Stickers {chat_id}" + (f" {level}" if level > 1 else ""),
            "stickers": [sticker_obj]
        }, timeout=30)
        result = r.json()
        if not result.get("ok"):
            tg("sendMessage", chat_id=chat_id,
               text=f"صار خطأ بإنشاء المجموعة 😕: {result.get('description')}")
            return
        extra = "\n\nالمجموعة السابقة امتلأت، فبدأت لك واحدة جديدة تلقائياً 🎉" if level > 1 else ""
        tg("sendMessage", chat_id=chat_id,
           text=f"تم إنشاء المجموعة ✅ (1/{MAX_PER_SET}) — المجموعة {level}{extra}\n\n"
                f"رابط المجموعة:\nhttps://t.me/addstickers/{name}\n\n"
                f"اضغط الرابط لتضيفها لملصقاتك. ابعث فيديو ثاني لو تبي تضيف أكثر")


@app.route("/webhook", methods=["POST"])
def webhook():
    update = request.get_json(force=True, silent=True) or {}
    log.info("update: %s", update)

    message = update.get("message")
    if not message:
        return jsonify(ok=True)

    chat_id = message["chat"]["id"]
    text = message.get("text", "")
    video = message.get("video")
    animation = message.get("animation")

    try:
        if text == "/start":
            handle_start(chat_id)
        elif video:
            handle_video(chat_id, video["file_id"])
        elif animation:
            handle_video(chat_id, animation["file_id"])
    except Exception:
        log.exception("unhandled error processing update")

    return jsonify(ok=True)


@app.route("/", methods=["GET"])
def health():
    return "videoconvibot is running", 200


@app.route("/set-webhook", methods=["GET"])
def set_webhook():
    domain = os.environ.get("RAILWAY_PUBLIC_DOMAIN")
    if not domain:
        return "RAILWAY_PUBLIC_DOMAIN not set", 500
    webhook_url = f"https://{domain}/webhook"
    r = requests.post(f"{TG_API}/setWebhook", json={"url": webhook_url}, timeout=30)
    return jsonify(r.json())


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
