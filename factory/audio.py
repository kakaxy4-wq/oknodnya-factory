"""Звук фабрики: голос, лофи-подложка и шум дождя — синтез кодом, без чужих треков.

Голос: ElevenLabs (живые голоса), если в /etc/oknodnya.env задан OKNO_ELEVEN_KEY; иначе — RHVoice (свободный, но
«робот»). Озвученные реплики кэшируются в voice-cache/ — пересборка того же ролика символы не тратит.
"""
import hashlib
import json
import os
import subprocess
import urllib.request
import wave
from pathlib import Path

import numpy as np

SR = 44100


def _read_wav(path):
    with wave.open(str(path)) as w:
        sr, n, ch = w.getframerate(), w.getnframes(), w.getnchannels()
        x = np.frombuffer(w.readframes(n), dtype=np.int16).astype(np.float32) / 32768
    if ch > 1:
        x = x.reshape(-1, ch).mean(axis=1)
    if sr != SR:                                     # линейная передискретизация до 44,1 кГц
        t = np.arange(int(len(x) * SR / sr)) * sr / SR
        x = np.interp(t, np.arange(len(x)), x)
    return x


def write_wav(path, x):
    x = np.clip(x, -1, 1)
    data = (x * 32767).astype(np.int16)
    if data.ndim == 1:
        data = np.stack([data, data], axis=1)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(data.tobytes())


def _trim_silence(x, thr=0.01):
    idx = np.where(np.abs(x) > thr)[0]
    if not len(idx):
        return x
    return x[max(0, idx[0] - 800): idx[-1] + 2000]


# Голоса сценариев (имена из RHVoice) → голоса ElevenLabs: женский — Marusya G (ей же озвучены заставки радио),
# мужской — Maxim, спокойный разговорный. Переопределить: OKNO_ELEVEN_VOICES="anna=ID,aleksandr-hq=ID"
ELEVEN_VOICES = {"anna": "6gs4Nc3jAINHpczl2vtf", "aleksandr-hq": "6YjFofyVXU1ASguf2hTJ"}
ELEVEN_MODEL = os.environ.get("OKNO_ELEVEN_MODEL", "eleven_multilingual_v2")
CACHE = Path(__file__).resolve().parent.parent / "voice-cache"


def _eleven_voice(name):
    over = dict(kv.split("=", 1) for kv in os.environ.get("OKNO_ELEVEN_VOICES", "").split(",") if "=" in kv)
    return over.get(name) or ELEVEN_VOICES.get(name) or ELEVEN_VOICES["aleksandr-hq"]


def _eleven(text, name, out_wav):
    """Реплика голосом ElevenLabs → wav; повтор той же фразы тем же голосом берётся из кэша."""
    vid = _eleven_voice(name)
    key = hashlib.sha256(f"{vid}|{ELEVEN_MODEL}|{text}".encode()).hexdigest()[:20]
    mp3 = CACHE / f"{key}.mp3"
    if not mp3.exists():
        body = json.dumps({"text": text, "model_id": ELEVEN_MODEL,
                           "voice_settings": {"stability": 0.45, "similarity_boost": 0.8, "style": 0.25}}).encode()
        req = urllib.request.Request(f"https://api.elevenlabs.io/v1/text-to-speech/{vid}?output_format=mp3_44100_128",
                                     data=body, headers={"xi-api-key": os.environ["OKNO_ELEVEN_KEY"],
                                                         "Content-Type": "application/json", "Accept": "audio/mpeg"})
        with urllib.request.urlopen(req, timeout=120) as r:
            data = r.read()
        CACHE.mkdir(exist_ok=True)
        mp3.write_bytes(data)
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(mp3), "-ac", "1", "-ar", str(SR), str(out_wav)], check=True)


def voice(lines, out_wav, tmp: Path, profile="aleksandr-hq", rate=None):
    """Озвучить реплики по одной; вернуть тайминги [(start, end, line)] для субтитров."""
    tmp.mkdir(parents=True, exist_ok=True)
    chunks, timings, t = [np.zeros(int(0.25 * SR))], [], 0.25
    for i, line in enumerate(lines):
        p = tmp / f"vo_{i}.wav"
        name = line.get("voice", profile)   # у реплики может быть свой голос
        try:
            if not os.environ.get("OKNO_ELEVEN_KEY"):
                raise RuntimeError("нет OKNO_ELEVEN_KEY")
            _eleven(line["say"], name, p)
        except Exception as e:  # noqa: BLE001 — нет ключа, кончились символы, сеть: ролик всё равно соберётся
            if os.environ.get("OKNO_ELEVEN_KEY"):
                print("ElevenLabs недоступен, озвучка RHVoice:", e)
            args = ["RHVoice-test", "-p", name, "-o", str(p)]
            if rate:
                args += ["-r", str(rate)]
            subprocess.run(args, input=line["say"].encode(), check=True, capture_output=True)
        x = _trim_silence(_read_wav(p))
        x = x / (np.abs(x).max() + 1e-9) * 0.9
        timings.append((t, t + len(x) / SR, line))
        pause = line.get("pause", 0.35)
        chunks += [x, np.zeros(int(pause * SR))]
        t += len(x) / SR + pause
    write_wav(out_wav, np.concatenate(chunks))
    return timings, t


def _lowpass(x, cutoff):
    a = np.exp(-2 * np.pi * cutoff / SR)
    y = np.empty_like(x)
    acc = 0.0
    for i in range(len(x)):                         # однополюсный ФНЧ (векторизация ниже для длинных сигналов)
        acc = (1 - a) * x[i] + a * acc
        y[i] = acc
    return y


def _lowpass_fast(x, cutoff):
    from scipy.signal import lfilter  # noqa: WPS433 — если scipy есть, быстрее
    a = np.exp(-2 * np.pi * cutoff / SR)
    return lfilter([1 - a], [1, -a], x)


def lowpass(x, cutoff):
    try:
        return _lowpass_fast(x, cutoff)
    except ImportError:
        return _lowpass(x, cutoff)


NOTE = {"C": 0, "D": 2, "E": 4, "F": 5, "G": 7, "A": 9, "B": 11}


def _hz(name, octave):
    return 440 * 2 ** ((NOTE[name[0]] + (1 if "#" in name else 0) - 9) / 12 + (octave - 4))


# Fmaj7 — Em7 — Dm7 — Cmaj7: мягкая лофи-гармония
CHORDS = [[("F", 3), ("A", 3), ("C", 4), ("E", 4)], [("E", 3), ("G", 3), ("B", 3), ("D", 4)],
          [("D", 3), ("F", 3), ("A", 3), ("C", 4)], [("C", 3), ("E", 3), ("G", 3), ("B", 3)]]


def lofi(duration, seed=1, bpm=72):
    """Лофи-подложка: электропиано, бас, мягкий бит, винил. Всё синтезируется кодом."""
    rng = np.random.default_rng(seed)
    n = int(duration * SR)
    out = np.zeros(n)
    beat = 60 / bpm
    bar = beat * 4
    t_local = np.arange(int(bar * SR)) / SR
    for k in range(int(duration / bar) + 1):
        start = int(k * bar * SR)
        chord = CHORDS[k % 4]
        seg = np.zeros(len(t_local))
        for j, (nm, octv) in enumerate(chord):
            f = _hz(nm, octv) * (1 + rng.uniform(-0.002, 0.002))
            on = j * 0.03                              # лёгкое «арпеджио» при взятии
            tt = np.clip(t_local - on, 0, None)
            env = np.exp(-tt * 0.9) * (t_local >= on)
            tone = np.sin(2 * np.pi * f * tt) + 0.25 * np.sin(4 * np.pi * f * tt) + 0.08 * np.sin(6 * np.pi * f * tt)
            seg += 0.12 * env * tone * (1 + 0.15 * np.sin(2 * np.pi * 4.5 * tt))
        root = _hz(chord[0][0], 2)
        for b in (0, 2):                                # бас на 1 и 3 долю
            tt = np.clip(t_local - b * beat, 0, None)
            seg += 0.18 * np.sin(2 * np.pi * root * tt) * np.exp(-tt * 2.2) * (t_local >= b * beat)
        for b in range(4):                              # бочка, «щётка», хэт
            tt = np.clip(t_local - b * beat, 0, None)
            m = t_local >= b * beat
            if b in (0, 2):
                seg += 0.35 * np.sin(2 * np.pi * (50 + 60 * np.exp(-tt * 30)) * tt) * np.exp(-tt * 9) * m
            else:
                seg += 0.06 * rng.standard_normal(len(tt)) * np.exp(-tt * 18) * m
            for half in (0, 0.5):
                th = np.clip(t_local - (b + half) * beat, 0, None)
                seg += 0.015 * rng.standard_normal(len(th)) * np.exp(-th * 60) * (t_local >= (b + half) * beat)
        end = min(n, start + len(seg))
        if start < n:
            out[start:end] += seg[: end - start]
    out = lowpass(out, 3200)                            # «тёплый», как с пластинки
    crackle = np.zeros(n)
    pos = rng.integers(0, n, int(duration * 7))
    crackle[pos] = rng.uniform(-0.25, 0.25, len(pos))
    out += lowpass(crackle, 5000) + 0.004 * lowpass(rng.standard_normal(n), 1500)
    fade = int(1.0 * SR)
    out[:fade] *= np.linspace(0, 1, fade)
    out[-fade:] *= np.linspace(1, 0, fade)
    return out / (np.abs(out).max() + 1e-9) * 0.8


def rain(duration, seed=2):
    rng = np.random.default_rng(seed)
    n = int(duration * SR)
    x = lowpass(rng.standard_normal(n), 2500) * 0.6
    drops = np.zeros(n)
    pos = rng.integers(0, n, int(duration * 40))
    drops[pos] = rng.uniform(0.2, 0.6, len(pos))
    x += lowpass(drops, 4000)
    return x / (np.abs(x).max() + 1e-9) * 0.6


def mix(voice_wav, duration, out_wav, music_gain=0.16, rain_gain=0.0, seed=1):
    v = _read_wav(voice_wav)
    n = int(duration * SR)
    v = np.pad(v, (0, max(0, n - len(v))))[:n]
    m = lofi(duration, seed=seed)[:n]
    env = lowpass(np.abs(v), 4)                         # «приглушение» музыки под голос
    duck = 1 - 0.55 * np.clip(env / (env.max() + 1e-9) * 3, 0, 1)
    x = v + music_gain * m * duck
    if rain_gain:
        x += rain_gain * rain(duration)[:n]
    write_wav(out_wav, x / (np.abs(x).max() + 1e-9) * 0.9)
