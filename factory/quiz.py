"""Загадка «Какой город за шторой?» в канале: ролик без ответа → опрос-викторина под ним → вечером разгадка.

Ролик-квиз в сценарии — тот, у кого есть "curtain_line" и "reveal" (ответ в «reveal»: «Ярославль!»).
Для канала ответ из ролика вырезается: реплики с curtain_line и дальше заменяет одна — «варианты в опросе».
Варианты можно задать в сценарии: "quiz": {"options": ["Ярославль", "Кострома", "Тверь", "Нижний Новгород"]}
(первый — правильный); иначе к ответу добавляются три других окна сайта, детерминированно от даты.
Чистые функции, без сети — тестируются в tests/test_quiz.py.
"""
import random
from datetime import datetime, timedelta

NOT_DECOY = {"doroga", "samolet", "transsib", "bam", "fontanka"}  # не города (или второе окно того же города)
REVEAL_AT = (21, 0)        # разгадка вечером…
MIN_OPEN = timedelta(hours=3)  # …но не раньше, чем через 3 часа после ролика
LATEST = (23, 30)          # позже — уже утром следующего дня
MORNING = (9, 0)
TEASER = "А вы как думаете? Варианты — в опросе под роликом, ответ вечером."


def answer_name(v):
    return v["reveal"].strip().rstrip("!?.").strip()


def is_quiz(v):
    return "curtain_line" in v and bool(v.get("reveal"))


def make_quiz(v, cities, day):
    """→ {"answer", "city", "options", "correct"} или None, если ролик не загадка или ответа нет среди окон."""
    if not is_quiz(v):
        return None
    ans = answer_name(v)
    by_name = {c["name"]: c for c in cities}
    city = by_name.get(ans)
    if not city:
        return None
    opts = list((v.get("quiz") or {}).get("options") or [])
    if opts:
        if opts[0] != ans or len(set(opts)) != len(opts) or not 2 <= len(opts) <= 10:
            raise ValueError(f"{v['id']}: quiz.options — первым правильный ответ «{ans}», без повторов, 2–10 штук")
    else:
        pool = sorted(c["name"] for c in cities if c.get("enabled") and not c.get("hidden")
                      and c["id"] not in NOT_DECOY and c["name"] != ans)
        opts = [ans] + random.Random(f"{day}:{v['id']}").sample(pool, min(3, len(pool)))
    order = opts[:]
    random.Random(f"{day}:{v['id']}:order").shuffle(order)
    return {"answer": ans, "city": city["id"], "options": order, "correct": order.index(ans)}


def teaser(v):
    """Сценарий ролика для канала: без разгадки — шторы не открываются, ответ не звучит."""
    lines = v["lines"][: v["curtain_line"]] + [{"say": TEASER, "cap": TEASER, "pause": 0.4}]
    out = {k: val for k, val in v.items() if k not in ("curtain_line", "reveal")}
    out["lines"] = lines
    return out


def post_text(post):
    return post.rstrip() + "\n\nВарианты — в опросе ниже 👇 Разгадка вечером."


def explanation(q):
    return f"{q['answer']}! Окно: lofi-goroda.ru/{q['city']}/"[:200]


def reveal_time(posted: datetime):
    """Когда раскрывать: в 21:00 того же дня (но не раньше чем через 3 ч после ролика); поздно — в 9:00 назавтра."""
    t = max(posted.replace(hour=REVEAL_AT[0], minute=REVEAL_AT[1], second=0, microsecond=0), posted + MIN_OPEN)
    if (t.hour, t.minute) > LATEST or t.date() > posted.date():
        nxt = posted.date() + timedelta(days=1)
        t = max(t, datetime(nxt.year, nxt.month, nxt.day, *MORNING))
    return t


def reveal_text(q, poll):
    """Пост-разгадка ответом на опрос. poll — объект Poll из stopPoll."""
    total = poll.get("total_voter_count", 0)
    right = poll["options"][q["correct"]].get("voter_count", 0) if total else 0
    if not total:
        score = "В этот раз никто не рискнул — завтра новая загадка."
    elif right == total:
        score = f"Угадали все {total} — сильны!" if total > 1 else "Угадал единственный голосовавший — браво!"
    else:
        score = f"Угадали {right} из {total}."
    return f"Разгадка: {q['answer']}! {score}\n\nПосидеть у этого окна: lofi-goroda.ru/{q['city']}/"
