# llala-laucher

`llala-laucher` — автономный Windows GUI на Python 3.13 и `tkinter` для настройки, запуска и остановки `llama-server.exe` из llama.cpp. Для системного tray используется небольшой Windows-пакет `infi.systray`.

## Требования и структура

- Windows 10/11;
- Python 3.13 с `tkinter`;
- `infi.systray==0.1.12.1` из `requirements.txt`;
- совместимая Windows-сборка llama.cpp;
- Go 1.26 только для сборки отдельного MCP-бинарника;
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
├─ requirements.txt
├─ mcp/
│  ├─ go.mod / go.sum
│  ├─ cmd/web-mcp/
│  └─ web-mcp.exe             # отдельный production stdio MCP binary
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

Минимальная команда сборки на Windows:

```powershell
python -m PyInstaller --onefile --noconsole --name llala-laucher --icon=icon.ico --add-data "icon.ico;." llala-laucher.py
```

Полная воспроизводимая portable-сборка выполняется из PowerShell:

```powershell
.\build-portable.ps1
```

Скрипт требует Go 1.26, сначала собирает `mcp/web-mcp.exe` с
`CGO_ENABLED=0 GOOS=windows GOARCH=amd64`, затем собирает GUI через
PyInstaller и копирует MCP как отдельный соседний файл в `dist/mcp/`.
Только GUI собирается с `--onefile --noconsole`; Go MCP через PyInstaller не
собирается и не встраивается внутрь GUI.

В source-режиме выбирается `<project>/icon.ico`. В frozen-режиме сначала проверяется `icon.ico` рядом с EXE, затем data-файл `icon.ico` внутри `sys._MEIPASS`, создаваемый указанным `--add-data` (в Windows разделитель source/destination — `;`). Если standalone ICO отсутствует, и главное окно, и tray пытаются извлечь icon resource из `sys.executable`, добавленный параметром `--icon=icon.ico`; извлечённые Win32 handles освобождаются при завершении.

Каталоги `llama/`, `models/` и `preset/` остаются внешними и должны находиться рядом с frozen EXE согласно структуре выше.

Итоговый переносимый каталог имеет вид:

```text
llala-laucher.exe
llama/
  llama-server.exe
  *.dll
  models/
  preset/
mcp/
  web-mcp.exe
```

На компьютере конечного пользователя для `web-mcp.exe` не нужны Python, Go
toolchain или внешние runtime-библиотеки.

## Web search через SearXNG

Launcher не устанавливает и не запускает Docker или SearXNG. Укажите адрес уже
работающего экземпляра, например `http://127.0.0.1:8080` или
`http://192.168.1.50:8080`. В `settings.yml` SearXNG должен быть разрешён JSON
формат Search API, например:

```yaml
search:
  formats:
    - html
    - json
```

Под строками **Status** и **PID** находится checkbox **Web search (SearXNG)**.
Он непосредственно включает MCP-инструменты и сохраняется в исторически
названном `laucher-settings.json`. Кнопка с шестерёнкой открывает отдельное
неблокирующее окно настроек:

- **URL** — base URL SearXNG, по умолчанию `http://127.0.0.1:8080`;
- **Results** — количество результатов по умолчанию, `1–20` (по умолчанию `8`);
- **Timeout (s)** — HTTP timeout, `1–120` секунд (по умолчанию `15`);
- **Test connection** — в worker thread вызывает именно
  `/search?q=...&format=json`, проверяет HTTP status и структуру JSON Search API;
- **Save** — проверяет значения и сохраняет их в `laucher-settings.json`.

Редактирование полей и **Test connection** работают с черновыми значениями и не
перезаписывают конфигурацию. Изменения применяются только кнопкой **Save**;
закрытие окна отбрасывает несохранённые значения.

Поисковые движки выбирает и агрегирует SearXNG. Launcher и модель не требуют
выбора Google, Bing, Yandex или другого конкретного движка; поле `engines` из
ответа возвращается модели для прозрачности.

При включении функции единый генератор preview/реального argv добавляет
`--mcp-servers-json` в подтверждённом Cursor-compatible формате. SearXNG URL,
Results и Timeout передаются `mcp/web-mcp.exe` отдельными аргументами внутри
JSON, сформированного `json.dumps`; shell-строка и `shell=True` не используются.
Если `llama-server --help` не содержит `--mcp-servers-json`, preview и Start
покажут явную ошибку. В таком случае установите свежую совместимую сборку
llama.cpp и нажмите **Recheck CLI**. Отсутствующий или незапускаемый
`mcp/web-mcp.exe` также считается локальной ошибкой и проверяется до старта
llama-server.

`web-mcp.exe` — отдельный Go 1.26 stdio MCP server с инструментами:

- `web_search` обращается к официальному SearXNG API
  `GET /search?q=...&format=json`, нормализует и ограничивает выдачу;
- `web_fetch` читает `text/html`, `text/plain`, `application/json` и
  `application/pdf`, извлекает текст/Markdown без браузерного движка и помечает
  содержимое как внешнее и недоверенное.

Сам MCP объявляет точные имена `web_search` и `web_fetch`. Текущий llama.cpp
добавляет к ним namespace имени сервера из Cursor-конфигурации, поэтому в
`GET /tools` и во внутреннем WebUI они отображаются как
`web-search_web_search` и `web-search_web_fetch`; llama.cpp снимает prefix при
фактическом `tools/call` к MCP.

`web_fetch` не исполняет JavaScript, не обходит авторизацию, CAPTCHA или защиту
от автоматических запросов. Он ограничивает redirects, размер загрузки и размер
выходного текста. SSRF-защита проверяет схему/credentials, все DNS-адреса и
каждый redirect непосредственно перед запросом; loopback, private, link-local,
multicast и unspecified IPv4/IPv6 блокируются. Local/LAN адрес разрешён только
специализированному SearXNG-клиенту, не `web_fetch`.

Go MCP собирается отдельно:

```powershell
.\mcp\build.ps1
```

Бинарник намеренно собирается как обычный Windows console-subsystem executable,
без `-H=windowsgui`: этот флаг способен нарушить надёжный stdio. Видимое окно
не появляется, потому что текущий `llama-server` создаёт stdio MCP-процессы с
Windows `no_window`, а сам launcher запускает `llama-server` с
`CREATE_NO_WINDOW`. stdin/stdout при этом остаются pipes; stdout MCP содержит
только NDJSON JSON-RPC, диагностика направляется в stderr.

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
cd mcp
go test ./...
go vet ./...
```

Тесты покрывают пустой каталог моделей, рекурсивный scan, model ID, preset round-trip, отсутствие EXE, исключение disabled/unsupported аргументов, эквивалентность `original-bat.json` исходному BAT, queue-only tray callbacks, X/Open/Quit lifecycle, идемпотентный Quit, ожидание server `exit`, повторный `NIM_ADD` и source/frozen icon resolution.
