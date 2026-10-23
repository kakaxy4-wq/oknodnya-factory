"""Ежедневный конвейер на сервере.

  python3 pipeline.py build [ДАТА]     # 06:00 — git pull, запись сцен, сборка, техпроверка → out/ДАТА/*/QA-OK
  python3 pipeline.py publish N [ДАТА] # 09:00 / 14:00 / 20:00 — публикация ролика N (1, 2, 3)

Сценарии пишет Claude (ежедневная задача) в content/ДАТА.json и ставит "approved": true только после просмотра кадров.
Без одобренного плана сервер ничего не собирает и не публикует.
"""
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SITE = Path(os.environ.get("OKNO_SITE_REPO", "/opt/lofi-goroda-public"))
CLIPS = ROOT / "clips"


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
    if not plan.get("approved"):
        notify(f"⚠️ Окно дня: сценарии на {day} не одобрены — публикации не будет.")
        return
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
    notify(f"🎬 Окно дня {day}: собрано {len(ok)}/{len(plan['videos'])} — {', '.join(ok)}")


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
    elif cmd == "publish":
        publish(sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else date.today().isoformat())
