"""Фабрика «Окно дня»: сценарии дня → голос → музыка → ролики 1080×1920 → проверка качества → пакет для публикации.

Запуск: python build.py content/2026-10-23.json --clips <папка с записями сцены>
"""
import argparse
import json
import shutil
import subprocess
from pathlib import Path

from factory import audio, compose

ROOT = Path(__file__).resolve().parent


def build_video(v, clips: Path, out_dir: Path):
    work = out_dir / "work"
    work.mkdir(parents=True, exist_ok=True)
    timings, vo_len = audio.voice(v["lines"], work / "voice.wav", work)
    duration = round(vo_len + 0.6, 2)
    # длина сцены = длина озвучки (последний план растягивается)
    shots = [dict(s) for s in v["shots"]]
    if all("at" in s for s in shots):
        # смена плана привязана к реплике: "at": [номер реплики, сдвиг в секундах от её начала]
        starts = [0.0 if i == 0 else timings[s["at"][0]][0] + s["at"][1] for i, s in enumerate(shots)]
        for i, s in enumerate(shots):
            end = starts[i + 1] if i + 1 < len(shots) else duration
            s["len"] = round(end - starts[i] + (0.5 if i + 1 < len(shots) else 0.3), 2)   # +0,5 с на наплыв
    else:
        total = sum(s["len"] for s in shots) - 0.5 * (len(shots) - 1)
        k = (duration + 0.5 * (len(shots) - 1) + 0.3) / (total + 0.5 * (len(shots) - 1))
        for s in shots:                               # планы растягиваются пропорционально под длину озвучки
            s["len"] = round(s["len"] * k, 2)
    audio.mix(work / "voice.wav", duration, work / "mix.wav", music_gain=0.17, rain_gain=v.get("rain", 0), seed=v.get("music_seed", 1))

    ov = []
    reveal_at = None
    if "curtain_line" in v:
        reveal_at = timings[v["curtain_line"]][0] - 0.7
    ov.append((compose.overlay(work / "brand.png", compose.brand), 0, duration))
    # заголовок сверху: хук, дальше меняется репликами с ключом "top" (и надписью-разгадкой после штор)
    tops = [(0.0, v["hook"], compose.CREAM)]
    tops += [(t0 - 0.05, ln["top"], compose.AMBER) for t0, _, ln in timings if ln.get("top")]
    if reveal_at:
        tops.append((reveal_at + 0.3, v["reveal"], compose.AMBER))   # разгадка — как только шторы поехали
    tops.sort(key=lambda x: x[0])
    for j, (t_in, text, color) in enumerate(tops):
        t_out = tops[j + 1][0] if j + 1 < len(tops) else duration
        png = compose.overlay(work / f"top_{j}.png", lambda im, t=text, c=color: compose.text_block(im, t, 200, size=76, color=c))
        ov.append((png, t_in, t_out))
    # плашки «время · город»
    badges = [(t0 - 0.05, ln["badge"]) for t0, _, ln in timings if ln.get("badge")]
    for j, (t_in, text) in enumerate(badges):
        t_out = badges[j + 1][0] if j + 1 < len(badges) else duration
        ov.append((compose.overlay(work / f"badge_{j}.png", lambda im, t=text: compose.badge(im, t)), t_in, t_out))
    last = len(timings) - 1
    for i, (t0, t1, line) in enumerate(timings[:-1]):
        png = compose.overlay(work / f"cap_{i}.png", lambda im, c=line["cap"], w=line.get("who"): compose.caption(im, c, w))
        ov.append((png, t0 - 0.05, timings[i + 1][0] - 0.12))
    # финал вместо субтитра — карточка «куда идти»
    def cta(im):
        y = compose.text_block(im, "Окно дня — в Telegram", 1420, size=66, color=compose.AMBER)
        compose.text_block(im, "t.me/oknodnya", y + 44, size=58)
    ov.append((compose.overlay(work / "cta.png", cta), timings[last][0] - 0.05, duration))

    spec = {"shots": shots, "overlays": ov}
    if reveal_at:
        spec["curtain"] = {"open_at": reveal_at}
    mp4 = out_dir / f"{v['id']}.mp4"
    compose.render(spec, clips, work, work / "mix.wav", duration, mp4)
    (out_dir / "post.txt").write_text(v["post"], encoding="utf-8")
    # обложка — кадр с хуком на 1-й секунде
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", "1.2", "-i", str(mp4), "-frames:v", "1", "-q:v", "3", str(out_dir / "cover.jpg")], check=True)
    return mp4, duration, timings


def qa(mp4: Path, out_dir: Path):
    """Техническая проверка: параметры, громкость, кадры для просмотра."""
    info = compose.probe(mp4)
    r = subprocess.run(["ffmpeg", "-nostats", "-i", str(mp4), "-af", "ebur128", "-f", "null", "-"], capture_output=True, text=True)
    loud = [ln.strip() for ln in r.stderr.splitlines() if ln.strip().startswith(("I:", "LRA:", "Peak:"))][-3:]
    frames = out_dir / "qa_frames"
    frames.mkdir(exist_ok=True)
    dur = float(info["format"]["duration"])
    for t in [0.5, 2, dur * 0.35, dur * 0.6, dur * 0.8, dur - 0.5]:
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", f"{t:.2f}", "-i", str(mp4), "-frames:v", "1", "-vf", "scale=360:-1",
                        str(frames / f"t{t:05.1f}.png")], check=True)
    return {"probe": info, "loudness": loud}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("plan")
    ap.add_argument("--clips", required=True)
    ap.add_argument("--out", default=str(ROOT / "out"))
    ap.add_argument("--only", default="", help="собрать только ролик с этим id")
    a = ap.parse_args()
    plan = json.loads(Path(a.plan).read_text(encoding="utf-8"))
    for v in plan["videos"]:
        if a.only and v["id"] != a.only:
            continue
        out_dir = Path(a.out) / plan["date"] / v["id"]
        if out_dir.exists():
            shutil.rmtree(out_dir)
        out_dir.mkdir(parents=True)
        mp4, dur, _ = build_video(v, Path(a.clips), out_dir)
        rep = qa(mp4, out_dir)
        (out_dir / "qa.json").write_text(json.dumps(rep, ensure_ascii=False, indent=1), encoding="utf-8")
        size = int(rep["probe"]["format"]["size"]) / 1e6
        print(f"{v['id']}: {dur:.1f} с, {size:.1f} МБ, {rep['loudness']}")


if __name__ == "__main__":
    main()
