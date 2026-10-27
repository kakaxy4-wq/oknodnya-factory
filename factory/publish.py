"""Публикация ролика дня в Telegram-канал через Bot API (без сторонних библиотек).

Нужно: бот — администратор канала с правом публикации. Токен — только из переменной окружения.
  export OKNO_BOT_TOKEN=...            # токен @oknodnya_bot
  export OKNO_CHANNEL=@oknodnya
  python -m factory.publish out/2026-10-23/1-ugadai-murmansk          # опубликовать
  python -m factory.publish out/2026-10-23/1-ugadai-murmansk --dry    # только проверить
Журнал: log/posts.csv — повторно один и тот же ролик не уходит.
"""
import csv
import json
import os
import sys
import time
import urllib.request
import uuid
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOG = ROOT / "log" / "posts.csv"


def _multipart(fields, files):
    boundary = uuid.uuid4().hex
    body = bytearray()
    for k, v in fields.items():
        body += f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode()
    for k, (name, data, ctype) in files.items():
        body += f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"; filename="{name}"\r\nContent-Type: {ctype}\r\n\r\n'.encode()
        body += data + b"\r\n"
    body += f"--{boundary}--\r\n".encode()
    return bytes(body), f"multipart/form-data; boundary={boundary}"


def api(method, fields=None, files=None):
    token = os.environ["OKNO_BOT_TOKEN"]
    body, ctype = _multipart(fields or {}, files or {})
    req = urllib.request.Request(f"https://api.telegram.org/bot{token}/{method}", data=body, headers={"Content-Type": ctype})
    with urllib.request.urlopen(req, timeout=120) as r:
        res = json.loads(r.read())
    if not res.get("ok"):
        raise RuntimeError(res)
    return res["result"]


def published(video_id):
    if not LOG.exists():
        return False
    with LOG.open(encoding="utf-8") as f:
        return any(row["id"] == video_id for row in csv.DictReader(f))


def publish(folder: Path, dry=False):
    mp4 = next(folder.glob("*.mp4"))
    vid = mp4.stem
    qa = json.loads((folder / "qa.json").read_text(encoding="utf-8"))
    size = int(qa["probe"]["format"]["size"])
    assert size < 49e6, "Bot API принимает до 50 МБ"
    assert (folder / "QA-OK").exists(), "ролик не прошёл проверку качества (нет файла QA-OK)"
    if published(vid):
        print("уже опубликован:", vid)
        return
    caption = (folder / "post.txt").read_text(encoding="utf-8")[:1024]
    if dry:
        print("проверка ок:", vid, f"{size / 1e6:.1f} МБ")
        return
    channel = os.environ.get("OKNO_CHANNEL", "@oknodnya")
    # своя ссылка-приглашение на каждый ролик — видно, какой ролик приводит подписчиков
    link = api("createChatInviteLink", {"chat_id": channel, "name": vid[:32]})["invite_link"]
    for attempt in range(3):
        try:
            msg = api("sendVideo", {"chat_id": channel, "caption": caption, "supports_streaming": "true",
                                    "width": 1080, "height": 1920},
                      {"video": (mp4.name, mp4.read_bytes(), "video/mp4"),
                       "thumbnail": ("cover.jpg", (folder / "cover.jpg").read_bytes(), "image/jpeg")})
            break
        except Exception as e:  # noqa: BLE001 — повтор при сетевой ошибке
            if attempt == 2:
                raise
            print("повтор через", 2 ** (attempt + 1) * 60, "с:", e)
            time.sleep(2 ** (attempt + 1) * 60)
    LOG.parent.mkdir(exist_ok=True)
    new = not LOG.exists()
    with LOG.open("a", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["id", "posted_at", "message_id", "invite_link"])
        w.writerow([vid, datetime.now().isoformat(timespec="minutes"), msg["message_id"], link])
    print("опубликовано:", vid, "· ссылка для внешних площадок:", link)
    if (folder / "quiz.json").exists():
        send_quiz(folder, channel, msg["message_id"])


def send_quiz(folder: Path, channel, video_msg_id):
    """Опрос-викторина ответом на ролик-загадку; id опроса — в POLL.json, по нему вечером разгадка (pipeline due)."""
    from factory import quiz
    q = json.loads((folder / "quiz.json").read_text(encoding="utf-8"))
    try:
        poll = api("sendPoll", {"chat_id": channel, "question": "Какой город за шторой?",
                                "options": json.dumps([{"text": o} for o in q["options"]], ensure_ascii=False),
                                "type": "quiz", "correct_option_id": q["correct"], "is_anonymous": "true",
                                "explanation": quiz.explanation(q),
                                "reply_parameters": json.dumps({"message_id": video_msg_id})})
    except Exception as e:  # noqa: BLE001 — ролик уже вышел; без опроса просто не будет разгадки
        print("опрос не отправлен:", e)
        return
    (folder / "POLL.json").write_text(json.dumps({"message_id": poll["message_id"], "video_message_id": video_msg_id,
                                                  "posted_at": datetime.now().isoformat(timespec="minutes")}), encoding="utf-8")


if __name__ == "__main__":
    publish(Path(sys.argv[1]), dry="--dry" in sys.argv)
