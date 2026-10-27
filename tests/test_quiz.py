"""python -m pytest tests/test_quiz.py — загадка в канале: варианты, ролик без ответа, время и текст разгадки."""
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from factory import quiz  # noqa: E402

CITIES = [{"id": i, "name": n, "enabled": True} for i, n in [
    ("yaroslavl", "Ярославль"), ("kazan", "Казань"), ("vologda", "Вологда"), ("sochi", "Сочи"),
    ("doroga", "Марсоход"), ("fontanka", "Фонтанка")]] + [{"id": "samolet", "name": "Самолёт", "enabled": True, "hidden": True}]
V = {"id": "2-spor-yaroslavl", "reveal": "Ярославль!", "curtain_line": 2, "post": "Спорим?",
     "lines": [{"say": "Спорим?"}, {"say": "Подсказка"}, {"say": "Ярославль!"}, {"say": "Эх."}]}


def test_make_quiz_defaults():
    q = quiz.make_quiz(V, CITIES, "2026-10-28")
    assert q["answer"] == "Ярославль" and q["city"] == "yaroslavl"
    assert len(q["options"]) == 4 and q["options"][q["correct"]] == "Ярославль"
    assert not {"Марсоход", "Фонтанка", "Самолёт"} & set(q["options"])  # не города и скрытые — не варианты
    assert q == quiz.make_quiz(V, CITIES, "2026-10-28")  # детерминированно


def test_make_quiz_explicit_and_not_quiz():
    q = quiz.make_quiz({**V, "quiz": {"options": ["Ярославль", "Кострома"]}}, CITIES, "2026-10-28")
    assert sorted(q["options"]) == ["Кострома", "Ярославль"]
    assert quiz.make_quiz({"id": "1-pov", "lines": []}, CITIES, "d") is None
    assert quiz.make_quiz({**V, "reveal": "Атлантида!"}, CITIES, "d") is None
    try:
        quiz.make_quiz({**V, "quiz": {"options": ["Кострома", "Ярославль"]}}, CITIES, "d")
        assert False
    except ValueError:
        pass


def test_teaser_cuts_answer():
    t = quiz.teaser(V)
    says = [ln["say"] for ln in t["lines"]]
    assert "Ярославль!" not in says and says[-1] == quiz.TEASER and len(says) == 3
    assert "curtain_line" not in t and "reveal" not in t and V["curtain_line"] == 2  # исходник не тронут


def test_reveal_time():
    assert quiz.reveal_time(datetime(2026, 10, 28, 14, 0)) == datetime(2026, 10, 28, 21, 0)
    assert quiz.reveal_time(datetime(2026, 10, 28, 19, 0)) == datetime(2026, 10, 28, 22, 0)
    assert quiz.reveal_time(datetime(2026, 10, 28, 21, 10)) == datetime(2026, 10, 29, 9, 0)
    assert quiz.reveal_time(datetime(2026, 10, 28, 23, 20)) == datetime(2026, 10, 29, 9, 0)


def test_reveal_text():
    q = {"answer": "Ярославль", "city": "yaroslavl", "options": ["Казань", "Ярославль"], "correct": 1}
    t = quiz.reveal_text(q, {"total_voter_count": 5, "options": [{"voter_count": 2}, {"voter_count": 3}]})
    assert t.startswith("Разгадка: Ярославль! Угадали 3 из 5.") and "lofi-goroda.ru/yaroslavl/" in t
    assert "никто" in quiz.reveal_text(q, {"total_voter_count": 0, "options": [{}, {}]})
    assert "все 4" in quiz.reveal_text(q, {"total_voter_count": 4, "options": [{"voter_count": 0}, {"voter_count": 4}]})
    assert len(quiz.explanation(q)) <= 200
