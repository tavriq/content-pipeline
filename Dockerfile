FROM python:3.13-slim-bookworm

ARG BLOG_UID=109
ARG BLOG_GID=112

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update \
    && apt-get install --yes --no-install-recommends \
        ffmpeg \
        git \
        openssh-client \
        procps \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid "${BLOG_GID}" blog \
    && useradd --uid "${BLOG_UID}" --gid "${BLOG_GID}" --home-dir /home/blog --create-home blog

WORKDIR /app

COPY requirements.txt /tmp/requirements.txt
RUN pip install --requirement /tmp/requirements.txt

# Копия кода нужна для смоук-запуска образа в CI. На сервере поверх
# монтируется рабочее дерево репозитория (docker-compose.yml), потому
# что бот коммитит материалы и ходит в git на хосте: код доставляет
# git pull, а не пересборка образа.
COPY --chown=blog:blog . /app
RUN install -d -o blog -g blog /app/logs /app/data \
    && chown blog:blog /app \
    && python -m compileall -q scripts tg

USER blog

CMD ["python", "scripts/review_bot.py"]
