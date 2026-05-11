# Источник сборок

https://github.com/kulikov0/whitelist-bypass

# Описание проекта

`WebRTCRunner` — это обертка для запуска headless-компонентов `whitelist-bypass` в Docker.
Сервис поднимает процессы для VK/Telemost (и опционально WBStream), читает cookie-файлы из `cookies/`,
выводит `join_link` в логи и может отправлять уведомления в Telegram.
Примечание: Для клиентской части используется кастомный APK, где прокси работает в режиме `0.0.0.0`.

# WebRTCRunner: минимальный запуск

## 1) Подготовка

1. Создай файл `.env` из шаблона:
   ```bash
   cp .env.example .env
   ```
2. Получи cookie-файлы из креаторов в репозитории `https://github.com/kulikov0/whitelist-bypass` и положи их в папку `cookies/`:
   - `vk-cookies.json`
   - `cookies-yandex.json`
3. При необходимости заполни в `.env`:
   - `SERVER_NAME`
   - `TELEGRAM_BOT_TOKEN`
   - `TELEGRAM_CHAT_ID`

## 2) Запуск

```bash
docker compose up -d --build
```

## 3) Проверка логов

```bash
docker compose logs -f webrtc-runner
```

## 4) Остановка

```bash
docker compose down
```
