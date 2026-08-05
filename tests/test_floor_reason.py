"""
Пол по обороту описывается тем, что он делает, а не чьим-то депозитом.

ЗАЧЕМ ЭТИ ТЕСТЫ СУЩЕСТВУЮТ. Порог был взят из размера позиции владельца,
и в проде 05.08 это стоило 46 бумаг из 80, замолчавших целиком. Сканер описывает
РЫНОК, а не исполнимость чьей-то заявки: у каждого свой счёт.

Тесты читают ИСХОДНИК, а не только значения: фраза-объяснение тоже часть
поведения. Именно расхождение между кодом и его описанием дважды уводило
разбор не в ту сторону.
"""
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VE = "src/analysis/volume_events.py"
API = "src/api/main.py"


def _src(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


def test_default_floor_is_fifty_thousand():
    assert 'VOLUME_FLOOR_RUB", "50000"' in _src(VE)


def test_the_floor_is_not_explained_by_someones_deposit():
    for rel in (VE, API):
        assert "позиции Артёма" not in _src(rel), rel


def test_env_still_overrides_the_default():
    assert 'os.getenv("VOLUME_FLOOR_RUB"' in _src(VE)


def test_floor_still_scales_with_step():
    assert 'p["floor"] * step' in _src(VE)


def test_the_explanation_names_the_variable_not_a_person():
    s = _src(VE)
    i = s.index("FLOOR_RUB = float(")
    window = s[max(0, i - 2000):i]
    assert "VOLUME_FLOOR_RUB" in window


def test_value_matches_the_source_when_env_is_not_set():
    if os.getenv("VOLUME_FLOOR_RUB"):
        return                       # в проде переменная задана и она главнее
    from src.analysis.volume_events import FLOOR_RUB
    assert FLOOR_RUB == 50000.0
