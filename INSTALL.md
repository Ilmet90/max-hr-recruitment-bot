# INSTALL

Подробная установка MAX HR Recruitment Bot на Debian/Ubuntu Server.

## 1. Подготовить сервер

Обновите систему и убедитесь, что есть доступ в интернет:

```bash
sudo apt update
sudo apt upgrade
```

## 2. Клонировать репозиторий

```bash
git clone <repo-url>
cd max-hr-recruitment-bot
```

## 3. Запустить установщик

```bash
sudo bash install.sh
```

## 4. Ответить на вопросы установщика

Установщик попросит:

- полное название организации;
- краткое название организации;
- вышестоящую организацию;
- регион;
- название бота;
- логин главной web-учётной записи;
- пароль главной web-учётной записи;
- служебный код для команды `/admin`;
- `MAX_BOT_TOKEN`;
- `APP_HOST`;
- `APP_PORT`.

Если пароль оставить пустым, установщик сгенерирует его и покажет один раз в конце установки.

## 5. Открыть web-админку

Откройте:

```text
http://<IP_сервера>:<APP_PORT>
```

Войдите под логином и паролем главной учётной записи.

## 6. Внести MAX-токен, если он не был указан

Откройте:

```bash
sudo nano /opt/max-hr-recruitment-bot/.env
```

Заполните `MAX_BOT_TOKEN`, затем выполните:

```bash
sudo systemctl start max-hr-bot.service
sudo systemctl enable max-hr-bot.service
```

## 7. Проверить службы

```bash
sudo systemctl status max-hr-admin.service
sudo systemctl status max-hr-bot.service
```

Логи:

```bash
sudo journalctl -u max-hr-admin.service -f
sudo journalctl -u max-hr-bot.service -f
```

## 8. Настройка импорта вакансий

После установки можно подключить импорт вакансий с портала «Работа России»:

1. Войдите в web-админку.
2. Откройте «Вакансии → Импорт с Работа России».
3. Укажите ИНН работодателя или код работодателя на портале.
4. Включите импорт и сохраните настройки.
5. Нажмите «Проверить вакансии» и изучите предпросмотр.
6. Нажмите «Импортировать найденные вакансии».

## 9. Включить обслуживание из web-интерфейса

Для проверки обновлений, обновления из GitHub и перезапуска служб из раздела «О программе» выполните один раз:

```bash
cd /opt/max-hr-recruitment-bot
sudo bash scripts/setup_maintenance_sudoers.sh
```

Скрипт создаёт ограниченный sudoers-файл только для управления службами `max-hr-admin.service` и `max-hr-bot.service`.

## 10. Настроить firewall

Открывайте порт web-админки только во внутренней сети, через VPN или SSH-туннель. Не публикуйте web-админку в интернет без HTTPS и дополнительной защиты.

## 11. Сделать первый backup

```bash
cd /opt/max-hr-recruitment-bot
sudo bash scripts/backup.sh
```

## 12. Удалить проект

```bash
cd /opt/max-hr-recruitment-bot
sudo bash scripts/uninstall.sh
```
