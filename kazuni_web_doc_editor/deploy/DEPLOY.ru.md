# Развёртывание KazUni Doc Editor

## Содержание

1. [Структура проекта](#1-структура-проекта)
2. [Режим 1 — локальный запуск (разработка)](#2-режим-1--локальный-запуск-разработка)
3. [Режим 2 — продакшн без Docker (текущий)](#3-режим-2--продакшн-без-docker-текущий)
4. [Режим 3 — продакшн через Docker Compose](#4-режим-3--продакшн-через-docker-compose)
5. [Конфигурация Nginx](#5-конфигурация-nginx)
6. [SSL / Let's Encrypt](#6-ssl--lets-encrypt)
7. [Проверка после деплоя](#7-проверка-после-деплоя)

---

## 1. Структура проекта

```
/opt/kazuni_doc_editor/
├── app.py                        # Flask-приложение (точка входа)
├── requirements.txt
├── word_constructor/             # Blueprint: конструктор шаблонов
├── templates/                   # Jinja2-шаблоны
├── google-api-secrets.json       # Ключи Google API (не коммитить!)
├── data/                        # Постоянные данные (документы, превью)
└── deploy/
    ├── DEPLOY.ru.md              # ← этот файл
    ├── Dockerfile                # Образ приложения
    ├── docker-compose.yml        # Полный стек (app + onlyoffice + nginx)
    ├── nginx.docker.conf         # Nginx для Docker-режима
    ├── kazuni-doc-editor.service # systemd-юнит для продакшн без Docker
    └── kazuni-doc-editor.birqadam.kz.nginx.conf  # Nginx для продакшн без Docker
```

**Внешние зависимости:**
- **OnlyOffice Document Server** — редактор документов (запускается отдельным Docker-контейнером `kazuni-onlyoffice`)
- **LibreOffice** — конвертация документов в PDF (должен быть установлен на хосте в режиме без Docker)
- **Nginx** — reverse-proxy, терминирует SSL

---

## 2. Режим 1 — локальный запуск (разработка)

### Требования

- Python 3.10+
- LibreOffice (`libreoffice` или `soffice` в PATH)
- Docker (для OnlyOffice)

### Шаги

```bash
# 1. Клонировать репозиторий
git clone <repo-url> /opt/kazuni_doc_editor
cd /opt/kazuni_doc_editor

# 2. Создать виртуальное окружение
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 3. Запустить OnlyOffice локально
docker run -d \
  --name kazuni-onlyoffice-dev \
  -p 8020:80 \
  -e JWT_ENABLED=true \
  -e JWT_SECRET=kazuni-onlyoffice-secret \
  onlyoffice/documentserver:latest

# Дождаться готовности OnlyOffice (обычно 30–60 секунд)
until curl -sf http://localhost:8020/healthcheck; do
  echo "Ожидание OnlyOffice..."; sleep 5
done

# 4. Запустить Flask
export ONLYOFFICE_JWT_SECRET=kazuni-onlyoffice-secret
export ONLYOFFICE_INTERNAL_BASE_URL=http://localhost:8016
python3 app.py
```

Приложение доступно по адресу: http://localhost:8016

> **Важно:** при локальном запуске OnlyOffice должен иметь доступ к Flask-приложению
> для callback-запросов при сохранении документов. Если OnlyOffice запущен в Docker,
> используйте `http://host.docker.internal:8016` в `ONLYOFFICE_INTERNAL_BASE_URL`.

---

## 3. Режим 2 — продакшн без Docker (текущий)

Это текущий рабочий режим: Flask/Gunicorn запускается как systemd-сервис на хосте,
OnlyOffice работает в Docker, Nginx проксирует запросы.

### Требования на сервере

```bash
# Python и pip
apt-get install python3 python3-pip python3-venv

# LibreOffice для конвертации PDF
apt-get install libreoffice

# Nginx
apt-get install nginx

# Docker (для OnlyOffice)
curl -fsSL https://get.docker.com | sh
```

### Шаг 1 — Установить зависимости Python

```bash
cd /opt/kazuni_doc_editor
pip3 install -r requirements.txt
```

### Шаг 2 — Запустить OnlyOffice

```bash
docker run -d \
  --name kazuni-onlyoffice \
  --restart unless-stopped \
  -p 127.0.0.1:8020:80 \
  -e JWT_ENABLED=true \
  -e JWT_SECRET=kazuni-onlyoffice-secret \
  -e JWT_HEADER=Authorization \
  -e JWT_IN_BODY=true \
  -v onlyoffice_data:/var/www/onlyoffice/Data \
  -v onlyoffice_logs:/var/log/onlyoffice \
  -v onlyoffice_cache:/var/lib/onlyoffice \
  -v onlyoffice_db:/var/lib/postgresql \
  onlyoffice/documentserver:latest

# Дождаться готовности
until curl -sf http://localhost:8020/healthcheck; do
  echo "Ожидание OnlyOffice..."; sleep 5
done && echo "OnlyOffice готов"
```

### Шаг 3 — Установить systemd-сервис

```bash
# Скопировать юнит-файл
cp /opt/kazuni_doc_editor/deploy/kazuni-doc-editor.service \
   /etc/systemd/system/kazuni-doc-editor.service

# Перечитать конфигурацию systemd
systemctl daemon-reload

# Включить автозапуск и запустить
systemctl enable kazuni-doc-editor
systemctl start kazuni-doc-editor

# Проверить статус
systemctl status kazuni-doc-editor
```

### Шаг 4 — Настроить Nginx

```bash
# Скопировать конфиг
cp /opt/kazuni_doc_editor/deploy/kazuni-doc-editor.birqadam.kz.nginx.conf \
   /etc/nginx/sites-available/kazuni-doc-editor

ln -s /etc/nginx/sites-available/kazuni-doc-editor \
      /etc/nginx/sites-enabled/kazuni-doc-editor

# Проверить и перезагрузить
nginx -t && systemctl reload nginx
```

### Управление сервисом

```bash
systemctl start   kazuni-doc-editor   # запустить
systemctl stop    kazuni-doc-editor   # остановить
systemctl restart kazuni-doc-editor   # перезапустить после изменений кода
systemctl status  kazuni-doc-editor   # статус

# Логи в реальном времени
journalctl -u kazuni-doc-editor -f

# Последние 100 строк логов
journalctl -u kazuni-doc-editor -n 100
```

---

## 4. Режим 3 — продакшн через Docker Compose

Все сервисы (app + OnlyOffice + Nginx) запускаются в контейнерах.

### Шаги

```bash
cd /opt/kazuni_doc_editor

# 1. Собрать и запустить
docker compose -f deploy/docker-compose.yml up -d --build

# 2. Проверить что все сервисы поднялись
docker compose -f deploy/docker-compose.yml ps

# 3. Логи приложения
docker compose -f deploy/docker-compose.yml logs -f app

# 4. Логи OnlyOffice
docker compose -f deploy/docker-compose.yml logs -f onlyoffice
```

### Обновление кода

```bash
cd /opt/kazuni_doc_editor
git pull

docker compose -f deploy/docker-compose.yml up -d --build app
```

### Остановка

```bash
docker compose -f deploy/docker-compose.yml down

# Удалить также тома (осторожно — удалит данные!)
docker compose -f deploy/docker-compose.yml down -v
```

---

## 5. Конфигурация Nginx

Проект использует **два** nginx-конфига в зависимости от режима:

| Файл | Режим | Описание |
|------|-------|----------|
| `kazuni-doc-editor.birqadam.kz.nginx.conf` | Продакшн без Docker | Проксирует на `172.17.0.1:8016` (Docker bridge IP хоста) и `127.0.0.1:8020` (OnlyOffice) |
| `nginx.docker.conf` | Docker Compose | Проксирует на `app:8016` и `onlyoffice` (имена сервисов) |

### Структура маршрутов

```
/                              → Flask app (порт 8016)
/onlyoffice/                   → OnlyOffice (порт 8020), со stripped prefix
/web-apps/  /sdkjs/  /cache/   → OnlyOffice (статика и API)
/coauthoring/ и т.д.           → OnlyOffice
/ws/                           → Flask WebSocket (upgrade headers)
/services/word-constructor/ws/ → Flask WebSocket (upgrade headers)
/healthcheck                   → OnlyOffice healthcheck
```

### Важные параметры

```nginx
client_max_body_size 100M;     # Загрузка больших документов

proxy_read_timeout 3600s;      # WebSocket и длинные операции (1 час)
proxy_send_timeout 3600s;

# WebSocket upgrade
proxy_http_version 1.1;
proxy_set_header Upgrade $http_upgrade;
proxy_set_header Connection "upgrade";
```

---

## 6. SSL / Let's Encrypt

### Получить сертификат

```bash
apt-get install certbot python3-certbot-nginx

# Получить сертификат (Nginx должен уже отвечать на домен)
certbot --nginx -d kazuni-doc-editor.birqadam.kz

# Проверить автообновление
certbot renew --dry-run
```

### Настроить автообновление через cron

```bash
crontab -e
# Добавить строку:
0 3 * * * certbot renew --quiet && systemctl reload nginx
```

---

## 7. Проверка после деплоя

### Базовая доступность

```bash
# Проверить что приложение отвечает (локально)
curl -I http://localhost:8016/

# Проверить OnlyOffice
curl -sf http://localhost:8020/healthcheck && echo "OnlyOffice OK"

# Проверить через Nginx (HTTP)
curl -I http://kazuni-doc-editor.birqadam.kz/

# Проверить HTTPS и редирект
curl -I https://kazuni-doc-editor.birqadam.kz/
curl -I http://kazuni-doc-editor.birqadam.kz/ | grep Location
```

### Проверка SSL-сертификата

```bash
# Показать информацию о сертификате
echo | openssl s_client -connect kazuni-doc-editor.birqadam.kz:443 -servername kazuni-doc-editor.birqadam.kz 2>/dev/null \
  | openssl x509 -noout -dates -subject

# Проверить цепочку сертификатов
curl -vI https://kazuni-doc-editor.birqadam.kz/ 2>&1 | grep -E "SSL|TLS|certificate|expire"
```

### Проверка WebSocket

```bash
# wscat должен быть установлен: npm install -g wscat
wscat -c "wss://kazuni-doc-editor.birqadam.kz/services/word-constructor/api/template-builder/test-session/ws"

# Без wscat — через curl (только handshake)
curl -I -N \
  -H "Connection: Upgrade" \
  -H "Upgrade: websocket" \
  -H "Sec-WebSocket-Version: 13" \
  -H "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==" \
  https://kazuni-doc-editor.birqadam.kz/ws/
```

### Проверка OnlyOffice через Nginx

```bash
# Healthcheck через Nginx
curl -sf https://kazuni-doc-editor.birqadam.kz/healthcheck && echo "OK"

# Проверить что OnlyOffice-статика отдаётся
curl -I https://kazuni-doc-editor.birqadam.kz/web-apps/apps/api/documents/api.js
```

### Проверка JWT (OnlyOffice)

```bash
# Создать тестовый JWT-токен и проверить что OnlyOffice его принимает
python3 -c "
import jwt, time
payload = {'exp': int(time.time()) + 300}
token = jwt.encode(payload, 'kazuni-onlyoffice-secret', algorithm='HS256')
print('Token:', token)
"
```

### Мониторинг ресурсов

```bash
# Процессы gunicorn
ps aux | grep gunicorn

# Использование портов
ss -tlnp | grep -E '8016|8020|80|443'

# Docker-контейнеры
docker ps --filter name=kazuni

# Место на диске (данные OnlyOffice и загрузки)
du -sh /tmp/kazuni_word_constructor/
docker system df
```

### Типичные проблемы

| Симптом | Причина | Решение |
|---------|---------|---------|
| 502 Bad Gateway | Flask не запущен | `systemctl restart kazuni-doc-editor` |
| OnlyOffice не открывает документ | JWT-секрет не совпадает | Проверить `ONLYOFFICE_JWT_SECRET` в сервисе и контейнере |
| Документ не сохраняется | OnlyOffice не может достучаться до Flask | Проверить `ONLYOFFICE_INTERNAL_BASE_URL` |
| WebSocket разрывается | Nginx не проксирует Upgrade | Убедиться что WS-location с `proxy_http_version 1.1` |
| 413 Request Entity Too Large | Лимит Nginx | Добавить `client_max_body_size 100M` |
