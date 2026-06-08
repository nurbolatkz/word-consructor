# Word Portal

Focused Flask service for the KazUni Word Constructor workflow with 1C and OnlyOffice.

## Docker Deployment

Copy `.env.example` to `.env`, set a strong `ONLYOFFICE_JWT_SECRET`, and set `ONLYOFFICE_PUBLIC_BASE_URL` to `/onlyoffice` for single-domain deployment or to a browser-reachable OnlyOffice URL.

Start the stack:

```bash
docker compose up -d --build
```

Default local URLs:

```text
App:        http://localhost:8080
OnlyOffice: http://localhost:8080/onlyoffice
```

1C template-builder endpoint:

```text
POST /services/word-constructor/api/1c/template-builder/bridge
```

The response contains `session_id`, `builder_url`, `status_url`, and `download_url`.

OnlyOffice is configured with `ALLOW_PRIVATE_IP_ADDRESS=true` so the Document Server can download files from the app container over the Docker network.

Stateless placeholder replacement endpoint:

```text
POST /services/word-constructor/api/1c/replace
```

Multipart fields:

```text
template=document.docx
params={"ФИО":"Иванов И.И.","Дата":"08.06.2026"}
```

The response is the replaced `.docx` file. Supported placeholder formats: `{{Key}}` and `[Key]`.

Replace and open manual edit session:

```text
POST /services/word-constructor/api/1c/replace-edit
```

The request format is the same as `/api/1c/replace`. The response contains:

```json
{
  "id": "...",
  "editor_url": "/services/word-constructor/template-builder/...",
  "update_url": "/services/word-constructor/api/1c/edit-sessions/.../document",
  "status_url": "/services/word-constructor/api/1c/edit-sessions/.../status"
}
```

When the user clicks `Обновить` in 1C, call `update_url`. The service force-saves OnlyOffice and returns the latest `.docx` without deleting the edit session.

Session lifetime defaults to 35 minutes (`SESSION_TTL_SECONDS=2100`, `TEMPLATE_BUILDER_SESSION_TTL_SECONDS=2100`).

Table row expansion is supported for JSON object arrays. In Word, create a table with a single template row using placeholders like:

```text
[Услуги.НоваяСтрока.Наименование]    [Услуги.НоваяСтрока.Цена]
```

Then send params:

```json
{
  "Услуги": [
    {"Наименование": "Разработка", "Цена": "1000"},
    {"Наименование": "Поддержка", "Цена": "250"}
  ]
}
```

The service clones the template row once per array item and replaces field placeholders from each object.

For replace-edit sessions, the editor opens with a `Записать и Закрыть` button. After the user clicks it, poll `status_url` until `status` is `ready`; the response includes `download_url`. Downloading that URL returns the saved `.docx` and deletes the session files from the server.
