"""Сборка вертикального ролика 1080×1920: сцена «Лофи-городов» (пиксели ×3 без размытия), заголовок, субтитры, финальная карточка."""
import json
import subprocess
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

ROOT = Path(__file__).resolve().parent.parent
FONT = str(ROOT / "assets/fonts/pangolin.ttf")
PIXEL = FONT                      # в пиксельных шрифтах проекта нет базовой кириллицы — заголовки тоже Pangolin
W, H = 1080, 1920
SCENE_Y = 560                      # сцена 360×270 ×3 = 1080×810, с y=560 до 1370
CREAM, INK, AMBER = (243, 230, 200), (24, 18, 30), (255, 196, 92)


def _font(path, size):
    return ImageFont.truetype(path, size)


def _wrap(text, font, width):
    lines, cur = [], ""
    for word in text.split():
        test = (cur + " " + word).strip()
        if font.getlength(test) <= width or not cur:
            cur = test
        else:
            lines.append(cur)
            cur = word
    return lines + ([cur] if cur else [])


def _panel(img, box, radius=36, fill=(20, 14, 28, 190)):
    layer = Image.new("RGBA", img.size)
    ImageDraw.Draw(layer).rounded_rectangle(box, radius, fill=fill)
    img.alpha_composite(layer)


def text_block(img, text, y, size=64, color=CREAM, width=920, panel=True, font=FONT, align_center=True, gap=1.18):
    f = _font(font, size)
    lines = _wrap(text, f, width)
    lh = int(size * gap)
    total = lh * len(lines)
    if panel:
        wmax = max(f.getlength(ln) for ln in lines)
        _panel(img, ((W - wmax) / 2 - 40, y - 28, (W + wmax) / 2 + 40, y + total + 18))
    d = ImageDraw.Draw(img)
    for i, ln in enumerate(lines):
        x = (W - f.getlength(ln)) / 2 if align_center else 80
        yy = y + i * lh
        d.text((x + 4, yy + 4), ln, font=f, fill=(0, 0, 0, 170))
        d.text((x, yy), ln, font=f, fill=color)
    return y + total


def caption(img, text, who=None, y=1440):
    """Субтитр; у диалога — имя говорящего янтарной плашкой над ним."""
    if who:
        f = _font(FONT, 44)
        x = (W - f.getlength(who)) / 2
        _panel(img, (x - 22, y - 92, x + f.getlength(who) + 22, y - 36), radius=18, fill=(255, 196, 92, 230))
        ImageDraw.Draw(img).text((x, y - 92), who, font=f, fill=INK)
    return text_block(img, text, y, size=62)


def badge(img, text, y=480):
    """Плашка «время · город» над сценой."""
    f = _font(FONT, 46)
    x = (W - f.getlength(text)) / 2
    _panel(img, (x - 26, y - 4, x + f.getlength(text) + 26, y + 58), radius=20, fill=(20, 14, 28, 200))
    ImageDraw.Draw(img).text((x, y - 2), text, font=f, fill=AMBER)


def overlay(path, draw_fn):
    img = Image.new("RGBA", (W, H))
    draw_fn(img)
    img.save(path)
    return path


def brand(img):
    f = _font(FONT, 40)
    t = "Лофи-города"
    x = (W - f.getlength(t)) / 2
    _panel(img, (x - 28, 86, x + f.getlength(t) + 28, 150), radius=22, fill=(20, 14, 28, 170))
    ImageDraw.Draw(img).text((x, 92), t, font=f, fill=AMBER)


def curtains(path_l, path_r):
    """Плотные пиксельные шторы (в масштабе сцены): закрывают только окно, комната остаётся видна.
    Складки — вертикальные полосы со светом и тенью, как у штор в самой сцене."""
    shade = [(104, 22, 30), (122, 28, 36), (138, 34, 42), (150, 40, 48), (138, 34, 42), (122, 28, 36), (104, 22, 30), (92, 18, 26)]
    for path, flip in ((path_l, False), (path_r, True)):
        im = Image.new("RGBA", (246, 200))
        d = ImageDraw.Draw(im)
        for x in range(246):
            d.line([(x, 0), (x, 199)], fill=shade[(x // 3) % len(shade)] + (255,))
        d.rectangle([0, 0, 245, 4], fill=(64, 16, 20, 255))            # карниз
        for y in range(186, 200):                                       # низ чуть темнее — тень от подоконника
            d.line([(0, y), (245, y)], fill=(0, 0, 0, 40 + (y - 186) * 6))
        edge = 0 if flip else 245
        d.line([(edge, 0), (edge, 199)], fill=(70, 14, 20, 255), width=2)
        (im.transpose(Image.FLIP_LEFT_RIGHT) if flip else im).save(path)


def run(args):
    r = subprocess.run(["ffmpeg", "-v", "error", "-y", *args], capture_output=True, text=True)
    if r.returncode:
        raise RuntimeError(r.stderr[-3000:])


def render(spec, clips_dir: Path, work: Path, audio_wav: Path, duration: float, out_mp4: Path):
    """spec: shots [{clip, start, len, crop_x}], overlays [(png, t0, t1)], curtain {open_at} (опционально)."""
    work.mkdir(parents=True, exist_ok=True)
    inputs, filt = [], []
    # 1) дорожка сцены 480×270 нужной длины
    for i, s in enumerate(spec["shots"]):
        inputs += ["-ss", str(s.get("start", 0)), "-t", str(s["len"]), "-i", str(clips_dir / (s["clip"] + ".webm"))]
        cx = s.get("crop_x", 60)
        filt.append(f"[{i}:v]fps=30,setpts=PTS-STARTPTS,crop=360:270:{cx}:0,setsar=1,tpad=stop_mode=clone:stop_duration=2[s{i}]")
    n = len(spec["shots"])
    if n == 1:
        filt.append("[s0]null[scene0]")
    else:  # мягкие переходы между городами
        acc, off = "s0", 0.0
        for i in range(1, n):
            off += spec["shots"][i - 1]["len"] - 0.5
            filt.append(f"[{acc}][s{i}]xfade=transition=fade:duration=0.5:offset={off:.2f}[x{i}]")
            acc = f"x{i}"
        filt.append(f"[{acc}]null[scene0]")
    k = n
    if spec.get("curtain"):
        cl, cr = work / "curtain_l.png", work / "curtain_r.png"
        curtains(cl, cr)
        t0 = spec["curtain"]["open_at"]
        inputs += ["-loop", "1", "-i", str(cl), "-loop", "1", "-i", str(cr)]
        # в кадре 360 px (x=60..420): левая половина сдвигается влево, правая — вправо за ~1,4 с
        filt.append(f"[scene0][{k}:v]overlay=x='-6-max(0,t-{t0})*190':y=6:shortest=1[c1]")
        filt.append(f"[c1][{k + 1}:v]overlay=x='120+max(0,t-{t0})*190':y=6:shortest=1[scene]")
        k += 2
    else:
        filt.append("[scene0]null[scene]")
    # 2) фон: та же сцена, размыта и затемнена; поверх — чёткая сцена ×3
    filt.append("[scene]split[a][b]")
    filt.append(f"[a]scale=-2:{H}:flags=bilinear,crop={W}:{H},boxblur=24:2,eq=brightness=-0.22:saturation=0.8[bg]")
    filt.append("[b]scale=1080:810:flags=neighbor[fg]")
    filt.append(f"[bg][fg]overlay=0:{SCENE_Y}[base]")
    cur = "base"
    for j, (png, t_in, t_out) in enumerate(spec["overlays"]):
        inputs += ["-loop", "1", "-i", str(png)]
        nxt = f"o{j}"
        filt.append(f"[{cur}][{k}:v]overlay=0:0:enable='between(t,{t_in:.2f},{t_out:.2f})':shortest=1[{nxt}]")
        cur, k = nxt, k + 1
    filt.append(f"[{cur}]trim=0:{duration:.2f},format=yuv420p[v]")
    inputs += ["-i", str(audio_wav)]
    run([*inputs, "-filter_complex", ";".join(filt), "-map", "[v]", "-map", f"{k}:a",
         "-af", "loudnorm=I=-14:TP=-1.5:LRA=11", "-t", f"{duration:.2f}",
         "-c:v", "libx264", "-preset", "medium", "-crf", "19", "-r", "30",
         "-c:a", "aac", "-b:a", "160k", "-ar", "44100", "-movflags", "+faststart", str(out_mp4)])


def probe(path):
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration,size:stream=codec_name,width,height,r_frame_rate,pix_fmt",
                        "-of", "json", str(path)], capture_output=True, text=True, check=True)
    return json.loads(r.stdout)
