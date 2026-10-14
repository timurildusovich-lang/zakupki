# Команды для Codex

Все пути входных файлов должны быть внутри проекта. По умолчанию используется тестовый справочник и тестовая история. Для черновиков по рабочему справочнику перед командой укажите `--env live`; это не включает рабочие подключения.

Почтовый тест с настоящей почтой использует `--env mail_test`. Его отправитель timur-ildusovich@mail.ru, адресат каждого письма — только timurildusovich@gmail.com. Переменные EMAIL_LOGIN и EMAIL_PASSWORD читаются из локального .env; файл не выводится. Mail.ru требует пароль внешнего приложения.

```powershell
.\run.ps1 --env mail_test doctor
.\run.ps1 --env mail_test mail-check
.\run.ps1 --env mail_test prepare --text "Запросить сток Davines и Tigi"
.\run.ps1 --env mail_test preview --request REQUEST_ID
.\run.ps1 --env mail_test revise --request REQUEST_ID --file data/edits/change.json
.\run.ps1 --env mail_test approve --request REQUEST_ID --digest DIGEST --actor "Подтвердивший пользователь"
.\run.ps1 --env mail_test send --request REQUEST_ID
.\run.ps1 --env mail_test poll
```

mail-check проверяет вход, не отправляет письма и не читает содержимое старой почты. До успешного результата send заблокирован. В предпросмотре recipients — Gmail, intended_recipients — справочные email поставщика. Напоминания в этой среде также ограничены Gmail и требуют нового текста Codex через remind. Для проверки ответов ответьте с Gmail на соответствующее тестовое письмо. Состояния, файлы и курсор хранятся в data/mail_test; старые письма не импортируются. Прайсы сохраняются только локально, без заданий Google Drive.

```powershell
.\run.ps1 doctor
.\run.ps1 demo
.\run.ps1 report
.\run.ps1 prepare --text "Запросить сток Davines и Tigi"
.\run.ps1 prepare --intent data/intents/request.json --actor "Пользователь Codex"
.\run.ps1 --env live prepare --intent data/intents/request.json
```

Намерение:

```json
{
  "source_text": "Исходное сообщение постановщика",
  "brands": ["Davines", "Tigi"],
  "kinds": ["stock", "preorder"],
  "all_brands": false,
  "supplier_ids": ["supplier_02", "supplier_05"]
}
```

`supplier_ids` необязателен: без него используются все участвующие поставщики этих брендов. Идентификаторы тестовых поставщиков отличаются: `test_eu`, `test_ru`, `test_silent`.

Для справочника сохраните connector values вместе с range и временем чтения в data/source-sheet.json, затем:

```powershell
.\run.ps1 import-catalog --file data/source-sheet.json
```

Результат `prepare` содержит request ID, deliveries, blockers, skipped и preview_digest. Перед подтверждением покажите все письма. Файл изменений:

```json
{"deliveries": [
  {"id": "DELIVERY_ID", "recipients": ["prices@example.invalid"], "subject": "stock Davines", "body": "Hi! Please send the current stock list for Davines."},
  {"id": "OTHER_DELIVERY_ID", "excluded": true}
]}
```

```powershell
.\run.ps1 revise --request REQUEST_ID --file data/edits/change.json
.\run.ps1 preview --request REQUEST_ID
.\run.ps1 approve --request REQUEST_ID --digest DIGEST_FROM_LAST_PREVIEW --actor "Подтвердивший пользователь"
.\run.ps1 send --request REQUEST_ID
```

`send` в тестовой среде записывает `simulated`. В рабочей среде он заблокирован настройками до отдельного перехода к эксплуатации. Изменить brands/kinds следует созданием нового плана; для текста/адресов используется revise. Уже отправленные письма не редактируются задним числом.

Входящее тестовое письмо:

```json
{
  "id": "unique-source-id",
  "from": "prices@example.invalid",
  "received_at": "2026-10-01T12:00:00+03:00",
  "subject": "Current price list",
  "body": "Please find the price list attached.",
  "references": ["MESSAGE_ID_FROM_OUTBOX"],
  "links": [],
  "attachments": [{"name": "Davines stock.csv", "path": "fixtures/prices/Davines stock.csv"}]
}
```

```powershell
.\run.ps1 ingest --file data/incoming/message.json
.\run.ps1 ingest-eml --file fixtures/incoming.eml --received-at "2026-10-01T12:00:00+03:00"
.\run.ps1 classify --message MESSAGE_ID --file data/reviews/review.json
```

Классификация после проверки файла:

```json
{
  "kind": "price",
  "coverage": [{"brand": "Davines", "kind": "stock"}],
  "evidence": {"attachment_names": ["Davines stock.csv"], "links": [], "file_brands": ["Davines"]},
  "actor": "Codex"
}
```

Для вопроса `{"kind":"question","actor":"Codex"}`. Для автоматического или постороннего ответа — other. Если связь неоднозначна, после решения постановщика:

В `evidence.file_brands` укажите все проверенные бренды файла, включая незапрошенные. Для файла с несколькими брендами нужно выбрать папку. `coverage` перечисляет только полученные пары из текущего запроса. Обновление индекса прайсов через `inventory` также закрывает соответствующие позиции активных запросов.

```powershell
.\run.ps1 associate --message MESSAGE_ID --request REQUEST_ID --delivery DELIVERY_ID --actor "Автор решения"
```

Индекс существующих прайсов и выполненных проверок:

```json
{
  "checks": [{"supplier_id":"supplier_02","brand":"Tigi","checked_at":"2026-10-01T12:00:00+03:00","complete":true,"sources":["imap","drive"]}],
  "prices": [{"id":"drive_FILE_ID","supplier_id":"supplier_02","brands":["Tigi"],"kinds":["stock"],"coverage":[{"brand":"Tigi","kind":"stock"}],"date_from":"2026-09-01T00:00:00+03:00","date_to":"2026-09-30T23:59:59+03:00","source":"drive","url":"VERIFIED_URL"}]
}
```

Значения примера не являются действующими данными. Успешность checks означает, что Codex действительно проверил почту и соответствующие папки, не только создал JSON.

```powershell
.\run.ps1 inventory --file data/inventory/check.json
.\run.ps1 tick
.\run.ps1 tick --at "2026-10-02T15:00:00+03:00" --simulate
.\run.ps1 remind --request REQUEST_ID --delivery DELIVERY_ID --file data/drafts/reminder.json
.\run.ps1 pause --request REQUEST_ID --delivery DELIVERY_ID
.\run.ps1 resume --request REQUEST_ID --delivery DELIVERY_ID
.\run.ps1 notifications
.\run.ps1 notifications --acknowledge
```

Не передавайте `--simulate` для рабочей среды. `tick` без этого ключа показывает действия, не отправляет письма. Для рабочего напоминания файл содержит новые subject/body. Перед `notifications --acknowledge` покажите уведомления в чате.

Сохранение через connector выполняется отдельно. Jobs находятся в state.json. Выбор папки:

```powershell
.\run.ps1 route --job JOB_ID --folder VERIFIED_FOLDER_ID
.\run.ps1 complete-job --job JOB_ID --file data/storage/receipt.json
```

Для теста receipt: `{"simulated":true,"url":"data/test/attachments/.../file.csv"}`. Для рабочего результата: `{"verified":true,"file_id":"OBSERVED_FILE_ID","url":"OBSERVED_GOOGLE_URL"}` — только после реального сохранения и чтения результата. На текущем этапе внешний upload запрещён.

Рабочий `poll` доступен только после настройки и включения IMAP. Он сохраняет входящие и продвигает UID-курсор после успешного локального сохранения всех прочитанных писем. UIDVALIDITY предотвращает потерю сообщений после пересоздания папки. Неразобранное письмо не закрывает запрос.

Текущий статус: mode=live, production_transition.status=waiting_for_production_credentials. До успешного `--env live mail-check` для tam_sabis4@mail.ru фактическая рассылка заблокирована. Для него в .env нужны PRODUCTION_EMAIL_LOGIN и PRODUCTION_EMAIL_PASSWORD; тестовую пару EMAIL_LOGIN/EMAIL_PASSWORD сохранять отдельно. Автоматизация на паузе до подключения. Сам переход разрешён; новые рассылки требуют просмотра и подтверждения текстов/адресатов.
