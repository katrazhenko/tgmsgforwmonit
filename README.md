# 🔍 Telegram Monitor Bot

Userbot на базі [Telethon](https://github.com/LonamiWebs/Telethon), який моніторить групи та канали Telegram у реальному часі, фільтрує повідомлення за ключовими словами та пересилає цільові повідомлення в заданий канал. Підтримує AI-фільтрацію через OpenAI GPT для відсіювання реклами та спаму.

Запускається як **systemd-сервіс** `tgmsgforwmonit.service` — працює у фоні, стартує автоматично після перезавантаження сервера.

---

## 📋 Зміст

- [Як це працює](#як-це-працює)
- [Вимоги](#вимоги)
- [Встановлення](#встановлення)
- [Налаштування](#налаштування)
- [Systemd-сервіс](#systemd-сервіс)
- [Команди управління](#команди-управління)
- [Логіка фільтрації](#логіка-фільтрації)
- [AI-фільтрація](#ai-фільтрація)
- [Структура проєкту](#структура-проєкту)
- [Тести](#тести)

---

## Як це працює

```
Telegram-групи → [Keyword-фільтр] → [Minus-слова] → [AI-фільтр GPT] → Черга → Канал-дестинація
```

1. Бот слухає **всі** вхідні повідомлення в групах, де присутній акаунт
2. Перевіряє наявність **ключових слів** (`keywords`)
3. Відкидає повідомлення, що містять **мінус-слова** (`minus_words`)
4. За бажанням — пропускає через **GPT**, який вирішує: цільове чи спам
5. Цільові повідомлення потрапляють у **чергу** та пересилаються у вказаний канал із затримкою

---

## Вимоги

- Python 3.10+
- Linux-сервер з systemd (Ubuntu 20.04+ / Debian 11+)
- Telegram-акаунт (не бот-токен, а саме акаунт)
- API-ключі з [my.telegram.org](https://my.telegram.org/apps)
- *(Опційно)* OpenAI API ключ для AI-фільтрації

---

## Встановлення

```bash
# 1. Клонуй репозиторій у /opt (стандартне місце для сервісів)
sudo git clone https://github.com/your-repo/telegram-monitor-bot.git /opt/tgmsgforwmonit
cd /opt/tgmsgforwmonit

# 2. Створи virtualenv
python3 -m venv .venv
source .venv/bin/activate

# 3. Встанови залежності
pip install -r requirements.txt
```

---

## Налаштування

### 1. Файл `.env`

```bash
cp .env.example .env
nano .env
```

```env
# Telegram API — отримай на https://my.telegram.org/apps
TG_API_ID=12345678
TG_API_HASH=your_api_hash_here
TG_PHONE=+34600000000
```

> ⚠️ Ніколи не додавай `.env` у git. Файл вже включено в `.gitignore`.

### 2. Перший запуск — авторизація

Перед реєстрацією сервісу потрібно **один раз** авторизуватись вручну — Telethon запитає код з SMS або Telegram:

```bash
cd /opt/tgmsgforwmonit
source .venv/bin/activate
python main.py
# Введи код підтвердження → Ctrl+C після успішного старту
```

Сесія збережеться у файлі `<номер>.session`. Надалі авторизація автоматична.

### 3. `config.json`

```json
{
  "keywords": ["cita", "tarjeta", "renovar", "асило"],
  "minus_words": ["garantizado", "bitcoin", "usdt"],
  "skip_words": ["de", "la", "el", "в", "на"],
  "forward_channel": "@your_channel",
  "admins": ["@your_username"],
  "join_queue": [],
  "ai_filter_enabled": false,
  "openai_api_key": "",
  "openai_model": "gpt-4o-mini"
}
```

| Поле | Опис |
|---|---|
| `keywords` | Слова/фрази → повідомлення пересилається |
| `minus_words` | Стоп-слова → повідомлення ігнорується |
| `skip_words` | Артиклі, прийменники — ігноруються при очищенні `minus_words` |
| `forward_channel` | Канал або username адміна для пересилки |
| `admins` | Username (з `@`) з правом на команди |
| `ai_filter_enabled` | Увімкнути GPT-фільтрацію |
| `openai_api_key` | Ключ OpenAI |
| `openai_model` | Модель GPT (`gpt-4o-mini` — оптимально) |

---

## Systemd-сервіс

### Створення файлу сервісу

```bash
sudo nano /etc/systemd/system/tgmsgforwmonit.service
```

Вміст файлу:

```ini
[Unit]
Description=Telegram Monitor Bot — tgmsgforwmonit
Documentation=https://your-org.github.io/tgmsgforwmonit
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=ubuntu
Group=ubuntu
WorkingDirectory=/opt/tgmsgforwmonit
EnvironmentFile=/opt/tgmsgforwmonit/.env
ExecStart=/opt/tgmsgforwmonit/.venv/bin/python main.py
Restart=on-failure
RestartSec=10
StandardOutput=journal
StandardError=journal
SyslogIdentifier=tgmsgforwmonit

# Захист від подвійного запуску при рестарті:
# lock-файл видаляється автоматично при зупинці через atexit,
# але якщо процес впав — прибираємо вручну
ExecStopPost=/bin/rm -f /opt/tgmsgforwmonit/monitor.lock

[Install]
WantedBy=multi-user.target
```

> **`User=ubuntu`** — замін на ім'я свого системного користувача (`whoami`).  
> Не запускай від `root`.

### Активація та запуск

```bash
# Перечитати конфіги systemd
sudo systemctl daemon-reload

# Увімкнути автозапуск при старті системи
sudo systemctl enable tgmsgforwmonit.service

# Запустити сервіс
sudo systemctl start tgmsgforwmonit.service

# Перевірити статус
sudo systemctl status tgmsgforwmonit.service
```

Очікуваний вивід:

```
● tgmsgforwmonit.service - Telegram Monitor Bot — tgmsgforwmonit
     Loaded: loaded (/etc/systemd/system/tgmsgforwmonit.service; enabled)
     Active: active (running) since Thu 2026-02-20 10:00:00 UTC; 5s ago
   Main PID: 12345 (python)
```

### Управління сервісом

| Дія | Команда |
|---|---|
| Старт | `sudo systemctl start tgmsgforwmonit` |
| Зупинка | `sudo systemctl stop tgmsgforwmonit` |
| Перезапуск | `sudo systemctl restart tgmsgforwmonit` |
| Статус | `sudo systemctl status tgmsgforwmonit` |
| Вимкнути автозапуск | `sudo systemctl disable tgmsgforwmonit` |

### Перегляд логів

```bash
# Останні 50 рядків
sudo journalctl -u tgmsgforwmonit -n 50

# Live-стрім логів
sudo journalctl -u tgmsgforwmonit -f

# Логи за сьогодні
sudo journalctl -u tgmsgforwmonit --since today

# Логи за конкретний період
sudo journalctl -u tgmsgforwmonit --since "2026-02-20 10:00" --until "2026-02-20 11:00"
```

Файловий лог також пишеться в `/opt/tgmsgforwmonit/monitor.log`.

### Оновлення коду

```bash
cd /opt/tgmsgforwmonit
git pull
sudo systemctl restart tgmsgforwmonit
sudo systemctl status tgmsgforwmonit
```

### Усунення проблем

**Сервіс не стартує після краша (залишився `monitor.lock`)**

```bash
sudo systemctl stop tgmsgforwmonit
rm /opt/tgmsgforwmonit/monitor.lock
sudo systemctl start tgmsgforwmonit
```

Файл `ExecStopPost` в unit-файлі прибирає lock автоматично — але якщо OOM-killer або `kill -9` завершив процес, прибери вручну.

**Сесія протухла / потрібна повторна авторизація**

```bash
sudo systemctl stop tgmsgforwmonit
rm /opt/tgmsgforwmonit/*.session
cd /opt/tgmsgforwmonit && source .venv/bin/activate && python main.py
# Пройди авторизацію → Ctrl+C
sudo systemctl start tgmsgforwmonit
```

---

## Команди управління

Надсилаються в **особисте повідомлення акаунту** з акаунту, що є в `admins`.

### 🤖 AI-фільтрація

| Команда | Опис |
|---|---|
| `/ai_enable` | Увімкнути GPT-фільтрацію |
| `/ai_disable` | Вимкнути |
| `/ai_set_key sk-...` | Задати OpenAI API ключ |
| `/ai_set_model gpt-4o-mini` | Змінити модель |
| `/ai_status` | Статус + статистика |
| `/ai_test <текст>` | Протестувати на тексті |

### 📢 Канал пересилки

| Команда | Опис |
|---|---|
| `/set_channel @канал` | Задати канал |
| `/get_channel` | Поточний канал |
| `/queue_status` | Кількість повідомлень у черзі |

### 🔍 Ключові слова

| Команда | Опис |
|---|---|
| `/add_word <слово>` | Додати |
| `/del_word <слово>` | Видалити |

### 🚫 Мінус-слова

| Команда | Опис |
|---|---|
| `/add_minus <слово>` | Додати |
| `/del_minus <слово>` | Видалити |
| `/clean_minus` | Очистити від дублів та skip/keyword слів |

### ⏭️ Skip-слова

| Команда | Опис |
|---|---|
| `/add_skip <слово>` | Додати |
| `/del_skip <слово>` | Видалити |

### 📥 Групи

| Команда | Опис |
|---|---|
| `/join_add @г1 @г2` | Додати в чергу |
| `/join_del @група` | Видалити з черги |
| `/join_list` | Показати чергу |
| `/join_all` | Вступити у всі (15 сек між вступами) |
| `/join @група` | Вступити в одну |
| `/leave @група` | Вийти |

### ⚙️ Загальне

| Команда | Опис |
|---|---|
| `/groups` | Всі групи/канали акаунту |
| `/list` | Всі поточні налаштування |
| `/help` | Довідка |

---

## Логіка фільтрації

```
Вхідне повідомлення
        │
        ▼
 Є текст? ──── Ні ──→ ІГНОР
        │
       Так
        │
        ▼
 Є мінус-слово? ──── Так ──→ ІГНОР
        │
        Ні
        │
        ▼
 Є keyword? ──── Ні ──→ ІГНОР
        │
       Так
        │
        ▼
 AI увімкнено? ──── Ні ──→ ЧЕРГА → ПЕРЕСИЛКА
        │
       Так
        │
        ▼
  GPT: SPAM? ──── Так ──→ ІГНОР
        │
        Ні (TARGET)
        │
        ▼
   ЧЕРГА → ПЕРЕСИЛКА
```

**Ключові слова** — перевіряється кожен токен фрази через `\b` (word-boundary). Часткові збіги не рахуються: `tarjetita` ≠ `tarjeta`. Регістр ігнорується.

**Мінус-слова** — пряме входження рядка (підтримуються мультислівні фрази). Спрацьовує до перевірки keywords.

---

## AI-фільтрація

GPT отримує текст, знайдене keyword, назву чату та контекст зі списків. Відповідає: `TARGET` або `SPAM`.

- **Рекомендована модель:** `gpt-4o-mini` — найдешевша, достатньо точна
- **При помилці:** повідомлення пропускається (не блокується), щоб не втрачати цільові

---

## Структура проєкту

```
/opt/tgmsgforwmonit/
├── main.py                          # Основний файл
├── tests.py                         # Юніт-тести
├── config.json                      # Налаштування
├── requirements.txt                 # Залежності
├── .env                             # Секрети (не в git!)
├── .env.example                     # Шаблон
├── monitor.log                      # Файловий лог
├── monitor.lock                     # Захист від подвійного запуску
├── <phone>.session                  # Telethon-сесія
└── docs/                            # GitHub Pages
    └── index.html
```

---

## Тести

```bash
cd /opt/tgmsgforwmonit
source .venv/bin/activate
python tests.py
```

**30 тестів, 0 помилок.** Покривають: `clean_minus_words`, `has_minus_word`, `find_keyword`, `format_sender`, `is_admin`, інтеграційні сценарії.

---

## .gitignore

```gitignore
.env
*.session
monitor.lock
monitor.log
__pycache__/
.venv/
```
