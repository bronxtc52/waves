# План: hello — игрушечная цепочка из двух волн

## Волна W1 — hello.py с тестом
- Цель: добавить скрипт `hello.py`, который печатает `hello`, и тест на него.
- Готово, когда:
  - `python3 hello.py` печатает `hello`;
  - тест `tests/test_hello.py` проходит;
  - PR в `main` открыт, CI зелёный.
- Проверка: `python3 -m unittest discover -s tests`
- Зависит от: нет

## Волна W2 — флаг --name
- Цель: добавить в `hello.py` флаг `--name`: `python3 hello.py --name Мир` печатает `hello, Мир`.
- Готово, когда:
  - без флага поведение прежнее (`hello`);
  - с флагом `--name X` печатается `hello, X`;
  - тесты на оба случая проходят, PR в `main` открыт, CI зелёный.
- Проверка: `python3 -m unittest discover -s tests`
- Зависит от: W1
