"""
Одноразовый патч: отдавать ПРИЧИНУ пустого профиля рядом с profiles_ready.

Сейчас /api/volume-scan говорит profiles_ready: 0. Это факт без причины:
непонятно, истории мало, дни короткие или база пуста вовсе. Причина
уже считается в profile_gap/profile_note и кладётся в stream.profile_note —
но видна только в логах контейнера, куда лишний раз не полезешь.

ПОЧЕМУ СКРИПТОМ. src/api/main.py — около 167 КБ. Переписывать его
целиком ради одной строки — лучший способ потерять кусок файла.

ПОЧЕМУ ВСЕГДА ВЫХОД НУЛЬ. Падение на шаге «Патч» никто не увидит:
доступа к логам сборки у агента нет, и два патча уже падали молча.
Поэтому неудача не роняет шаг, а сознательно оставляет файл без правки,
чтобы упал tests/test_api_profile_note.py — его вывод приезжает в PR.
Отказ должен кричать там, где его увидят.

Идемпотентен: второй запуск ничего не делает.
"""
import pathlib

API = pathlib.Path("src/api/main.py")
ANCHOR = '"profiles_ready"'
MARK = '"profile_note"'

src = API.read_text(encoding="utf-8")

if MARK in src:
    print("уже применено")
    raise SystemExit(0)

lines = src.split("\n")
hits = [i for i, ln in enumerate(lines) if ANCHOR in ln]

if len(hits) != 1:
    print(f"НЕ ПРИМЕНЕНО: {ANCHOR} встречается {len(hits)} раз, нужен ровно один.")
    print("Файл не тронут намеренно — пусть упадёт тест и скажет об этом в PR.")
    raise SystemExit(0)

i = hits[0]
line = lines[i]
indent = line[:len(line) - len(line.lstrip())]

#  getattr с None по умолчанию: атрибут ставит фоновая задача _volume_profiles,
#  и до её первого круга его просто нет. Маршрут не должен из-за этого падать.
lines.insert(i + 1, f'{indent}"profile_note": getattr(CURRENT, "profile_note", None),')
API.write_text("\n".join(lines), encoding="utf-8")
print(f"применено: строка добавлена после {ANCHOR}")
