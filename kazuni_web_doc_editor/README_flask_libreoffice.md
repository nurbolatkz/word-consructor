# Flask + LibreOffice document API

Простое приложение для загрузки документов, генерации PDF preview и открытия файла в LibreOffice для ручного редактирования.

## Что умеет API

- `POST /api/documents` загрузка файла
- `GET /api/documents` список документов
- `GET /api/documents/<id>` информация о документе
- `GET /api/documents/<id>/download` скачать исходный файл
- `GET /api/documents/<id>/preview` получить PDF preview
- `POST /api/documents/<id>/refresh` пересоздать preview
- `POST /api/documents/<id>/open-in-libreoffice` открыть исходный файл в LibreOffice

## Запуск

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python3 app.py
```

Приложение по умолчанию стартует на `http://127.0.0.1:5000`.

## Требования

- Python 3.10+
- LibreOffice или `soffice` в `PATH`

При необходимости путь к бинарнику LibreOffice можно задать так:

```bash
export LIBREOFFICE_BIN=/usr/bin/libreoffice
```

## Хранение данных

- Загруженные файлы: `data/uploads/`
- Сгенерированные preview: `data/previews/`
- Метаданные: `data/documents.json`

## Замечания

- Для файлов PDF preview просто копируется без конвертации.
- Для остальных форматов preview генерируется через headless-конвертацию LibreOffice.
- Эндпоинт открытия в LibreOffice рассчитан на локальную машину с GUI. На headless-сервере окно LibreOffice открыть нельзя, поэтому для такого деплоя рабочий сценарий это `upload -> preview -> download/edit locally -> upload/refresh`.
