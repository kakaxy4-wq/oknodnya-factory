"""python -m pytest tests/test_pipeline.py — автопубликация и алерты, Telegram замокан."""
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import pipeline  # noqa: E402

DAY = "2026-11-18"
FORBIDDEN = ("без ✅ не выйдут", "ждёт ✅")


class Publisher:
    """Подмена factory.publish.publish / published: сеть не трогаем."""

    def __init__(self):
        self.calls = []
        self.done = set()
        self.fail = None
        self.fail_ids = set()

    def published(self, video_id):
        return video_id in self.done

    def publish(self, folder):
        self.calls.append(folder.name)
        if self.fail is not None:
            raise self.fail
        stem = next(folder.glob("*.mp4")).stem
        if stem in self.fail_ids:
            raise RuntimeError(f"sendVideo: сеть отвалилась ({stem})")
        self.done.add(stem)


@pytest.fixture
def env(monkeypatch, tmp_path):
    root = tmp_path / "repo"
    (root / "content").mkdir(parents=True)
    (root / "out").mkdir()
    approvals = tmp_path / "approvals"
    monkeypatch.setattr(pipeline, "ROOT", root)
    monkeypatch.setattr(pipeline, "SITE", tmp_path / "site")
    monkeypatch.setattr(pipeline, "APPROVALS", approvals)
    monkeypatch.delenv("OKNO_AUTO_PUBLISH", raising=False)
    monkeypatch.setenv("OKNO_BOT_TOKEN", "test-token")
    monkeypatch.setenv("OKNO_OWNER_CHAT", "4242")
    sent = []

    def api(method, fields=None, files=None):
        sent.append({"method": method, "fields": dict(fields or {}), "files": files})
        return {"message_id": len(sent)}

    monkeypatch.setattr("factory.publish.api", api)
    pub = Publisher()
    monkeypatch.setattr("factory.publish.publish", pub.publish)
    monkeypatch.setattr("factory.publish.published", pub.published)
    return SimpleNamespace(root=root, approvals=approvals, sent=sent, pub=pub, monkeypatch=monkeypatch)


def messages(sent):
    return [c["fields"].get("text", "") for c in sent if c["method"] == "sendMessage"]


def videos(sent):
    return [c for c in sent if c["method"] == "sendVideo"]


def captions(sent):
    return [c["fields"].get("caption", "") for c in videos(sent)]


def markup(call):
    return json.loads(call["fields"]["reply_markup"])


def enable_auto(monkeypatch):
    monkeypatch.setenv("OKNO_AUTO_PUBLISH", "1")


def write_qa(folder, *, size=1000, dur=30, width=1080):
    folder.mkdir(parents=True, exist_ok=True)
    payload = {
        "probe": {
            "format": {"size": str(size), "duration": str(dur)},
            "streams": [{"codec_type": "video", "codec_name": "h264", "width": width, "height": 1920}],
        }
    }
    (folder / "qa.json").write_text(json.dumps(payload), encoding="utf-8")


def ready_clip(root, vid, *, post="Пост дня", qa_ok=True):
    folder = root / "out" / DAY / vid
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{vid}.mp4").write_bytes(b"fake-mp4")
    (folder / "post.txt").write_text(post, encoding="utf-8")
    if qa_ok:
        (folder / "QA-OK").write_text("ok\n")
    return folder


def plan_for(*ids, approved=True):
    return {
        "approved": approved,
        "note": "",
        "scenes": {},
        "videos": [{"id": i} for i in ids],
    }


def at(*hm):
    hour, minute = hm
    return datetime(2026, 11, 18, hour, minute)


def run_due(moment):
    """due, который не смог выложить одобренный ролик: слоты обработаны, код выхода ненулевой."""
    with pytest.raises(RuntimeError):
        pipeline.due(moment)


def stub_commands(monkeypatch, fail=None):
    calls = []

    def sh(*args, **kwargs):
        calls.append(args)
        if "pull" in args:
            return subprocess.CompletedProcess(args, 0)
        if fail is not None:
            raise fail
        raise AssertionError(args)

    monkeypatch.setattr(pipeline, "sh", sh)
    monkeypatch.setattr(pipeline, "_port_free", lambda port: False)
    return calls


def test_auto_flag_is_only_one(monkeypatch):
    monkeypatch.delenv("OKNO_AUTO_PUBLISH", raising=False)
    assert pipeline.auto_publish_enabled() is False
    for value in ("", "0", "true", "yes", "on", "2"):
        monkeypatch.setenv("OKNO_AUTO_PUBLISH", value)
        assert pipeline.auto_publish_enabled() is False
    monkeypatch.setenv("OKNO_AUTO_PUBLISH", " 1 ")
    assert pipeline.auto_publish_enabled() is True


def test_slots_and_cutoff_unchanged():
    assert pipeline.SLOTS == {"1": (9, 0), "2": (14, 0), "3": (20, 0)}
    assert pipeline.CUTOFF == (23, 30)
    assert pipeline.LATE_MINUTES == 30
    assert pipeline.AUTO_MARK == "auto\n"


def test_finish_without_flag_keeps_manual_approval(env):
    write_qa(env.root / "out" / DAY / "1-utro")
    (env.root / "out" / DAY / "1-utro" / "post.txt").write_text("Утро на Фонтанке", encoding="utf-8")
    (env.root / "out" / DAY / "1-utro" / "1-utro.mp4").write_bytes(b"mp4")
    pipeline.finish_build(DAY, plan_for("1-utro"))
    assert (env.root / "out" / DAY / "1-utro" / "QA-OK").read_text() == "ok\n"
    assert not (env.approvals / f"{DAY}-1.ok").exists()
    assert messages(env.sent) == [
        f"🎬 Окно дня {DAY}: собрано 1/1. Ниже ролики — без ✅ в канал не выйдут."
    ]
    call = videos(env.sent)[0]
    assert call["fields"]["caption"].startswith(f"№1 на 18.11 · выйдет в 09:00 после ✅\n\nУтро на Фонтанке")
    assert markup(call)["inline_keyboard"] == [[
        {"text": "✅ В канал", "callback_data": f"ov:ok:{DAY}:1"},
        {"text": "⏭ Пропустить", "callback_data": f"ov:skip:{DAY}:1"},
    ]]


def test_finish_with_flag_writes_ok_and_preview(env):
    enable_auto(env.monkeypatch)
    write_qa(env.root / "out" / DAY / "1-utro")
    (env.root / "out" / DAY / "1-utro" / "post.txt").write_text("Утро на Фонтанке", encoding="utf-8")
    (env.root / "out" / DAY / "1-utro" / "1-utro.mp4").write_bytes(b"mp4")
    (env.root / "out" / DAY / "1-utro" / "quiz.json").write_text(
        json.dumps({"options": ["Вологда", "Псков"], "answer": "Вологда"}, ensure_ascii=False),
        encoding="utf-8",
    )
    pipeline.finish_build(DAY, plan_for("1-utro"))
    assert (env.approvals / f"{DAY}-1.ok").read_text(encoding="utf-8") == "auto\n"
    text = messages(env.sent)[0]
    assert "превью" in text and "⏭ снимает ролик до слота" in text
    for phrase in FORBIDDEN:
        assert phrase not in text
    call = videos(env.sent)[0]
    caption = call["fields"]["caption"]
    assert caption.startswith("№1 на 18.11 · выйдет в 09:00\n\nУтро на Фонтанке")
    assert "после ✅" not in caption
    assert "🗳 Под роликом опрос: Вологда · Псков (верно: Вологда)." in caption
    assert markup(call)["inline_keyboard"] == [[
        {"text": "⏭ Пропустить", "callback_data": f"ov:skip:{DAY}:1"},
    ]]
    for blob in [text, caption]:
        for phrase in FORBIDDEN:
            assert phrase not in blob


def test_flag_keeps_skip_across_rebuild_and_does_not_approve(env):
    enable_auto(env.monkeypatch)
    env.approvals.mkdir()
    (env.approvals / f"{DAY}-1.skip").write_text("1\n", encoding="utf-8")
    (env.approvals / f"{DAY}-1.ok").write_text("auto\n", encoding="utf-8")
    write_qa(env.root / "out" / DAY / "1-utro")
    (env.root / "out" / DAY / "1-utro" / "post.txt").write_text("ещё раз", encoding="utf-8")
    (env.root / "out" / DAY / "1-utro" / "1-utro.mp4").write_bytes(b"mp4")
    write_qa(env.root / "out" / DAY / "2-den")
    (env.root / "out" / DAY / "2-den" / "post.txt").write_text("день", encoding="utf-8")
    (env.root / "out" / DAY / "2-den" / "2-den.mp4").write_bytes(b"mp4")
    pipeline.finish_build(DAY, plan_for("1-utro", "2-den"))
    assert (env.approvals / f"{DAY}-1.skip").exists()
    assert not (env.approvals / f"{DAY}-1.ok").exists()
    assert (env.approvals / f"{DAY}-2.ok").read_text(encoding="utf-8") == "auto\n"
    caps = captions(env.sent)
    assert any(c.startswith("№1 на 18.11 · не выйдет в 09:00: уже стоит ⏭") for c in caps)
    assert any(c.startswith("№2 на 18.11 · выйдет в 14:00\n") for c in caps)
    assert all("после ✅" not in c for c in caps)


def test_rebuild_without_flag_clears_ok_and_skip(env):
    env.approvals.mkdir()
    (env.approvals / f"{DAY}-1.ok").write_text("owner\n", encoding="utf-8")
    (env.approvals / f"{DAY}-1.skip").write_text("1\n", encoding="utf-8")
    write_qa(env.root / "out" / DAY / "1-utro")
    (env.root / "out" / DAY / "1-utro" / "post.txt").write_text("утро", encoding="utf-8")
    (env.root / "out" / DAY / "1-utro" / "1-utro.mp4").write_bytes(b"mp4")
    pipeline.finish_build(DAY, plan_for("1-utro"))
    assert not (env.approvals / f"{DAY}-1.ok").exists()
    assert not (env.approvals / f"{DAY}-1.skip").exists()


def test_auto_ok_does_not_overwrite_existing_approval(env):
    enable_auto(env.monkeypatch)
    env.approvals.mkdir()
    path = env.approvals / f"{DAY}-1.ok"
    path.write_text("owner\n", encoding="utf-8")
    assert pipeline.write_auto_ok(DAY, "1") is True
    assert path.read_text(encoding="utf-8") == "owner\n"


def test_draft_note_survives_and_auto_summary_has_no_gate_phrase(env):
    enable_auto(env.monkeypatch)
    write_qa(env.root / "out" / DAY / "1-utro")
    (env.root / "out" / DAY / "1-utro" / "post.txt").write_text("утро", encoding="utf-8")
    (env.root / "out" / DAY / "1-utro" / "1-utro.mp4").write_bytes(b"mp4")
    pipeline.finish_build(DAY, plan_for("1-utro", approved=False))
    text = messages(env.sent)[0]
    assert "черновик задачи" in text
    for phrase in FORBIDDEN:
        assert phrase not in text


def test_qa_failure_alerts_once_with_reason_and_blocks_auto_ok(env):
    enable_auto(env.monkeypatch)
    folder = env.root / "out" / DAY / "1-utro"
    write_qa(folder, size=50_000_000, dur=9, width=720)
    (folder / "post.txt").write_text("утро", encoding="utf-8")
    (folder / "1-utro.mp4").write_bytes(b"mp4")
    pipeline.finish_build(DAY, plan_for("1-utro", "2-net-papki"))
    assert not (folder / "QA-OK").exists()
    assert not (env.approvals / f"{DAY}-1.ok").exists()
    notes = messages(env.sent)
    qa = [t for t in notes if "не прошёл QA" in t]
    assert len(qa) == 2
    assert "№1" in qa[0] and "50.0 МБ" in qa[0] and "9.0 с" in qa[0] and "1080" in qa[0]
    assert "папка не собрана" in qa[1]
    assert any("В слоты выходить нечему" in t for t in notes)
    pipeline.finish_build(DAY, plan_for("1-utro", "2-net-papki"))
    assert len([t for t in messages(env.sent) if "не прошёл QA" in t]) == 2


def test_qa_reason_boundaries(tmp_path):
    def reason(**kw):
        folder = tmp_path / "clip"
        if folder.exists():
            for child in folder.iterdir():
                child.unlink()
        write_qa(folder, **kw)
        return pipeline.qa_failure_reason(folder)

    assert reason(size=48_999_999, dur=10.01, width=1080) is None
    assert "лимит 49" in reason(size=49_000_000, dur=30, width=1080)
    assert "длительность" in reason(size=1000, dur=10, width=1080)
    assert "длительность" in reason(size=1000, dur=60, width=1080)
    assert "1080" in reason(size=1000, dur=30, width=1079)
    (tmp_path / "broken").mkdir()
    (tmp_path / "broken" / "qa.json").write_text("{", encoding="utf-8")
    assert "не разбирается" in pipeline.qa_failure_reason(tmp_path / "broken")
    empty = tmp_path / "empty"
    empty.mkdir()
    assert pipeline.qa_failure_reason(empty) == "нет qa.json"


def test_missing_plan_alerts_once_and_does_not_record(env):
    calls = stub_commands(env.monkeypatch)
    pipeline.build(DAY)
    pipeline.build(DAY)
    assert len(calls) == 4  # два git pull на каждый заход, record.js не запускается
    assert all(a[0] == "git" and "pull" in a for a in calls)
    notes = messages(env.sent)
    assert notes == [f"⚠️ Окно дня {DAY}: нет файла content/{DAY}.json с approved: true — сегодня ролики не выйдут."]
    assert (env.root / "log" / "alerts" / f"{DAY}-no-content.sent").exists()


def test_unapproved_plan_alerts_and_build_still_starts(env):
    calls = stub_commands(env.monkeypatch, fail=subprocess.CalledProcessError(1, ["node", "record.js"]))
    (env.root / "content" / f"{DAY}.json").write_text(
        json.dumps({"approved": False, "videos": [], "scenes": {"a": {"url": "/", "sec": 1}}}),
        encoding="utf-8",
    )
    with pytest.raises(subprocess.CalledProcessError):
        pipeline.build(DAY)
    assert any("record.js" in str(a) for a in calls)
    notes = messages(env.sent)
    assert any("нет approved: true" in t and "только после ✅" in t for t in notes)
    assert any("сборка упала" in t for t in notes)
    env.sent.clear()
    with pytest.raises(subprocess.CalledProcessError):
        pipeline.build(DAY)
    assert env.sent == []


def test_unapproved_plan_with_flag_names_skip(env):
    enable_auto(env.monkeypatch)
    stub_commands(env.monkeypatch, fail=subprocess.CalledProcessError(1, ["node"]))
    (env.root / "content" / f"{DAY}.json").write_text(
        json.dumps({"approved": False, "scenes": {"a": {"url": "/", "sec": 1}}, "videos": []}),
        encoding="utf-8",
    )
    with pytest.raises(subprocess.CalledProcessError):
        pipeline.build(DAY)
    text = "\n".join(messages(env.sent))
    assert "выйдут в слоты сами" in text
    for phrase in FORBIDDEN:
        assert phrase not in text


def test_build_crash_alerts_once(env):
    stub_commands(env.monkeypatch, fail=subprocess.CalledProcessError(2, ["node", "record.js"]))
    (env.root / "content" / f"{DAY}.json").write_text(
        json.dumps({"approved": True, "videos": [], "scenes": {"a": {"url": "/", "sec": 1}}}),
        encoding="utf-8",
    )
    with pytest.raises(subprocess.CalledProcessError):
        pipeline.build(DAY)
    notes = messages(env.sent)
    assert len(notes) == 1 and "сборка упала" in notes[0] and "record.js" in notes[0]
    env.sent.clear()
    with pytest.raises(subprocess.CalledProcessError):
        pipeline.build(DAY)
    assert env.sent == []


def test_broken_plan_json_is_a_build_alert(env):
    stub_commands(env.monkeypatch)
    (env.root / "content" / f"{DAY}.json").write_text("{", encoding="utf-8")
    with pytest.raises(json.JSONDecodeError):
        pipeline.build(DAY)
    assert len(messages(env.sent)) == 1
    assert "сборка упала" in messages(env.sent)[0]


def test_alert_retries_until_telegram_accepts(env):
    state = {"n": 0}

    def flaky(method, fields=None, files=None):
        state["n"] += 1
        if state["n"] == 1:
            raise RuntimeError("timeout")
        env.sent.append({"method": method, "fields": dict(fields or {}), "files": files})
        return {"message_id": 1}

    env.monkeypatch.setattr("factory.publish.api", flaky)
    assert pipeline.alert_once(f"{DAY}-1-late", "один раз") is False
    mark = env.root / "log" / "alerts" / f"{DAY}-1-late.sent"
    assert not mark.exists()
    assert pipeline.alert_once(f"{DAY}-1-late", "один раз") is True
    assert mark.read_text(encoding="utf-8") == "один раз"
    assert pipeline.alert_once(f"{DAY}-1-late", "один раз") is False
    assert state["n"] == 2


def test_alert_without_token_is_marked_so_due_does_not_spam(env, monkeypatch):
    monkeypatch.delenv("OKNO_BOT_TOKEN")
    assert pipeline.alert_once(f"{DAY}-x", "некуда слать") is False
    assert (env.root / "log" / "alerts" / f"{DAY}-x.sent").exists()
    assert pipeline.alert_once(f"{DAY}-x", "некуда слать") is False
    assert env.sent == []


def test_due_without_flag_reminds_once_and_does_not_publish(env):
    ready_clip(env.root, "1-utro")
    pipeline.due(at(9, 0))
    pipeline.due(at(9, 5))
    assert env.pub.calls == []
    assert not (env.approvals / f"{DAY}-1.ok").exists()
    assert messages(env.sent) == [
        f"⏰ Окно дня {DAY}: ролик №1 ждёт ✅ — выйдет сразу после одобрения (до 23:30)."
    ]
    assert (env.root / "out" / DAY / "1-utro" / "REMINDED").exists()


def test_due_without_flag_publishes_approved_video(env):
    ready_clip(env.root, "1-utro")
    env.approvals.mkdir()
    (env.approvals / f"{DAY}-1.ok").write_text("owner\n", encoding="utf-8")
    pipeline.due(at(8, 59))
    assert env.pub.calls == []
    pipeline.due(at(9, 0))
    assert env.pub.calls == ["1-utro"]
    assert "1-utro" in env.pub.done
    assert messages(env.sent) == []
    pipeline.due(at(9, 5))
    assert env.pub.calls == ["1-utro"]


def test_due_with_flag_approves_and_publishes_without_reminder(env):
    enable_auto(env.monkeypatch)
    ready_clip(env.root, "1-utro")
    pipeline.due(at(9, 0))
    assert (env.approvals / f"{DAY}-1.ok").read_text(encoding="utf-8") == "auto\n"
    assert env.pub.calls == ["1-utro"]
    blob = "\n".join(messages(env.sent) + captions(env.sent))
    for phrase in FORBIDDEN:
        assert phrase not in blob


def test_due_with_flag_respects_skip_and_does_not_alert(env):
    enable_auto(env.monkeypatch)
    ready_clip(env.root, "1-utro")
    env.approvals.mkdir()
    (env.approvals / f"{DAY}-1.skip").write_text("1\n", encoding="utf-8")
    (env.approvals / f"{DAY}-1.ok").write_text("auto\n", encoding="utf-8")
    pipeline.due(at(9, 0))
    pipeline.due(at(9, 30))
    pipeline.due(at(23, 30))
    assert env.pub.calls == []
    assert messages(env.sent) == []
    assert (env.approvals / f"{DAY}-1.ok").read_text(encoding="utf-8") == "auto\n"


def test_due_does_not_restore_ok_over_skip(env):
    enable_auto(env.monkeypatch)
    ready_clip(env.root, "1-utro")
    env.approvals.mkdir()
    (env.approvals / f"{DAY}-1.skip").write_text("1\n", encoding="utf-8")
    pipeline.due(at(9, 0))
    assert not (env.approvals / f"{DAY}-1.ok").exists()
    assert env.pub.calls == []


def test_due_keeps_owner_ok_text(env):
    enable_auto(env.monkeypatch)
    ready_clip(env.root, "2-den")
    env.approvals.mkdir()
    (env.approvals / f"{DAY}-2.ok").write_text("owner\n", encoding="utf-8")
    pipeline.due(at(14, 0))
    assert (env.approvals / f"{DAY}-2.ok").read_text(encoding="utf-8") == "owner\n"
    assert env.pub.calls == ["2-den"]


def test_publish_failure_alerts_at_plus_30_and_cutoff_without_duplicates(env):
    ready_clip(env.root, "1-utro")
    ready_clip(env.root, "2-den")
    env.approvals.mkdir()
    (env.approvals / f"{DAY}-1.ok").write_text("owner\n", encoding="utf-8")
    (env.approvals / f"{DAY}-2.ok").write_text("owner\n", encoding="utf-8")
    env.pub.fail = RuntimeError("sendVideo: network down")

    run_due(at(9, 0))
    run_due(at(9, 25))
    assert env.pub.calls == ["1-utro", "1-utro"]
    assert messages(env.sent) == []

    run_due(at(9, 30))
    late = messages(env.sent)
    assert late == [
        f"❌ Окно дня {DAY}: ролик №1 не опубликован через 30 минут после слота 09:00. "
        "Ошибка: sendVideo: network down. Повторяю попытки до 23:30."
    ]
    run_due(at(9, 35))
    assert messages(env.sent) == late
    assert env.pub.calls.count("1-utro") == 4  # слот, +25, +30, +35 — попытки не прекратились

    run_due(at(14, 0))
    assert "2-den" in env.pub.calls
    assert not any("№2" in t for t in messages(env.sent))

    run_due(at(23, 30))
    missed = [t for t in messages(env.sent) if "не вышел" in t]
    assert missed == [
        f"❌ Окно дня {DAY}: ролик №1 не вышел к 23:30. Ошибка: sendVideo: network down",
        f"❌ Окно дня {DAY}: ролик №2 не вышел к 23:30. Ошибка: sendVideo: network down",
    ]
    assert messages(env.sent).count(late[0]) == 1
    run_due(at(23, 30))
    assert [t for t in messages(env.sent) if "не вышел" in t] == missed
    assert (env.root / "log" / "alerts" / f"{DAY}-1-late.sent").exists()
    assert (env.root / "log" / "alerts" / f"{DAY}-1-missed.sent").exists()
    assert (env.root / "log" / "alerts" / f"{DAY}-2-missed.sent").exists()
    assert not (env.root / "log" / "alerts" / f"{DAY}-2-late.sent").exists()


def test_due_still_retries_after_late_alert_and_skips_final_if_published(env):
    ready_clip(env.root, "1-utro")
    env.approvals.mkdir()
    (env.approvals / f"{DAY}-1.ok").write_text("owner\n", encoding="utf-8")
    env.pub.fail = RuntimeError("timeout")
    run_due(at(9, 30))
    assert any("30 минут" in t for t in messages(env.sent))
    env.pub.fail = None
    pipeline.due(at(10, 0))
    assert "1-utro" in env.pub.done
    pipeline.due(at(23, 30))
    assert not any("не вышел" in t for t in messages(env.sent))


def test_success_at_cutoff_does_not_send_missed_alert(env):
    ready_clip(env.root, "3-vecher")
    env.approvals.mkdir()
    (env.approvals / f"{DAY}-3.ok").write_text("owner\n", encoding="utf-8")
    env.pub.fail = RuntimeError("timeout")
    run_due(at(20, 30))
    env.pub.fail = None
    pipeline.due(at(23, 30))
    assert "3-vecher" in env.pub.done
    assert not any("не вышел" in t for t in messages(env.sent))


def test_one_slot_failure_does_not_block_the_next(env):
    ready_clip(env.root, "1-utro")
    ready_clip(env.root, "2-den")
    env.approvals.mkdir()
    (env.approvals / f"{DAY}-1.ok").write_text("owner\n", encoding="utf-8")
    (env.approvals / f"{DAY}-2.ok").write_text("owner\n", encoding="utf-8")
    env.pub.fail_ids.add("1-utro")
    with pytest.raises(RuntimeError, match="№1"):
        pipeline.due(at(20, 0))
    assert "2-den" in env.pub.done
    assert "1-utro" not in env.pub.done
    assert any("1-utro" in name for name in env.pub.calls)


def test_cutoff_window(env):
    ready_clip(env.root, "1-utro")
    env.approvals.mkdir()
    (env.approvals / f"{DAY}-1.ok").write_text("owner\n", encoding="utf-8")
    pipeline.due(at(23, 31))
    assert env.pub.calls == []
    pipeline.due(at(23, 30))
    assert env.pub.calls == ["1-utro"]


@pytest.mark.parametrize("n,hh,mm", [("1", 9, 0), ("2", 14, 0), ("3", 20, 0)])
def test_late_alert_follows_each_slot(env, n, hh, mm):
    vid = f"{n}-clip"
    ready_clip(env.root, vid)
    env.approvals.mkdir()
    (env.approvals / f"{DAY}-{n}.ok").write_text("owner\n", encoding="utf-8")
    env.pub.fail = RuntimeError("api")
    run_due(at(hh, mm + 29))
    assert messages(env.sent) == []
    assert env.pub.calls == [vid]
    run_due(at(hh, mm + 30))
    assert len(messages(env.sent)) == 1
    assert f"после слота {hh:02d}:{mm:02d}" in messages(env.sent)[0]
    assert "Ошибка: api" in messages(env.sent)[0]


def test_flag_on_does_not_remind_when_auto_ok_cannot_be_written(env):
    enable_auto(env.monkeypatch)
    ready_clip(env.root, "1-utro")
    env.monkeypatch.setattr(pipeline, "write_auto_ok", lambda day, n: False)
    pipeline.due(at(9, 0))
    assert env.pub.calls == []
    assert messages(env.sent) == []
    assert not (env.root / "out" / DAY / "1-utro" / "REMINDED").exists()


def test_manual_publish_still_reports_immediately(env):
    ready_clip(env.root, "1-utro")

    def boom(folder):
        raise RuntimeError("sendVideo: 400")

    env.monkeypatch.setattr("factory.publish.publish", boom)
    with pytest.raises(RuntimeError):
        pipeline.publish("1", DAY)
    assert messages(env.sent) == [f"❌ Окно дня {DAY}: не удалось опубликовать №1: sendVideo: 400"]
    assert "sendVideo: 400" in (env.root / "log" / "alerts" / f"{DAY}-1.err").read_text(encoding="utf-8")


def test_manual_publish_missing_folder(env):
    pipeline.publish("2", DAY)
    assert messages(env.sent) == [f"⚠️ Окно дня {DAY}: ролик №2 не собран."]


def test_preview_failure_alerts_once(env):
    enable_auto(env.monkeypatch)
    write_qa(env.root / "out" / DAY / "1-utro")
    (env.root / "out" / DAY / "1-utro" / "post.txt").write_text("утро", encoding="utf-8")
    (env.root / "out" / DAY / "1-utro" / "1-utro.mp4").write_bytes(b"mp4")

    def api(method, fields=None, files=None):
        if method == "sendVideo":
            raise RuntimeError("entity too large")
        env.sent.append({"method": method, "fields": dict(fields or {}), "files": files})
        return {"message_id": 1}

    env.monkeypatch.setattr("factory.publish.api", api)
    pipeline.finish_build(DAY, plan_for("1-utro"))
    pipeline.finish_build(DAY, plan_for("1-utro"))
    preview = [t for t in messages(env.sent) if "Не смог прислать" in t]
    assert preview == [f"Не смог прислать ролик 1-utro на превью: entity too large"]


def test_no_qa_means_no_late_alert(env):
    ready_clip(env.root, "1-utro", qa_ok=False)
    env.approvals.mkdir()
    (env.approvals / f"{DAY}-1.ok").write_text("owner\n", encoding="utf-8")
    pipeline.due(at(9, 30))
    pipeline.due(at(23, 30))
    assert env.pub.calls == []
    assert messages(env.sent) == []


def test_missing_mp4_is_the_late_error(env):
    folder = ready_clip(env.root, "1-utro")
    (folder / "1-utro.mp4").unlink()
    env.approvals.mkdir()
    (env.approvals / f"{DAY}-1.ok").write_text("owner\n", encoding="utf-8")
    run_due(at(9, 30))
    assert "нет mp4" in messages(env.sent)[0]
    assert env.pub.calls == []
