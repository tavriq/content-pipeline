# DEPLOY: review-бот на VPS

Review-бот работает в Docker Compose на Amsterdam VPS. Он собирает источники,
ранжирует темы, генерирует материалы и выполняет отложенные Telegram-публикации.

## Текущее окружение

- рабочий каталог: `/srv/prod/blog-endocrinology`;
- процесс: `blog-prod-review-bot-1`;
- системный пользователь внутри и снаружи контейнера: `blog`, uid/gid `109:112`;
- секреты: `/srv/prod/blog-endocrinology/.env`, права `600`;
- read-write deploy key: `/etc/blog/ssh`, вне репозитория;
- Whisper cache: `/var/lib/blog-cache`, вне репозитория;
- аварийная systemd-копия: `/opt/blog`, сервис `review-bot.service` отключён;
- backup перед миграцией: `/srv/backups/blog/`.
- PostgreSQL: отдельные database/role в `platform-infra-postgres-1`, сеть `platform-db`;
- DNS контейнера: `1.1.1.1`, затем `8.8.8.8`; штатный DNS Timeweb не разрешает NCBI E-utilities;

Docker-контейнер запускается без root, с `no-new-privileges`, без Linux
capabilities, с read-only root filesystem и лимитами CPU, RAM и PID.

## Проверка состояния

```bash
cd /srv/prod/blog-endocrinology
docker compose ps
docker compose logs --tail=50
docker inspect --format '{{.State.Health.Status}}' blog-prod-review-bot-1
```

Ожидается `Up ... (healthy)`. В логах должны быть строки `бот запускается` и
`Application started`, без `ERROR`, `FATAL` и traceback.

Проверка конфигурации без запуска второго polling-процесса:

```bash
cd /srv/prod/blog-endocrinology
docker compose run --rm --no-deps review-bot \
  python scripts/review_bot.py --check

# Применить новые миграции перед запуском бота:
docker compose run --rm --no-deps review-bot \
  python scripts/database.py migrate
```

## Что доставляет код

Образ содержит окружение: python, ffmpeg, git, зависимости. Сам код
приезжает `git pull` на хосте — рабочее дерево смонтировано внутрь
контейнера, потому что бот коммитит материалы и расшифровки. Пересборка
образа нужна только когда менялись `requirements.txt` или `Dockerfile`.

Миграции базы накатываются вручную, до старта бота:

```bash
docker compose run --rm --no-deps review-bot python scripts/database.py migrate
```

## Ручное обновление после merge в main

Сначала убедиться, что GitHub Actions для `main` зелёный. Затем:

```bash
cd /srv/prod/blog-endocrinology

# Бот сам коммитит материалы: грязное дерево нельзя затирать.
sudo -u blog git status --short --branch

docker compose stop review-bot
sudo -u blog env \
  GIT_SSH_COMMAND='ssh -i /etc/blog/ssh/id_ed25519 -o IdentitiesOnly=yes -o UserKnownHostsFile=/etc/blog/ssh/known_hosts' \
  git pull --ff-only origin main

# Образ собирается до миграций: migrate исполняется в нём же, и на старом
# образе он пойдёт со старыми зависимостями.
docker compose build

# Миграции — до старта бота, всегда. Схема могла уехать вперёд кода.
docker compose run --rm --no-deps review-bot python scripts/database.py migrate

docker compose up --detach
docker compose ps
docker compose logs --tail=50
```

Шаг с миграциями не пропускать даже когда кажется, что их не было:
`migrate` идемпотентен и на актуальной схеме отрабатывает вхолостую, а
пропущенная миграция роняет бота на первом же запросе к базе.

Перед миграцией, меняющей схему, снять дамп — он занимает секунду и
избавляет от выбора между откатом и потерей ответов автора:

```bash
docker exec platform-infra-postgres-1 \
  pg_dump -U platform_admin -d blog_content --format=custom \
  > /root/backups/blog_content-$(date +%F).dump
```

Если `git status` показывает незакоммиченные изменения или `pull --ff-only`
не проходит, обновление остановить и сначала разобраться с git. Не применять
`reset --hard`: в дереве могут быть ответы автора, которые бот ещё не успел
отправить на GitHub.

## Проверка полного рабочего круга

1. Отправить боту `/start`, затем `/status`.
2. Положить материал в `tg/review/` и влить изменение в `main`.
3. Отправить `/review` — бот должен показать материал с кнопками.
4. Подтвердить материал и проверить, что коммит бота появился на GitHub.
5. Прислать короткое голосовое и проверить появление расшифровки в
   `core/transcripts/`.

## Аварийный откат на systemd

Старая установка в `/opt/blog` не удаляется. Если контейнер не запускается:

```bash
cd /srv/prod/blog-endocrinology
docker compose down
sudo systemctl start review-bot.service
sudo systemctl status review-bot.service
```

После устранения проблемы вернуть Docker:

```bash
sudo systemctl stop review-bot.service
cd /srv/prod/blog-endocrinology
docker compose up --detach
docker compose ps
```

Не запускать systemd и Docker одновременно: два процесса с одним Telegram
токеном будут конфликтовать за `getUpdates`.

## Логи и данные

- stdout/stderr: `docker compose logs -f review-bot`;
- файловый лог: `/srv/prod/blog-endocrinology/logs/bot.log`;
- состояние очереди: `data/bot-state.json`;
- Docker-логи ротируются драйвером `local`: максимум три файла по 10 МБ;
- модель Whisper хранится в `/var/lib/blog-cache` и повторно не скачивается.

Бот делает `pull --rebase --autostash` перед записью и `push` после коммита.
При отказе push смотреть `git status`, контейнерные логи и `logs/bot.log`.
