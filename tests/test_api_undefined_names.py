"""
Необъявленные имена в маршрутах API — статическая проверка.

ЗАЧЕМ. 02.08 маршрут /api/levels час отдавал на проде голую пятисотку. Причина —
одна строка: он обращался к CURRENT, который в этом модуле импортируется ЛОКАЛЬНО
внутри каждой функции. NameError.

Почему это не поймали. Функция чтения из базы была проверена отдельно и работала;
проверялись КУСКИ, из которых собран маршрут, и все куски были исправны. А сам
маршрут не вызывал никто: в песочнице Python 3.9, а модуль API использует запись
`datetime | None` из 3.10 — приложение здесь просто не импортируется. То есть ни
один тест в репозитории физически не может дёрнуть маршрут.

Отсюда проверка БЕЗ импорта: разбор синтаксического дерева. Она ловит имя,
которое читается внутри функции, но нигде не связано — ни аргументом, ни
присваиванием, ни импортом, ни на уровне модуля.

Проверка НАМЕРЕННО снисходительна. Любое имя, связанное где угодно внутри
функции, считается доступным; вложенные области не разбираются. Ложная тревога
здесь дороже пропуска: она заставит спорить с инструментом вместо работы.
"""
import ast
import builtins
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
FILES = ["src/api/main.py", "main.py"]

BUILTINS = set(dir(builtins)) | {"__file__", "__name__", "__doc__"}


def _module_names(tree: ast.AST) -> set:
    """Имена, доступные на уровне модуля."""
    out = set()
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for a in node.names:
                out.add((a.asname or a.name).split(".")[0])
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            out.add(node.name)
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                out.update(_targets(t))
        elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
            out.update(_targets(node.target))
        elif isinstance(node, (ast.If, ast.Try, ast.With)):
            # Импорты и присваивания под условием тоже дают имена модуля.
            out.update(_module_names(node))
    return out


def _targets(node: ast.AST) -> set:
    if isinstance(node, ast.Name):
        return {node.id}
    if isinstance(node, (ast.Tuple, ast.List)):
        out = set()
        for e in node.elts:
            out |= _targets(e)
        return out
    return set()


def _bound_inside(fn: ast.AST) -> set:
    """Всё, что связывается где угодно внутри функции."""
    out = set()
    args = fn.args
    for a in (list(args.args) + list(args.posonlyargs) + list(args.kwonlyargs)):
        out.add(a.arg)
    for a in (args.vararg, args.kwarg):
        if a:
            out.add(a.arg)
    for node in ast.walk(fn):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for a in node.names:
                out.add((a.asname or a.name).split(".")[0])
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                out |= _targets(t)
        elif isinstance(node, (ast.AnnAssign, ast.AugAssign, ast.NamedExpr)):
            out |= _targets(node.target)
        elif isinstance(node, (ast.For, ast.AsyncFor, ast.comprehension)):
            out |= _targets(node.target)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            out.add(node.name)
        elif isinstance(node, ast.withitem) and node.optional_vars is not None:
            out |= _targets(node.optional_vars)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            out.add(node.name)
            if node is not fn:
                out |= _bound_inside(node) if not isinstance(node, ast.ClassDef) else set()
        elif isinstance(node, ast.Global):
            out.update(node.names)
        elif isinstance(node, ast.Lambda):
            for a in list(node.args.args) + list(node.args.kwonlyargs):
                out.add(a.arg)
    return out


def _children(node: ast.AST) -> list:
    """Функции, вложенные непосредственно в этот узел."""
    out = []
    for child in ast.iter_child_nodes(node):
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
            out.append(child)
        elif not isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
            out.extend(_children(child))
    return out


def _own_names(fn: ast.AST) -> set:
    """Имена, читаемые непосредственно в теле функции, без вложенных функций."""
    out = []
    stack = list(ast.iter_child_nodes(fn))
    while stack:
        node = stack.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue                      # у вложенной своя область
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            out.append((node.id, node.lineno))
        stack.extend(ast.iter_child_nodes(node))
    return out


def undefined_names(path: pathlib.Path) -> list:
    tree = ast.parse(path.read_text())
    mod = _module_names(tree)
    bad = []

    def walk(fn, inherited):
        # ЗАМЫКАНИЯ: вложенная функция видит локальные имена внешней. Без этого
        # проверка кричала бы на любую внутреннюю функцию, а спорить с
        # инструментом дороже, чем его не иметь.
        known = inherited | _bound_inside(fn)
        for name, line in _own_names(fn):
            if name not in known:
                bad.append((fn.name, name, line))
        for child in _children(fn):
            walk(child, known)

    base = mod | BUILTINS
    for fn in _children(tree):
        walk(fn, base)
    return bad


@pytest.mark.parametrize("rel", FILES)
def test_no_undefined_names(rel):
    """
    Имя, которое читается, но нигде не связано, — это пятисотка в бою. Именно так
    NameError в /api/levels доехал до прода и держался там час.
    """
    bad = undefined_names(ROOT / rel)
    assert not bad, "необъявленные имена: " + "; ".join(
        f"{fn}:{name} (строка {ln})" for fn, name, ln in sorted(bad))


def test_the_checker_actually_catches_the_bug(tmp_path):
    """
    Проверка проверки. Без неё легко получить инструмент, который молчит всегда —
    и тогда зелёный тест хуже отсутствующего.

    Здесь воспроизведён ровно тот случай: CURRENT берётся из локального импорта в
    одной функции и используется без импорта в другой.
    """
    src = tmp_path / "sample.py"
    src.write_text(
        "async def ok():\n"
        "    from src.collector.stream import CURRENT\n"
        "    return CURRENT.lots\n"
        "\n"
        "async def broken():\n"
        "    return CURRENT.lots\n"
    )
    bad = undefined_names(src)
    assert [(fn, n) for fn, n, _ in bad] == [("broken", "CURRENT")]


def test_the_checker_does_not_cry_wolf(tmp_path):
    """
    Ложная тревога дороже пропуска: она заставляет спорить с инструментом вместо
    работы. Обычные связывания именем считаться не должны.
    """
    src = tmp_path / "fine.py"
    src.write_text(
        "import os\n"
        "GLOBAL = 1\n"
        "def f(a, *args, **kw):\n"
        "    b = a + GLOBAL\n"
        "    for i in range(3):\n"
        "        b += i\n"
        "    xs = [y * 2 for y in args]\n"
        "    with open(os.devnull) as fh:\n"
        "        fh.read()\n"
        "    try:\n"
        "        pass\n"
        "    except ValueError as e:\n"
        "        print(e)\n"
        "    def inner(z):\n"
        "        return z + b\n"
        "    return inner(len(xs)) + len(kw)\n"
    )
    assert undefined_names(src) == []
