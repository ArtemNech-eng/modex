"""
Healthcheck не должен звать бинарник, которого нет в образе.

Ошибка, ради которой написан файл: docker-compose проверял живость командой
    ["CMD", "curl", "-f", "http://localhost:8000/api/stats"]
а Dockerfile ставил из системных пакетов только gcc и g++. В python:3.11-slim
(Debian bookworm-slim) curl отсутствует, поэтому проверка падала не потому, что
сервис мёртв, а потому что нет такого файла.

Для оркестратора разницы нет. Контейнер никогда не становится healthy, деплой
признаётся неудачным, и продолжает работать ПРЕЖНИЙ образ. Ровно эта картина
наблюдалась 30–31.07: контейнер перезапускался, а код в нём оставался старым —
четыре коммита подряд не доезжали до прода.

Тест проверяет два свойства: команда healthcheck исполнима в этом образе, и у
проверки есть период прогрева.
"""
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DOCKERFILE = ROOT / "Dockerfile"
COMPOSE = ROOT / "docker-compose.yml"

# Что даёт базовый образ python:3.11-slim само по себе. Список намеренно узкий:
# в slim нет ни curl, ни wget, ни ping, ни netcat.
BASE_IMAGE_BINARIES = {"python", "python3", "pip", "sh", "bash", "test", "cat"}


def _apt_installed() -> set:
    """
    Пакеты, которые Dockerfile ставит через apt-get install.

    Разбор построчный, а не одной регуляркой: команда разнесена по строкам
    обратными слэшами, и regex через продолжения читается хуже, чем склейка.
    """
    joined = []
    buf = ""
    for line in DOCKERFILE.read_text().splitlines():
        s = line.strip()
        if s.startswith("#"):
            continue
        if s.endswith("\\"):
            buf += s[:-1] + " "
            continue
        joined.append(buf + s)
        buf = ""
    if buf:
        joined.append(buf)

    out = set()
    for cmd in joined:
        if "apt-get install" not in cmd:
            continue
        tail = cmd.split("apt-get install", 1)[1]
        # Обрезаем всё после следующего && — там уже не список пакетов.
        tail = tail.split("&&", 1)[0]
        for tok in tail.split():
            if tok.startswith("-"):      # флаги вида -y, --no-install-recommends
                continue
            out.add(tok)
    return out


def _healthcheck_binaries() -> list:
    """Первое слово каждой команды healthcheck — и в Dockerfile, и в compose."""
    bins = []
    df = DOCKERFILE.read_text()
    m = re.search(r"HEALTHCHECK[^\n]*(?:\\\s*\n[^\n]*)*", df)
    if m:
        cmd = re.search(r"CMD\s+(.+)", m.group(0))
        if cmd:
            first = cmd.group(1).strip().strip('[').strip('"').split()[0]
            bins.append(first.strip('",'))
    cy = COMPOSE.read_text()
    m = re.search(r'test:\s*\[([^\]]+)\]', cy)
    if m:
        parts = [p.strip().strip('"').strip("'") for p in m.group(1).split(",")]
        parts = [p for p in parts if p and p.upper() != "CMD" and p.upper() != "CMD-SHELL"]
        if parts:
            bins.append(parts[0])
    return bins


def test_healthcheck_binaries_exist_in_image():
    """Каждый бинарник из healthcheck либо в базовом образе, либо ставится apt."""
    available = BASE_IMAGE_BINARIES | _apt_installed()
    found = _healthcheck_binaries()
    assert found, "healthcheck не найден ни в Dockerfile, ни в compose"
    for b in found:
        assert b in available, (
            f"healthcheck зовёт «{b}», которого нет в образе. "
            f"Ставится apt: {sorted(_apt_installed())}. "
            f"Именно так деплой молча оставался на старом образе."
        )


def test_curl_is_installed_since_healthcheck_uses_it():
    """Раз проверка на curl — curl обязан ставиться."""
    if any(b == "curl" for b in _healthcheck_binaries()):
        assert "curl" in _apt_installed()


def test_healthcheck_has_start_period():
    """
    Без start_period отсчёт retries идёт с первой секунды, и холодный старт
    (импорт цепочки модулей + setup_db) не успевает уложиться в interval x retries.
    """
    df = DOCKERFILE.read_text()
    assert "--start-period" in df, "в Dockerfile у HEALTHCHECK нет --start-period"
    cy = COMPOSE.read_text()
    assert "start_period" in cy, "в compose у healthcheck нет start_period"


def test_healthcheck_hits_a_local_route_without_external_calls():
    """
    Проверять надо маршрут, отвечающий из памяти. Если healthcheck пойдёт на
    маршрут, зависящий от биржи или Claude, он превратится в проверку чужой
    доступности и уронит деплой при первом сбое у поставщика данных.
    """
    for text in (DOCKERFILE.read_text(), COMPOSE.read_text()):
        for url in re.findall(r"http://[^\s\"',\]]+", text):
            assert "/api/stats" in url, f"healthcheck ходит на {url}, а должен на /api/stats"
            assert "127.0.0.1" in url or "localhost" in url


def test_data_dir_created_when_ignored_from_context():
    """
    data/ исключён из образа, значит каталог обязан создаваться в Dockerfile —
    иначе sqlite не откроет ./data/moodex.db, если том не смонтирован.
    """
    di = ROOT / ".dockerignore"
    if di.exists() and re.search(r"^data/", di.read_text(), re.M):
        assert "mkdir -p /app/data" in DOCKERFILE.read_text()


@pytest.mark.parametrize("secret", [".env", "*.session"])
def test_secrets_not_copied_into_image(secret):
    """.env и сессии Telegram не должны попадать в слой образа."""
    di = ROOT / ".dockerignore"
    assert di.exists(), "нет .dockerignore — COPY . . тащит в образ всё, включая секреты"
    assert secret in di.read_text()


# ─── torch: только CPU-сборка ─────────────────────────────────────────────────
#
# 31.07 сборки перестали проходить. Причина — не код, а разрешение зависимостей.
# В requirements.txt стояло --extra-index-url на CPU-индекс PyTorch, и это НЕ
# гарантирует CPU-колесо: extra-index не задаёт приоритет источника, pip сливает
# оба индекса и выбирает по версии. Пин torch==2.4.1 без суффикса одинаково
# подходит к 2.4.1 с PyPI и к 2.4.1+cpu с индекса PyTorch.
#
# Замерено:
#     torch 2.4.1+cpu                      195 МБ
#     torch 2.4.1 с PyPI                   797 МБ + 12 пакетов nvidia-*
#     только семь из этих nvidia-пакетов 2 824 МБ
# Лишних около 3.4 ГБ на сборку — на небольшом сервере это «no space left on
# device», причём плавающе: зависит от того, сколько оставили прошлый образ и
# кэш сборки. Отсюда и «раньше собиралось».


def test_torch_installed_from_cpu_index_only():
    """
    torch ставится с --index-url (только этот источник), а не с
    --extra-index-url (оба источника, приоритета нет).
    """
    df = DOCKERFILE.read_text()
    assert "--index-url https://download.pytorch.org/whl/cpu" in df, (
        "torch должен ставиться с --index-url на индекс PyTorch"
    )
    torch_step = [ln for ln in df.splitlines() if "torch==" in ln]
    assert torch_step, "в Dockerfile нет отдельного шага установки torch"


def test_dockerfile_installs_torch_before_requirements():
    """
    Шаг с torch должен идти ДО pip install -r requirements.txt: иначе
    requirements успеет притащить CUDA-колесо, и заслон сработает поздно.
    """
    lines = DOCKERFILE.read_text().splitlines()
    i_torch = next(i for i, ln in enumerate(lines) if "torch==" in ln)
    i_req = next(i for i, ln in enumerate(lines) if "requirements.txt" in ln and "pip install" in ln)
    assert i_torch < i_req, "torch ставится после requirements.txt — порядок неверный"


def test_build_fails_loudly_on_cuda_packages():
    """
    Если CUDA-сборка всё же просочилась, сборка обязана упасть с внятной
    причиной, а не через десять минут по нехватке диска.
    """
    df = DOCKERFILE.read_text()
    assert "nvidia-" in df and "exit 1" in df, "нет заслона на пакеты nvidia-*"


def test_requirements_warns_about_the_trap():
    """
    Ловушку надо описать там, где её увидят: в requirements.txt рядом со
    строкой --extra-index-url. Иначе следующий человек уберёт шаг из Dockerfile
    как дублирующий.
    """
    req = (ROOT / "requirements.txt").read_text()
    assert "--index-url" in req and "Dockerfile" in req, (
        "в requirements.txt нет предупреждения про extra-index-url и Dockerfile"
    )
