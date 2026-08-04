"""
Одноразовый патч: три правки по ревью + удаление самого себя.

ЗАЧЕМ ТАК. Агент правит репозиторий через GitHub API, а там нет операции
«заменить строку»: файл кладётся ЦЕЛИКОМ. Для tinkoff_client.py и settings.py
это значит перепечатать тысячи строк ради трёх правок. Одна опечатка в
конфиге счёта, на котором торгуют живыми деньгами, стоит дороже любого
удобства, поэтому замены описаны явно и выполняются рядом с файлами.

Падает ГРОМКО, если якорь не найден. Тихо неприменённая правка хуже красного
CI: ровно так месяц жила рукописная таблица FIGI с 22 чужими бумагами.

Идемпотентен: если правка уже стоит, он просто ничего не делает.
"""
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
FAILED = []


def must(cond: bool, msg: str) -> None:
    if not cond:
        FAILED.append(msg)


def edit(rel: str, fn) -> None:
    p = ROOT / rel
    if not p.exists():
        FAILED.append(f"нет файла {rel}")
        return
    src = p.read_text(encoding="utf-8")
    out = fn(src)
    if out is None or out == src:
        print(f"= {rel}: изменений не требуется")
        return
    p.write_text(out, encoding="utf-8")
    print(f"+ {rel}: изменён")


# ── 1. клиент: общая чистка токена + приоритет борда ───────────────────────────

IMPORT_NOTE = (
    "# Чистка токена живёт В НЕЙТРАЛЬНОМ модуле, а не здесь. Причина не в красоте:\n"
    "# токен уходит в сеть ДВУМЯ независимыми путями — REST отсюда и gRPC из\n"
    "# MarketStream, который берёт значение прямо из config.settings. Пока функция\n"
    "# лежала здесь, она чинила только первый путь, и значение в кавычках давало\n"
    "# самую неприятную картину: health зелёный, реалтайм мёртв.\n"
    "from config.token import clean_token\n"
)

ALIAS_NOTE = (
    "# Имя сохранено псевдонимом: на него ссылаются тесты, а ломать имена ради\n"
    "# переезда функции незачем. Реализация теперь ровно ОДНА на весь проект,\n"
    "# и тест сторожит именно тождество, а не схожесть поведения.\n"
    "_clean_token = clean_token\n"
)

OLD_KEY = ('        key=lambda i: (bool(i.get("apiTradeAvailableFlag")),\n'
           '                       str(i.get("classCode") or "").upper().startswith("TQ")),\n')
NEW_KEY = ('        key=lambda i: (str(i.get("classCode") or "").upper().startswith("TQ"),\n'
           '                       bool(i.get("apiTradeAvailableFlag"))),\n')

BOARD_NOTE = (
    "    ПОРЯДОК ПРЕДПОЧТЕНИЙ, если совпадений несколько: сначала московский борд\n"
    "    TQ*, и только потом apiTradeAvailableFlag. Ключ был расставлен наоборот, и\n"
    "    при reverse=True флаг перевешивал борд: внебиржевая бумага с флагом\n"
    "    обходила TQBR без флага. Мы торгуем Мосбиржу, и чужой инструмент даст\n"
    "    свои свечи и свой стакан — ровно та же тихая подмена, что и HYDR с\n"
    "    данными FEES, только не в таблице, а в порядке сортировки. Флаг не\n"
    "    отменён — он решает внутри одного борда.\n"
    "    \"\"\"\n"
)


def patch_client(src: str) -> str:
    # а) импорт общего модуля
    if "from config.token import clean_token" not in src:
        anchor = "import httpx\n\nlogger = logging.getLogger(__name__)"
        must(anchor in src, "tinkoff_client: не найден якорь импортов")
        if anchor in src:
            src = src.replace(
                anchor,
                "import httpx\n\n" + IMPORT_NOTE + "\nlogger = logging.getLogger(__name__)",
                1)

    # б) свою копию функции заменяем псевдонимом — резать по границам
    #    функций, а не по тексту долгого докстринга
    if "def _clean_token(" in src:
        i = src.index("def _clean_token(")
        must("def _is_share(" in src, "tinkoff_client: не найдена граница _is_share")
        if "def _is_share(" in src:
            j = src.index("def _is_share(")
            must(i < j, "tinkoff_client: порядок функций изменился")
            if i < j:
                src = src[:i] + ALIAS_NOTE + "\n\n" + src[j:]

    src = src.replace("self.token = _clean_token(token or os.getenv",
                      "self.token = clean_token(token or os.getenv")

    # в) приоритет борда выше флага
    if NEW_KEY not in src:
        must(OLD_KEY in src, "tinkoff_client: не найден ключ сортировки _pick_share")
        if OLD_KEY in src:
            src = src.replace(OLD_KEY, NEW_KEY, 1)

    # г) докстринг _pick_share говорит про порядок явно
    old_tail = ('    (одна бумага на разных бордах), предпочитаем торгуемую по API и московский\n'
                '    борд TQ*.\n'
                '    """\n')
    if old_tail in src:
        src = src.replace(old_tail, BOARD_NOTE, 1)
    return src


# ── 2. конфиг: тот же clean_token, что и у клиента ──────────────────────────────

SETTINGS_OLD = 'TINKOFF_TOKEN = os.getenv("TINKOFF_TOKEN", "").strip()'
SETTINGS_NEW = (
    "# Та же чистка, что и у TinkoffClient, именно одна и та же функция.\n"
    "# Отсюда токен уходит в MarketStream, а тот собирает gRPC-метаданные из\n"
    "# self.token НАПРЯМУЮ, мимо клиента. Голый .strip() снимал перевод строки,\n"
    "# но НЕ кавычки, и значение вида \"t.xxx\" давало самую неприятную картину:\n"
    "# REST чинился сам, /api/health/figi светил зелёным, а стрим шлёл Bearer\n"
    "# с кавычками и молча не получал данных — live 0 из 48 при «исправном» health.\n"
    "from config.token import clean_token\n"
    'TINKOFF_TOKEN = clean_token(os.getenv("TINKOFF_TOKEN", ""))'
)


def patch_settings(src: str) -> str:
    if 'clean_token(os.getenv("TINKOFF_TOKEN"' in src:
        return src
    must(SETTINGS_OLD in src, "settings: не найдена строка TINKOFF_TOKEN")
    if SETTINGS_OLD not in src:
        return src
    return src.replace(SETTINGS_OLD, SETTINGS_NEW, 1)


# ── 3. тесты: новый файл попадает в прогон + проверка конфига ────────────────

SUITES_OLD = "tests/test_volume_events.py tests/test_figi_failure_reason.py"
SUITES_NEW = ("tests/test_volume_events.py tests/test_figi_failure_reason.py "
              "tests/test_token_and_board.py")

SETTINGS_TEST = '''

def test_settings_uses_the_very_same_cleaner():
    """
    Главный тест всего коммита: токен чистится ОДИНАКОВО на двух путях.

    Проверка по ИСХОДНИКУ, а не по импорту, сознательно: config.settings тянет
    десятки переменных окружения и dotenv, и перезагружать его внутри теста
    значит менять глобальное состояние под остальными 943 тестами.
    В этом репозитории такая проверка уже принята (см. test_volume_events).
    """
    src = (ROOT / "config" / "settings.py").read_text(encoding="utf-8")
    assert "from config.token import clean_token" in src, \\
        "конфиг не берёт общую чистку"
    assert 'clean_token(os.getenv("TINKOFF_TOKEN"' in src, \\
        "токен в конфиге всё ещё чистится голым .strip()"
    assert '.strip()' not in src.split("TINKOFF_TOKEN")[1][:80], \\
        "рядом с TINKOFF_TOKEN остался старый .strip()"
'''


def patch_workflow(src: str) -> str:
    if "tests/test_token_and_board.py" in src:
        return src
    must(SUITES_OLD in src, "tests.yml: не найден список сюитов")
    return src.replace(SUITES_OLD, SUITES_NEW, 1)


def patch_tests(src: str) -> str:
    if "test_settings_uses_the_very_same_cleaner" in src:
        return src
    return src.rstrip("\n") + "\n" + SETTINGS_TEST


def main() -> int:
    edit("src/collector/tinkoff_client.py", patch_client)
    edit("config/settings.py", patch_settings)
    edit(".github/workflows/tests.yml", patch_workflow)
    edit("tests/test_token_and_board.py", patch_tests)

    # Самоуборка: патч одноразовый, и оставлять его в репозитории значит держать
    # воркфлоу, который будет шуметь на каждый push.
    for junk in (".github/workflows/patch-token-board.yml",
                 "scripts/_patch_token_board.py"):
        p = ROOT / junk
        if p.exists():
            p.unlink()
            print(f"- {junk}: удалён после применения")

    if FAILED:
        print("\nНЕ ПРИМЕНЕНО:")
        for m in FAILED:
            print(" -", m)
        return 1
    print("\nвсе замены прошли")
    return 0


if __name__ == "__main__":
    sys.exit(main())
