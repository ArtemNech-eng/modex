"""
Охрана веса образа.

05.08 два деплоя подряд умерли не от бага в коде, а от того, что образ не
влез на диск: около 2.2 ГБ занимали torch и transformers ради тональности,
которая в торговом контуре не используется. Тесты держат это решение и
страхуют от тихого возврата пакетов в основной requirements.

Тесты читают текст файлов, а не импортируют их: ни Dockerfile, ни
анализатор тональности здесь исполнять незачем.
"""
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _src(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


def _requirement_lines(rel):
    out = []
    for line in _src(rel).splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            out.append(line)
    return out


def test_heavy_packages_are_not_in_the_main_requirements():
    lines = _requirement_lines("requirements.txt")
    assert not [x for x in lines if x.startswith(("torch", "transformers"))]


def test_the_torch_index_hint_is_gone_from_the_main_requirements():
    """Строка --extra-index-url без torch бессмысленна и путает следующего."""
    assert "--extra-index-url" not in "\\n".join(_requirement_lines("requirements.txt"))


def test_the_model_is_moved_aside_not_lost():
    lines = _requirement_lines("requirements-nlp.txt")
    assert any(x.startswith("torch") for x in lines)
    assert any(x.startswith("transformers") for x in lines)


def test_the_moved_file_keeps_the_cpu_index():
    """Без индекса PyTorch локальная установка притащит CUDA-сборку на 3 ГБ."""
    assert "download.pytorch.org/whl/cpu" in _src("requirements-nlp.txt")


def test_the_image_no_longer_installs_torch():
    src = _src("Dockerfile")
    assert "RUN pip install --no-cache-dir \\\\\n    --index-url" not in src


def test_the_image_still_installs_the_rest():
    src = _src("Dockerfile")
    assert "RUN pip install --no-cache-dir -r requirements.txt" in src


def test_the_cuda_guard_survives():
    """Заслон дешёвый, а возврат CUDA-сборки стоит 3.4 ГБ и падения сборки."""
    assert "СБОРКА ОСТАНОВЛЕНА: в образ попала CUDA-сборка torch" in _src("Dockerfile")


def test_the_trust_stores_survive():
    """Правка про вес не имеет права задеть корень Минцифры и бандл certifi."""
    src = _src("Dockerfile")
    assert "russian_trusted_root_ca.crt" in src
    assert "import certifi; print(certifi.where())" in src
    assert "GRPC_DEFAULT_SSL_ROOTS_FILE_PATH" in src


def test_sentiment_survives_without_the_model():
    """Без этого разбор сообщений падает на RuntimeError из _predict_sync."""
    src = _src("src/nlp/sentiment_analyzer.py")
    assert "return [keyword_sentiment(t) for t in texts]" in src


def test_the_dictionary_fallback_is_checked_before_the_thread_pool():
    src = _src("src/nlp/sentiment_analyzer.py")
    body = src[src.index("async def analyze_batch"):src.index("def unload")]
    assert body.index("self._pipeline is None") < body.index("run_in_executor")
