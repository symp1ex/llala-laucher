# llala-laucher

`llala-laucher` — автономный Windows GUI на Python 3.13 и `tkinter` для настройки, запуска и остановки `llama-server.exe` из llama.cpp. Для системного tray используется небольшой Windows-пакет `infi.systray`.

## Требования и структура

- Windows 10/11;
- Python 3.13 с `tkinter`;
- `infi.systray==0.1.12.1` из `requirements.txt`;
- `beautifulsoup4` для извлечения читаемого HTML и pure-Python `pypdf` для PDF;
- совместимая Windows-сборка llama.cpp;
- одна или несколько моделей GGUF.

Установка Python-зависимостей:

```powershell
python -m pip install -r requirements.txt
```

Рекомендуемая структура отделяет код launcher от бинарников llama.cpp:

```text
llala-laucher/
├─ llala-laucher.py
├─ app.py
├─ app_paths.py
├─ tray.py
├─ windows_integration.py
├─ llama_server.py
├─ model_scanner.py
├─ parameter_specs.py
├─ preset_manager.py
├─ server_process.py
├─ widgets.py
├─ web-mcp.py                 # отдельная stdio MCP точка входа
├─ web_mcp/                   # SearXNG, web_fetch и JSON-RPC protocol
├─ web_search_settings.py
├─ requirements.txt
├─ icon.ico                    # окно, taskbar, Alt+Tab и tray
├─ laucher-settings.json       # создается после закрытия
└─ llama/
   ├─ llama-server.exe
   ├─ *.dll                     # DLL из той же сборки llama.cpp
   ├─ models/
   │  ├─ Model-A/model.gguf
   │  └─ Model-B/model.gguf
   └─ preset/
      └─ <model-id>/*.json
```

В готовом переносимом комплекте рядом с GUI также находится отдельный MCP EXE:

```text
portable/
├─ llala-laucher.exe
├─ icon.ico                   # необязательный sidecar
├─ mcp/
│  └─ web-mcp.exe
└─ llama/
   ├─ llama-server.exe
   ├─ *.dll
   ├─ models/
   └─ preset/
```

При запуске из исходников все пути вычисляются от каталога launcher. В frozen EXE рабочим корнем становится каталог самого EXE. Текущий каталог процесса (`%CD%`) не используется.

На этапе разработки в `app_paths.py` задан временный fallback:

```text
F:\itt\llama\llama-b10282-bin-win-cuda-13.3-x64
```

Если локальный `llama/llama-server.exe` существует, он всегда имеет приоритет. После копирования сборки в локальный `llama/` удалите значение `DEVELOPMENT_LLAMA_ROOT` (установите `None`). При активном fallback каталоги `models` и `preset` также берутся из fallback-корня.

## Запуск

Из корня проекта:

```powershell
python llala-laucher.py
```

Можно также открыть `llala-laucher.py` двойным кликом, если `.py` связан с Python. Если EXE не найден, приложение продолжит работать, покажет `NOT FOUND`, а кнопка Start будет недоступна. Пустой или отсутствующий `models/` тоже не приводит к падению.

При обычном запуске одновременно открываются главное окно и системный tray:

- системная кнопка **X** только скрывает окно; launcher, polling и запущенный `llama-server` продолжают работать;
- **Open** в tray и двойной клик по иконке возвращают то же главное окно без запуска второго процесса;
- встроенный пункт **Quit** — единственный полный выход: он без confirmation-диалога асинхронно останавливает `llama-server`, дожидается события завершения, сохраняет settings, удаляет tray icon и закрывает tkinter.

Callbacks `infi.systray` выполняются в отдельном Windows message-loop thread. Они не обращаются к tkinter: `tray_open` и `tray_quit` кладутся в `LauncherApp.background_events` и обрабатываются UI thread существующим polling.

Tray регистрирует Unicode-сообщение `TaskbarCreated` и повторяет `NIM_ADD` после перезапуска Explorer. Если `NIM_MODIFY` сообщает об утраченной регистрации, выполняется новый `NIM_ADD`.

## Иконка и сборка PyInstaller

Основной файл `icon.ico` используется через `iconbitmap(default=...)` и `WM_SETICON` для titlebar, taskbar, Alt+Tab и thumbnail preview, а также для notification icon. До создания `tk.Tk()` процесс получает постоянный AppUserModelID `llala.launcher`.

Соберите GUI и MCP из корня проекта. MCP намеренно является console-subsystem EXE: его
stdin/stdout остаются рабочим MCP transport. При штатном запуске он наследует скрытый
процесс `llama-server`, который launcher создаёт с `CREATE_NO_WINDOW`, поэтому отдельное
консольное окно не появляется. Не собирайте MCP с `--noconsole`: в таком EXE стандартные
потоки могут быть недоступны.

```powershell
python -m PyInstaller --clean --noconfirm --onefile --noconsole --name llala-laucher --icon=icon.ico --add-data "icon.ico;." --distpath dist llala-laucher.py
python -m PyInstaller --clean --noconfirm --onefile --console --name web-mcp --distpath dist/mcp web-mcp.py
```

В source-режиме выбирается `<project>/icon.ico`. В frozen-режиме сначала проверяется `icon.ico` рядом с EXE, затем data-файл `icon.ico` внутри `sys._MEIPASS`, создаваемый указанным `--add-data` (в Windows разделитель source/destination — `;`). Если standalone ICO отсутствует, и главное окно, и tray пытаются извлечь icon resource из `sys.executable`, добавленный параметром `--icon=icon.ico`; извлечённые Win32 handles освобождаются при завершении.

Каталоги `llama/`, `models/` и `preset/` остаются внешними и должны находиться рядом с frozen EXE согласно структуре выше.

В source-режиме launcher запускает текущий `sys.executable` и абсолютный путь к
`web-mcp.py`. Во frozen-режиме используется только соседний `mcp/web-mcp.exe`.
Аргументы передаются списком без shell; inline MCP JSON строится `json.dumps`, поэтому
пробелы, обратные слеши и кавычки в Windows-путях не требуют ручного escaping.

## Web search через SearXNG

Launcher не устанавливает и не управляет Docker или SearXNG. В существующем экземпляре
SearXNG разрешите JSON output в `settings.yml` и перезапустите сам SearXNG обычным для
вашей установки способом:

```yaml
search:
  formats:
    - html
    - json
```

В launcher откройте компактный блок **Web search (SearXNG)**, укажите базовый URL,
например `http://127.0.0.1:8080` или `http://192.168.1.50:8080`, и нажмите
**Test connection**. Проверка выполняется в worker thread и действительно вызывает
`GET /search?...&format=json`; tkinter не блокируется. После успешной проверки включите
checkbox. Значения по умолчанию: функция выключена, 8 результатов и timeout 15 секунд.
Они сохраняются в `laucher-settings.json`; старый, частичный или повреждённый файл
загружается с безопасными defaults.

При запуске добавляется Cursor-compatible конфигурация:

```json
{"mcpServers":{"web":{"command":"...","args":["...","--searxng-url","http://127.0.0.1:8080","--max-results","8","--timeout","15"]}}}
```

Передача выполняется через `--mcp-servers-json`. Launcher ищет точное имя switch в
выводе `llama-server.exe --help`. Если поиск включён, а флаг отсутствует, help не удалось
прочитать или `mcp/web-mcp.exe` отсутствует, preview и Start показывают явную локальную
ошибку — настройка не отбрасывается молча. Для ручной диагностики выполните:

```powershell
llama\llama-server.exe --help | Select-String mcp-servers-json
```

Сетевой сбой SearXNG при старте не останавливает GUI или MCP: `web_search` возвращает
модели типизированную временную ошибку и может быть вызван повторно после восстановления
сети. Конкретные поисковые движки не выбираются launcher или моделью; включение,
агрегация и fallback движков полностью задаются конфигурацией SearXNG. В результатах
сохраняется поле `engines`, если его вернул SearXNG.

MCP предоставляет два инструмента:

- `web_search` — нормализованная и ограниченная выдача SearXNG с query, language,
  page (1–50), time range и категориями general/news;
- `web_fetch` — HTML, plain text, JSON и PDF без browser engine.

`web_fetch` разрешает только публичные HTTP(S) адреса без credentials, проверяет все
DNS-ответы до запроса и на каждом redirect, блокирует loopback, private, link-local,
multicast и unspecified адреса, ограничивает redirects, Content-Length, реально
прочитанные bytes и возвращаемый текст. HTML очищается от script/style/navigation и
boilerplate, PDF читается максимум по 50 страницам. Ответ явно маркируется как
external/untrusted content. JavaScript-only страницы, авторизация, CAPTCHA и browser
automation не поддерживаются и возвращают честную ошибку.

## Модели и model ID

Кнопка **Refresh models** рекурсивно находит каждый `*.gguf` в `llama/models`. В списке отображается относительный путь, а внутри хранится абсолютный `Path`.

Preset-каталог строится из относительного пути модели: читаемая безопасная для Windows часть плюс первые 10 символов SHA-256 от case-insensitive относительного пути. Например:

```text
models/Qwen3.6-35B-A3B-Q4_K_M.gguf
preset/Qwen3.6-35B-A3B-Q4_K_M--f6b98dc27b/
```

Хэш не дает разным путям конфликтовать после замены запрещенных Windows-символов.

## Параметры и presets

Каждый CLI-параметр имеет собственный checkbox. Выключенный параметр не передается независимо от значения в поле. **Clear** сохраняет выбранные model/preset и восстанавливает безопасный профиль:

```text
-m <model>
--host 127.0.0.1
--port 8080
-c 4096
-np 1
```

Кнопка **Load** явно переносит значения выбранного preset в UI. Checkbox **Start using selected preset** действует иначе: команда строится непосредственно из JSON, а текущие поля UI не изменяются. В этом режиме необходим существующий выбранный preset.

**Save preset** записывает все параметры как `{ "enabled": ..., "value": ... }` в schema version 1. Неизвестные параметры будущих версий при загрузке игнорируются с предупреждением в Server output. API key входит в preset только при явном сохранении пользователем; в `laucher-settings.json` он никогда не записывается.

Пример `original-bat.json` находится в:

```text
llama/preset/Qwen3.6-35B-A3B-Q4_K_M--f6b98dc27b/original-bat.json
```

## Совместимость CLI и MoE

При старте и по кнопке **Recheck CLI** launcher выполняет `llama-server.exe --help`. Известные switches ищутся как отдельные имена. Неподдерживаемый параметр остается видимым с пометкой, блокируется и не попадает в argv. Если `--help` прочитать нельзя, используется встроенный декларативный каталог из `parameter_specs.py`.

Вкладка **MoE** использует реально подтвержденные сборкой llama.cpp b10282 параметры:

- `--cpu-moe` оставляет все веса экспертов в RAM и экономит VRAM, обычно ценой скорости;
- `--n-cpu-moe N` оставляет на CPU экспертов первых N слоев;
- `--override-tensor` дает расширенное размещение тензоров по pattern/buffer и требует синтаксиса конкретной сборки.

Не включайте MoE offload автоматически: оптимальные значения зависят от архитектуры модели, RAM, GPU backend и объема VRAM.

## Память и безопасность запуска

Большой context увеличивает KV cache; `262144` может потребовать очень много RAM/VRAM. Квантизация `-ctk/-ctv` экономит память, но может влиять на качество и производительность. `--mlock`, `--no-mmap`, GPU split и MoE-параметры по умолчанию выключены.

Preview и реальный запуск используют одну функцию `build_command()`. В `subprocess.Popen` передается `list[str]`, `shell=False`; preview — только человекочитаемое отображение argv. Пока процесс запущен, второй экземпляр из этого launcher стартовать нельзя. На Windows `llama-server.exe` запускается с `CREATE_NO_WINDOW`, поэтому отдельное консольное окно не появляется, а stdout/stderr по-прежнему читаются в daemon-thread и передаются в tkinter через очередь. Stop выполняет `CTRL_BREAK_EVENT`, затем `terminate`, затем `kill`, не блокируя UI.

Кнопка **Open Web UI** становится активной после запуска и открывает фактический адрес сервера в браузере по умолчанию. Bind-адреса `0.0.0.0` и `::` для открытия заменяются на локальный `127.0.0.1`.

## Проверка исходников

```powershell
python -m compileall .
python -m unittest discover -s tests -v
```

Тесты также используют только локальный mock HTTP server и покрывают MCP argv/config,
source/frozen resolution, настройки, SearXNG query/error paths, HTML/plain/JSON/PDF,
лимиты, SSRF на исходном URL и redirect, MCP handshake/list/call и чистоту stdout.
