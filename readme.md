## AI Code Review Webhook for GitLab

Webhook-сервис для автоматического code review Merge Request'ов
с использованием Claude Opus 4.5.

### Возможности
- GitLab Merge Request webhook
- Inline comments в MR
- Fallback в общий комментарий
- Docker-ready
- Поддержка Anthropic Messages API

### Быстрый старт

```bash
cp .env.example .env
# заполнить .env
docker compose up -d --build
