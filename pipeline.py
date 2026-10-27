"""Ежедневный конвейер на сервере.

  python3 pipeline.py build [ДАТА]     # 06:00 — git pull, запись сцен, сборка, техпроверка → out/ДАТА/*/QA-OK,
                                       #   каждый ролик — владельцу в личку бота с кнопками «✅ В канал / ⏭ Пропустить»
  python3 pipeline.py due              # каждые 5 минут: одобренный ролик выходит в свой слот (09:00 / 14:00 / 20:00)
                                       #   или сразу после одобрения, если слот уже прошёл (до 23:30)
  python3 pipeline.py publish N [ДАТА] # вручную: опубликовать ролик N (1, 2, 3) без одобрения

Кнопки обрабатывает бот сайта (тот же токен, он и так слушает Telegram): решение — файл approvals/ДАТА-N.ok|.skip.

Сценарии пишет Claude (ежедневная задача) в content/ДАТА.json. "approved": false (черновик: задача не смогла собрать
и посмотреть кадры) сборку не останавливает — настоящие ворота это ✅ владельца по каждому ролику; в сообщении пометка.
"""
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SITE = Path(os.environ.get("OKNO_SITE_REPO", "/opt/lofi-goroda-public"))
CLIPS = ROOT / "clips"
APPROVALS = Path(os.environ.get("OKNO_APPROVALS", ROOT / "approvals"))
SLOTS = {"1": (9, 0), "2": (14, 0), "3": (20, 0)}


def sh(*args, **kw):
    print("$", " ".join(map(str, args)), flush=True)
    return subprocess.run(args, check=True, **kw)


def notify(text):
    """Сообщение владельцу в личку бота (если задан OKNO_OWNER_CHAT)."""
    chat = os.environ.get("OKNO_OWNER_CHAT")
    if not chat or not os.environ.get("OKNO_BOT_TOKEN"):
        print("notify:", text)
        return
    from factory.publish import api
    try:
        api("sendMessage", {"chat_id": chat, "text": text[:4000]})
    except Exception as e:  # noqa: BLE001
        print("notify failed:", e)


def _port_free(port):
    with socket.socket() as s:
        return s.connect_ex(("127.0.0.1", port)) != 0


def build(day):
    for repo in (ROOT, SITE):                 # свежие сценарии и свежая версия сайта; без сети — работаем с тем, что есть
        try:
            sh("git", "-C", str(repo), "pull", "--ff-only", "-q")
        except subprocess.CalledProcessError as e:
            print("git pull не удался:", repo, e)
    plan_path = ROOT / "content" / f"{day}.json"
    if not plan_path.exists():
        notify(f"⚠️ Окно дня: на {day} нет сценариев — сегодня ролики не выйдут.")
        return
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    draft = not plan.get("approved")  # 25–26.10 из-за этого флага ролики не собирались вовсе, хотя решение всё равно за владельцем
    CLIPS.mkdir(exist_ok=True)
    port = 8765
    srv = None
    if _port_free(port):
        srv = subprocess.Popen([sys.executable, "tools/devserver.py", str(port)], cwd=SITE,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(2)
    try:
        shots = [{"name": k, **v} for k, v in plan["scenes"].items()]
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump(shots, f, ensure_ascii=False)
        sh("node", str(ROOT / "recorder" / "record.js"), str(CLIPS), f.name, cwd=ROOT / "recorder")
    finally:
        if srv:
            srv.terminate()
    sh(sys.executable, str(ROOT / "build.py"), str(plan_path), "--clips", str(CLIPS))
    ok = []
    for v in plan["videos"]:
        folder = ROOT / "out" / day / v["id"]
        qa = json.loads((folder / "qa.json").read_text(encoding="utf-8"))
        streams = {s["codec_type"] if "codec_type" in s else s["codec_name"]: s for s in qa["probe"]["streams"]}
        size = int(qa["probe"]["format"]["size"])
        dur = float(qa["probe"]["format"]["duration"])
        if size < 49e6 and 10 < dur < 60 and any(s.get("width") == 1080 for s in qa["probe"]["streams"]):
            (folder / "QA-OK").write_text("ok\n")
            ok.append(v["id"])
    for f in APPROVALS.glob(f"{day}-*"):  # пересобрали — прежние ✅/⏭ относились к другой версии роликов
        f.unlink(missing_ok=True)
    note = " Сценарии — черновик задачи (кадры она не смотрела): проверьте ролики внимательнее." if draft else ""
    notify(f"🎬 Окно дня {day}: собрано {len(ok)}/{len(plan['videos'])}. Ниже ролики — без ✅ в канал не выйдут.{note}")
    for vid in ok:
        send_for_review(day, vid)


def send_for_review(day, vid):
    """Ролик владельцу в личку с кнопками; решение запишет бот сайта в approvals/."""
    chat = os.environ.get("OKNO_OWNER_CHAT")
    if not chat:
        return
    from factory.publish import api
    folder, n = ROOT / "out" / day / vid, vid.split("-")[0]
    hh, mm = SLOTS.get(n, (0, 0))
    post = (folder / "post.txt").read_text(encoding="utf-8")
    if (folder / "quiz.json").exists():  # загадка: владелец видит и варианты опроса
        q = json.loads((folder / "quiz.json").read_text(encoding="utf-8"))
        post += f"\n\n🗳 Под роликом опрос: {' · '.join(q['options'])} (верно: {q['answer']}). Разгадка — в 21:00."
    caption = f"№{n} на {day[8:]}.{day[5:7]} · выйдет в {hh:02d}:{mm:02d} после ✅\n\n{post}"[:1024]
    kb = {"inline_keyboard": [[{"text": "✅ В канал", "callback_data": f"ov:ok:{day}:{n}"},
                               {"text": "⏭ Пропустить", "callback_data": f"ov:skip:{day}:{n}"}]]}
    try:
        mp4 = next(folder.glob("*.mp4"))
        api("sendVideo", {"chat_id": chat, "caption": caption, "supports_streaming": "true", "width": 1080, "height": 1920,
                          "reply_markup": json.dumps(kb, ensure_ascii=False)},
            {"video": (mp4.name, mp4.read_bytes(), "video/mp4")})
    except Exception as e:  # noqa: BLE001
        notify(f"Не смог прислать ролик {vid} на одобрение: {e}")


def decision(day, n):
    if (APPROVALS / f"{day}-{n}.skip").exists():
        return "skip"
    return "ok" if (APPROVALS / f"{day}-{n}.ok").exists() else None


def due(now=None):
    """Раз в 5 минут: одобренные ролики, чей слот наступил, — в канал; неодобренным — одно напоминание."""
    from factory.publish import published
    now = now or datetime.now()
    day = now.date().isoformat()
    for n, (hh, mm) in SLOTS.items():
        if (now.hour, now.minute) < (hh, mm) or (now.hour, now.minute) > (23, 30):
            continue
        folders = sorted((ROOT / "out" / day).glob(f"{n}-*"))
        if not folders or not (folders[0] / "QA-OK").exists():
            continue
        state = decision(day, n)
        if state == "ok" and not published(next(folders[0].glob("*.mp4")).stem):
            publish(n, day)
        elif state is None and not (folders[0] / "REMINDED").exists():
            (folders[0] / "REMINDED").write_text("1\n")
            notify(f"⏰ Окно дня {day}: ролик №{n} ждёт ✅ — выйдет сразу после одобрения (до 23:30).")


def publish(n, day):
    from factory.publish import publish as pub
    folders = sorted((ROOT / "out" / day).glob(f"{n}-*"))
    if not folders:
        notify(f"⚠️ Окно дня {day}: ролик №{n} не собран.")
        return
    try:
        pub(folders[0])
    except Exception as e:  # noqa: BLE001
        notify(f"❌ Окно дня {day}: не удалось опубликовать №{n}: {e}")
        raise


if __name__ == "__main__":
    cmd = sys.argv[1]
    if cmd == "build":
        build(sys.argv[2] if len(sys.argv) > 2 else date.today().isoformat())
    elif cmd == "due":
        due()
    elif cmd == "publish":
        publish(sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else date.today().isoformat())
