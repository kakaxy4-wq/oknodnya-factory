"""Ежедневный конвейер на сервере.

  python3 pipeline.py build [ДАТА]     # 06:00 — git pull, запись сцен, сборка, техпроверка → out/ДАТА/*/QA-OK,
                                       #   каждый ролик — владельцу в личку бота.
                                       #   Без OKNO_AUTO_PUBLISH — кнопки «✅ В канал / ⏭ Пропустить».
                                       #   С OKNO_AUTO_PUBLISH=1 — превью «выйдет в HH:MM» и кнопка «⏭ Пропустить»,
                                       #   approvals/ДАТА-N.ok пишется сам, если нет .skip.
  python3 pipeline.py due              # каждые 5 минут: одобренный ролик выходит в свой слот (09:00 / 14:00 / 20:00)
                                       #   или сразу после одобрения, если слот уже прошёл (до 23:30).
                                       #   Не вышел — один алерт через 30 минут после слота и последний в 23:30.
  python3 pipeline.py publish N [ДАТА] # вручную: опубликовать ролик N (1, 2, 3) без одобрения

Кнопки обрабатывает бот сайта (тот же токен, он и так слушает Telegram): решение — файл approvals/ДАТА-N.ok|.skip.
OKNO_AUTO_PUBLISH=1 (любое другое значение и пусто — выключено, нужен ✅ как раньше).

Сценарии пишет Claude (ежедневная задача) в content/ДАТА.json. "approved": false сборку не останавливает.
Нет файла с "approved": true, падение сборки и провал QA — одно сообщение владельцу на дату или ролик.
Повторно тот же алерт не шлётся: метка log/alerts/<ключ>.sent.
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
APPROVALS = Path(os.environ.get("OKNO_APPROVALS", ROOT / "approvals"))
SLOTS = {"1": (9, 0), "2": (14, 0), "3": (20, 0)}
CUTOFF = (23, 30)
LATE_MINUTES = 30
AUTO_MARK = "auto\n"


def sh(*args, **kw):
    print("$", " ".join(map(str, args)), flush=True)
    return subprocess.run(args, check=True, **kw)


def auto_publish_enabled():
    """Ровно OKNO_AUTO_PUBLISH=1. Без переменной и при любом другом значении поведение прежнее."""
    return os.environ.get("OKNO_AUTO_PUBLISH", "").strip() == "1"


def alerts_dir():
    return ROOT / "log" / "alerts"


def notify(text):
    """Сообщение владельцу в личку бота. True — Telegram принял sendMessage."""
    chat = os.environ.get("OKNO_OWNER_CHAT")
    if not chat or not os.environ.get("OKNO_BOT_TOKEN"):
        print("notify:", text)
        return False
    from factory.publish import api
    try:
        api("sendMessage", {"chat_id": chat, "text": text[:4000]})
    except Exception as e:  # noqa: BLE001
        print("notify failed:", e)
        return False
    return True


def alert_once(key, text):
    """Один алерт на ключ (дата или дата-ролик). Метка пишется только после доставки.

    Нет чата или токена — метка всё равно ставится, чтобы due не печатал одно и то же каждые 5 минут.
    Сбой sendMessage метку не ставит: следующая попытка due повторит.
    """
    directory = alerts_dir()
    directory.mkdir(parents=True, exist_ok=True)
    mark = directory / f"{key}.sent"
    if mark.exists():
        return False
    delivered = notify(text)
    if not delivered and os.environ.get("OKNO_OWNER_CHAT") and os.environ.get("OKNO_BOT_TOKEN"):
        return False
    mark.write_text(text, encoding="utf-8")
    return delivered


def remember_error(day, n, exc):
    text = str(exc).strip().replace("\n", " ")
    if not text:
        text = exc.__class__.__name__
    if len(text) > 500:
        text = text[:500] + "…"
    directory = alerts_dir()
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{day}-{n}.err").write_text(text, encoding="utf-8")


def read_error(day, n):
    path = alerts_dir() / f"{day}-{n}.err"
    if not path.exists():
        return "причина не записана"
    return path.read_text(encoding="utf-8").strip() or "причина не записана"


def _port_free(port):
    with socket.socket() as s:
        return s.connect_ex(("127.0.0.1", port)) != 0


def load_plan(day):
    """План на дату. None — файла нет, собирать нечего. Без approved: true — алерт, план всё равно возвращается."""
    plan_path = ROOT / "content" / f"{day}.json"
    if not plan_path.exists():
        alert_once(
            f"{day}-no-content",
            f"⚠️ Окно дня {day}: нет файла content/{day}.json с approved: true — сегодня ролики не выйдут.",
        )
        return None
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    if not plan.get("approved"):
        if auto_publish_enabled():
            extra = "Ролики соберутся и выйдут в слоты сами — ⏭ снимет ролик до слота."
        else:
            extra = "Сборка продолжится, в канал ролик попадёт только после ✅."
        alert_once(
            f"{day}-not-approved",
            f"⚠️ Окно дня {day}: в content/{day}.json нет approved: true к моменту сборки. {extra}",
        )
    return plan


def qa_failure_reason(folder):
    """None, если техпроверка пройдена. Иначе короткая причина для алерта."""
    qa_path = folder / "qa.json"
    if not qa_path.exists():
        return "нет qa.json"
    try:
        qa = json.loads(qa_path.read_text(encoding="utf-8"))
        streams = qa["probe"]["streams"]
        size = int(qa["probe"]["format"]["size"])
        dur = float(qa["probe"]["format"]["duration"])
    except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError) as e:
        return f"qa.json не разбирается: {e}"
    problems = []
    if size >= 49e6:
        problems.append(f"размер {size / 1e6:.1f} МБ, лимит 49")
    if not (10 < dur < 60):
        problems.append(f"длительность {dur:.1f} с, нужно больше 10 и меньше 60")
    if not any(s.get("width") == 1080 for s in streams):
        problems.append("нет кадра шириной 1080")
    return "; ".join(problems) or None


def apply_qa(day, videos):
    """Пишет QA-OK прошедшим роликам. По каждому провалу — один алерт с причиной."""
    ok = []
    for v in videos:
        vid = v["id"]
        n = vid.split("-")[0]
        folder = ROOT / "out" / day / vid
        if not folder.is_dir():
            alert_once(
                f"{day}-{n}-qa",
                f"❌ Окно дня {day}: ролик №{n} ({vid}) не прошёл QA: папка не собрана.",
            )
            continue
        try:
            reason = qa_failure_reason(folder)
        except Exception as e:  # noqa: BLE001
            reason = f"проверка не выполнилась: {e}"
        if reason:
            alert_once(
                f"{day}-{n}-qa",
                f"❌ Окно дня {day}: ролик №{n} ({vid}) не прошёл QA: {reason}.",
            )
            continue
        (folder / "QA-OK").write_text("ok\n")
        ok.append(vid)
    return ok


def reset_approvals(day):
    """Пересборка сбрасывает ✅ прошлой версии ролика.

    При автопубликации ⏭ не трогаем: владелец уже снял ролик, и новый .ok без .skip
    снова поставил бы его в слот.
    """
    if not APPROVALS.is_dir():
        return
    keep_skip = auto_publish_enabled()
    for path in APPROVALS.glob(f"{day}-*"):
        if keep_skip and path.name.endswith(".skip"):
            continue
        path.unlink(missing_ok=True)


def write_auto_ok(day, n):
    """При флаге пишет approvals/ДАТА-N.ok, если нет .skip. Уже лежащий .ok не перезаписывает.

    True — флаг включён и ролик не снят (файл создан сейчас или был раньше).
    """
    if not auto_publish_enabled():
        return False
    if (APPROVALS / f"{day}-{n}.skip").exists():
        return False
    APPROVALS.mkdir(parents=True, exist_ok=True)
    path = APPROVALS / f"{day}-{n}.ok"
    if not path.exists():
        path.write_text(AUTO_MARK, encoding="utf-8")
        print(f"автоодобрение: {day}-{n}", flush=True)
    return True


def finish_build(day, plan):
    """QA, авто-.ok и превью владельцу. Вызывается в конце сборки и из тестов."""
    ok = apply_qa(day, plan["videos"])
    reset_approvals(day)
    for vid in ok:
        write_auto_ok(day, vid.split("-")[0])
    notify(_summary(day, ok, len(plan["videos"]), not plan.get("approved")))
    for vid in ok:
        send_for_review(day, vid)
    return ok


def _summary(day, ok, total, draft):
    note = " Сценарии — черновик задачи (кадры она не смотрела): проверьте ролики внимательнее." if draft else ""
    if auto_publish_enabled():
        if ok:
            tail = " Ниже превью — ролики выйдут в свои слоты. ⏭ снимает ролик до слота."
        else:
            tail = " В слоты выходить нечему."
        return f"🎬 Окно дня {day}: собрано {len(ok)}/{total}.{tail}{note}"
    return f"🎬 Окно дня {day}: собрано {len(ok)}/{total}. Ниже ролики — без ✅ в канал не выйдут.{note}"


def build(day):
    try:
        _build(day)
    except Exception as e:  # noqa: BLE001 — таймер oneshot, без сообщения владелец увидит только journal
        alert_once(f"{day}-build", f"❌ Окно дня {day}: сборка упала: {e}")
        raise


def _build(day):
    for repo in (ROOT, SITE):                 # свежие сценарии и свежая версия сайта; без сети — работаем с тем, что есть
        try:
            sh("git", "-C", str(repo), "pull", "--ff-only", "-q")
        except subprocess.CalledProcessError as e:
            print("git pull не удался:", repo, e)
    plan = load_plan(day)
    if plan is None:
        return
    plan_path = ROOT / "content" / f"{day}.json"
    clips = ROOT / "clips"
    clips.mkdir(exist_ok=True)
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
        sh("node", str(ROOT / "recorder" / "record.js"), str(clips), f.name, cwd=ROOT / "recorder")
    finally:
        if srv:
            srv.terminate()
    sh(sys.executable, str(ROOT / "build.py"), str(plan_path), "--clips", str(clips))
    finish_build(day, plan)


def send_for_review(day, vid):
    """Ролик владельцу в личку. Решение по кнопке пишет бот сайта в approvals/."""
    chat = os.environ.get("OKNO_OWNER_CHAT")
    if not chat:
        return
    from factory.publish import api
    folder, n = ROOT / "out" / day / vid, vid.split("-")[0]
    hh, mm = SLOTS.get(n, (0, 0))
    when = f"{day[8:]}.{day[5:7]}"
    slot = f"{hh:02d}:{mm:02d}"
    if auto_publish_enabled() and (APPROVALS / f"{day}-{n}.skip").exists():
        head = f"№{n} на {when} · не выйдет в {slot}: уже стоит ⏭"
    elif auto_publish_enabled():
        head = f"№{n} на {when} · выйдет в {slot}"
    else:
        head = f"№{n} на {when} · выйдет в {slot} после ✅"
    skip = {"text": "⏭ Пропустить", "callback_data": f"ov:skip:{day}:{n}"}
    ok_btn = {"text": "✅ В канал", "callback_data": f"ov:ok:{day}:{n}"}
    row = [skip] if auto_publish_enabled() else [ok_btn, skip]
    kb = {"inline_keyboard": [row]}
    try:
        post = (folder / "post.txt").read_text(encoding="utf-8")
        if (folder / "quiz.json").exists():  # загадка: владелец видит и варианты опроса
            q = json.loads((folder / "quiz.json").read_text(encoding="utf-8"))
            post += f"\n\n🗳 Под роликом опрос: {' · '.join(q['options'])} (верно: {q['answer']}). Разгадка — в 21:00."
        caption = f"{head}\n\n{post}"[:1024]
        mp4 = next(folder.glob("*.mp4"))
        api("sendVideo", {"chat_id": chat, "caption": caption, "supports_streaming": "true", "width": 1080, "height": 1920,
                          "reply_markup": json.dumps(kb, ensure_ascii=False)},
            {"video": (mp4.name, mp4.read_bytes(), "video/mp4")})
    except Exception as e:  # noqa: BLE001
        what = "на превью" if auto_publish_enabled() else "на одобрение"
        alert_once(f"{day}-{n}-preview", f"Не смог прислать ролик {vid} {what}: {e}")


def decision(day, n):
    if (APPROVALS / f"{day}-{n}.skip").exists():
        return "skip"
    return "ok" if (APPROVALS / f"{day}-{n}.ok").exists() else None


def video_folder(day, n):
    out = ROOT / "out" / day
    if not out.is_dir():
        return None
    folders = sorted(p for p in out.glob(f"{n}-*") if p.is_dir())
    return folders[0] if folders else None


def due(now=None):
    """Раз в 5 минут: одобренные ролики, чей слот наступил, — в канал.

    Без флага неодобренному — одно напоминание «ждёт ✅». С флагом его нет: .ok уже стоит,
    либо ролик снят через .skip. Ошибка публикации не шлётся каждые 5 минут: один раз через
    30 минут после слота и последний раз в 23:30. Сами попытки идут до 23:30 включительно.
    """
    from factory.publish import publish as pub
    from factory.publish import published

    now = now or datetime.now()
    if (now.hour, now.minute) > CUTOFF:
        return
    day = now.date().isoformat()
    failed = []
    for n, (hh, mm) in SLOTS.items():
        if (now.hour, now.minute) < (hh, mm):
            continue
        if _due_slot(day, n, hh, mm, now, pub, published) is False:
            failed.append(n)
    if failed:  # все слоты уже обработаны; ненулевой код — как раньше, но без письма каждые 5 минут
        raise RuntimeError(f"Окно дня {day}: не опубликованы №{', '.join(failed)}")


def _due_slot(day, n, hh, mm, now, pub, published):
    """False — одобренный ролик к этому запуску так и не вышел."""
    folder = video_folder(day, n)
    if folder is None or not (folder / "QA-OK").exists():
        return True
    write_auto_ok(day, n)
    state = decision(day, n)
    if state == "skip":
        return True
    if state == "ok":
        if not _published_or_try(day, n, folder, pub, published):
            _alert_unpublished(day, n, hh, mm, now)
            return False
        return True
    if auto_publish_enabled():
        return True
    if not (folder / "REMINDED").exists():
        (folder / "REMINDED").write_text("1\n")
        notify(f"⏰ Окно дня {day}: ролик №{n} ждёт ✅ — выйдет сразу после одобрения (до 23:30).")
    return True


def _published_or_try(day, n, folder, pub, published):
    mp4 = next(folder.glob("*.mp4"), None)
    if mp4 is None:
        remember_error(day, n, "в папке ролика нет mp4")
        return False
    if published(mp4.stem):
        return True
    try:
        pub(folder)
    except Exception as e:  # noqa: BLE001
        remember_error(day, n, e)
        print(f"публикация №{n} не удалась:", e, flush=True)
        return False
    return True


def _alert_unpublished(day, n, hh, mm, now):
    err = read_error(day, n)
    if (now.hour, now.minute) >= CUTOFF:
        alert_once(
            f"{day}-{n}-missed",
            f"❌ Окно дня {day}: ролик №{n} не вышел к 23:30. Ошибка: {err}",
        )
        return
    slot = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if now >= slot + timedelta(minutes=LATE_MINUTES):
        alert_once(
            f"{day}-{n}-late",
            f"❌ Окно дня {day}: ролик №{n} не опубликован через 30 минут после слота "
            f"{hh:02d}:{mm:02d}. Ошибка: {err}. Повторяю попытки до 23:30.",
        )


def publish(n, day):
    from factory.publish import publish as pub
    folders = sorted((ROOT / "out" / day).glob(f"{n}-*"))
    if not folders:
        notify(f"⚠️ Окно дня {day}: ролик №{n} не собран.")
        return
    try:
        pub(folders[0])
    except Exception as e:  # noqa: BLE001
        remember_error(day, n, e)
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
