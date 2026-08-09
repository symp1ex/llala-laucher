# llala-launcher

`llala-launcher` — автономный Windows GUI на Python 3.13 и `tkinter` для настройки, запуска и остановки `llama-server.exe` из llama.cpp. Для системного tray используется небольшой Windows-пакет `infi.systray`.

## Требования и структура

- Windows 10/11;
- Python 3.13 с `tkinter`;
- `infi.systray==0.1.12.1` из `requirements.txt`;
- совместимая Windows-сборка llama.cpp;
- одна или несколько моделей GGUF.

Установка Python-зависимостей:

```powershell
python -m pip install -r requirements.txt
```

Рекомендуемая структура отделяет код launcher от бинарников llama.cpp:

```text
llala-launcher/
├─ llala-launcher.py
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
├─ icon.ico                    # окно, taskbar, Alt+Tab и tray
├─ launcher-settings.json       # создается после закрытия
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
python llala-launcher.py
```

Можно также открыть `llala-launcher.py` двойным кликом, если `.py` связан с Python. Если EXE не найден, приложение продолжит работать, покажет `NOT FOUND`, а кнопка Start будет недоступна. Пустой или отсутствующий `models/` тоже не приводит к падению.

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
python -m PyInstaller --onefile --noconsole --name llala-launcher --icon=icon.ico --add-data "icon.ico;." llala-launcher.py
```

В source-режиме выбирается `<project>/icon.ico`. В frozen-режиме сначала проверяется `icon.ico` рядом с EXE, затем data-файл `icon.ico` внутри `sys._MEIPASS`, создаваемый указанным `--add-data` (в Windows разделитель source/destination — `;`). Если standalone ICO отсутствует, и главное окно, и tray пытаются извлечь icon resource из `sys.executable`, добавленный параметром `--icon=icon.ico`; извлечённые Win32 handles освобождаются при завершении.

Каталоги `llama/`, `models/` и `preset/` остаются внешними и должны находиться рядом с frozen EXE согласно структуре выше.

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

**Save preset** записывает все параметры как `{ "enabled": ..., "value": ... }` в schema version 1. Неизвестные параметры будущих версий при загрузке игнорируются с предупреждением в Server output. API key входит в preset только при явном сохранении пользователем; в `launcher-settings.json` он никогда не записывается.

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

Preview и реальный запуск используют одну функцию `build_command()`. В `subprocess.Popen` передается `list[str]`, `shell=False`; preview — только человекочитаемое отображение argv. Пока процесс запущен, второй экземпляр из этого launcher стартовать нельзя. Stdout/stderr читаются в daemon-thread и передаются в tkinter через очередь. Stop выполняет `CTRL_BREAK_EVENT`, затем `terminate`, затем `kill`, не блокируя UI.

Кнопка **Open Web UI** становится активной после запуска и открывает фактический адрес сервера в браузере по умолчанию. Bind-адреса `0.0.0.0` и `::` для открытия заменяются на локальный `127.0.0.1`.

## Проверка исходников

```powershell
python -m compileall .
python -m unittest discover -v
```

Тесты покрывают пустой каталог моделей, рекурсивный scan, model ID, preset round-trip, отсутствие EXE, исключение disabled/unsupported аргументов, эквивалентность `original-bat.json` исходному BAT, queue-only tray callbacks, X/Open/Quit lifecycle, идемпотентный Quit, ожидание server `exit`, повторный `NIM_ADD` и source/frozen icon resolution.
