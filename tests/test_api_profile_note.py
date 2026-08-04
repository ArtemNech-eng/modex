"""
Причина пустого профиля обязана быть видна в ответе API, а не только в логах.

Затем здесь тест на ТЕКСТ файла, а не на вызов маршрута: поднять
всё приложение в CI значит притащить базу, токены и сеть. Главное же
здесь другое: патч в большой файл может НЕ примениться молча — так уже
было дважды, и логов сборки никто не читает. Пусть отказ будет красным
тестом с внятным текстом в комментарии PR.
"""
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]
API = ROOT / "src" / "api" / "main.py"

NEAR = 400  # символов: соседние ключи одного и того же ответа


def api_text():
    assert API.exists(), f"нет файла {API}"
    return API.read_text(encoding="utf-8")


def test_the_reason_is_reported_next_to_profiles_ready():
    src = api_text()
    assert src.count('"profiles_ready"') == 1, (
        "profiles_ready должен быть ровно один: патч ищет его как якорь"
    )
    assert '"profile_note"' in src, (
        "патч не применился: в ответе нет profile_note, то есть в проде "
        "виден только факт profiles_ready: 0 без причины"
    )

    at = src.index('"profiles_ready"')
    window = src[at:at + NEAR]
    assert '"profile_note"' in window, (
        "profile_note есть в файле, но не рядом с profiles_ready — скорее всего "
        "он попал в другой ответ, а не в volume-scan"
    )


def test_the_absent_attribute_cannot_break_the_route():
    """Атрибут ставит фоновая задача; до её первого круга его нет."""
    src = api_text()
    at = src.index('"profile_note"')
    line_end = src.index("\n", at)
    line = src[at:line_end]
    assert "getattr(" in line and "None" in line, (
        "profile_note должен браться через getattr(..., None): прямое обращение "
        "уронит маршрут, пока задача не сделала первый круг"
    )
    assert line.rstrip().endswith(","), "ключ в словаре обязан заканчиваться запятой"
