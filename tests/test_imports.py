"""Каждый модуль должен компилироваться и импортироваться.

Запуск: python3 tests/test_imports.py

Нужен потому, что остальные тесты местами проверяют ИСХОДНЫЙ ТЕКСТ файла
(например, что нужное поле попало в строку batch), а не импортируемость. Из-за
этого синтаксическая ошибка в claude_agent.py прошла весь набор из 172 тестов
незамеченной: текст на месте, а модуль не грузится.
"""

import os
import py_compile
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

SKIP_DIRS = {"__pycache__", ".git", "venv", ".venv", "node_modules"}


def _py_files():
    for base in ("src", "config", "scripts"):
        root = os.path.join(ROOT, base)
        if not os.path.isdir(root):
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
            for fn in filenames:
                if fn.endswith(".py"):
                    yield os.path.join(dirpath, fn)


def test_every_module_compiles():
    import tempfile
    broken = []
    with tempfile.TemporaryDirectory() as tmp:
        for i, path in enumerate(_py_files()):
            try:
                py_compile.compile(path, doraise=True,
                                   cfile=os.path.join(tmp, f"c{i}.pyc"))
            except py_compile.PyCompileError as e:
                broken.append(f"{os.path.relpath(path, ROOT)}: "
                              f"{str(e).splitlines()[-1][:120]}")
    assert not broken, "не компилируется:\n  " + "\n  ".join(broken)


def test_main_modules_import():
    """Ключевые модули должны реально грузиться, а не только компилироваться.

    Отсутствие СТОРОННЕЙ библиотеки в песочнице — не ошибка кода, такие модули
    пропускаем. Ошибка в НАШЕМ коде (синтаксис, опечатка в имени, битый импорт
    внутри проекта) валит тест.
    """
    import importlib
    broken, skipped = [], []
    for mod in ("src.db", "src.risk.engine", "src.analysis.technical",
                "src.analysis.intraday", "src.agent.claude_agent",
                "src.agent.screen", "src.agent.intraday_analyst",
                "src.agent.external_signal", "src.collector.moex_price_collector",
                "config.settings"):
        try:
            importlib.import_module(mod)
        except ModuleNotFoundError as e:
            missing = (e.name or "")
            if missing.split(".")[0] in ("src", "config"):
                broken.append(f"{mod}: не найден наш модуль {missing}")
            else:
                skipped.append(f"{mod} (нет библиотеки {missing})")
        except Exception as e:                       # noqa: BLE001
            broken.append(f"{mod}: {type(e).__name__}: {str(e)[:100]}")
    assert not broken, "не импортируется:\n  " + "\n  ".join(broken)
    if skipped:
        print("        пропущены из-за отсутствующих библиотек: "
              + ", ".join(skipped))


if __name__ == "__main__":
    tests = [(n, o) for n, o in sorted(globals().items())
             if n.startswith("test_") and callable(o)]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"  ok    {name}")
        except AssertionError as e:
            failed += 1
            print(f"  ПАДАЕТ {name}: {e}")
        except Exception as e:                       # noqa: BLE001
            failed += 1
            print(f"  ОШИБКА {name}: {type(e).__name__}: {e}")
    print(f"\n{len(tests) - failed} из {len(tests)} пройдено")
    sys.exit(1 if failed else 0)
