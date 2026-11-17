# Координатор Qwen в DeepSeek Harness (DSH)

DeepSeek Team поддерживает три координатора: **Codex**, **Claude Code** и
**Qwen** — модель Qwen (например, Qwen 4 Max) в роли кодинг-агента внутри
[DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness).
Это руководство описывает только интеграцию Qwen/DSH; контракт делегирования,
маршрутизация, изоляция воркеров и жизненный цикл одинаковы для всех координаторов
и описаны в [README](../README.ru.md) и [руководстве по маршрутизации](ROUTING.ru.md).

## Архитектура

DSH — хост плагинов на основе cordis, а не CLI с файлом хуков в стиле
Codex/Claude. Поэтому пакет поставляет **bridge-плагин**
(`codex_deepseek_team/data/dsh-bridge/index.js`, ESM без зависимостей) и
устанавливает одну принадлежащую пакету вставленную строку плагина в
`cordis.patch.yml` активного профиля DSH:

```yaml
# deepseek-team:qwen:start
- insert:
    - id: deepseek-team-qwen
      name: "/abs/path/to/codex_deepseek_team/data/dsh-bridge/index.js"
      config:
        entrypoint: "/abs/path/to/deepseek-team"
        runtime: qwen
# deepseek-team:qwen:end
```

DSH применяет строку патча, у которой есть `id`, но нет `insert`, как
**обновление** существующей записи, поэтому новый плагин нужно вставлять явно:
принадлежащий блок — это один патч `insert:`, добавляющий строку bridge, где
`name` — абсолютный путь к поставляемому модулю bridge в файловой системе.
pnpm, сеть и правка `package.json` не нужны. Все байты вне блока маркеров
сохраняются; запись атомарна и проверяет конкурентные изменения,
символические ссылки и нерегулярные файлы патча отклоняются. Чужая строка с id
`deepseek-team-qwen` вне маркеров диагностируется и никогда не принимается.

Bridge привязывается **только к корневым агентам** и транслирует события DSH в
вызовы `deepseek-team coordinator-hook --runtime qwen` (JSON в stdin,
ограниченные stdout/stderr, таймаут 10 с по умолчанию):

| Событие DSH | Событие хука | Применяемое решение |
|---|---|---|
| `agent/created` (`startup`/`resume`/`clear`/`compact`) | `SessionStart` | `additionalContext` через `agent.inject()` |
| `agent/pre-step` с новым сообщением `source.kind === 'user'` | `UserPromptSubmit` | контекст добавляется в допущенный батч шага; повторные id сообщений игнорируются |
| `tools/pre-execute` | `PreToolUse` | `permissionDecision: deny` → `{kind:'deny'}`; подсказка early-planning доставляется после вызова через `tools/post-execute` `additionalContexts` |
| `agent/turn-stopping` | `Stop` | `decision: block` → `agent.steer(reason)` продолжает turn; повторный stop (`stop_hook_active`) только предупреждает |

Нативные субагенты DSH, форки и участники команды — дочерние агенты (принадлежность
в реестре или происхождение в заголовке сессии): они никогда не привязываются и
не могут открыть или завершить ledger координатора. Их запуск из корневого агента
по-прежнему проходит гейт нативного делегирования и требует зарегистрированного
маркера `[deepseek-team:TASK_ID:DELIVERABLE_ID]`.

### Семантика fail-closed

- Ошибка или таймаут хука `PreToolUse` **отклоняет вызов инструмента**
  (паритет с Claude).
- Ошибка хука `UserPromptSubmit` никогда не отклоняет шаг: reject в DSH
  pre-step забрал бы пользовательский запрос без обработки. Вместо этого сессия
  помечается *ledger-down*: все вызовы инструментов отклоняются, пока следующий
  запрос снова не инициализирует ledger; модель видит диагностическое сообщение.
- Ошибки хуков `SessionStart` и `Stop` не блокируют.
- Bridge не читает учётные данные и не логирует payload.

### Ограничения покрытия

Bridge покрывает события, которые DSH действительно доставляет: распознанные
чтения/правки/записи, мутации bash и `terminal_send`, а также инструменты
нативного делегирования (`subagent`, `subagent_fork`, `review_code`,
`spawn_teammate`, `send_message`, `workflow`, `ralph`). Это **не** универсальный
слой перехвата: произвольная активность плагинов вне этих событий не
контролируется, и там авторитетными остаются инструкции. В plan mode DSH нет
исключения для нативных plan-файлов: мутации остаются под гейтом (их также
блокирует сам harness). Обязательная песочница ОС для воркеров не меняется.

## Воркеры: нативный цикл DeepSeek (Codex/Claude не нужны)

Координатор Qwen работает внутри DSH; DSH не является запускаемым worker-CLI.
Поэтому пакет поставляет собственный **нативный worker-runtime**
(`--runtime qwen`): самодостаточный Python-цикл на одной только стандартной
библиотеке (`native_worker.py`), который обращается к DeepSeek Responses API
напрямую. Связка Qwen + DeepSeek работает **без установленных Codex CLI и
Claude Code CLI**.

- Read-only воркеры используют `read_file`, `glob_files` и `grep_search` в
  пределах checkout (защита путей и учётных данных) и работают внутри той же
  границы Bubblewrap `sandbox.wrap_command`, что и воркеры Claude.
- Full-access воркеры добавляют `write_file`, `edit_file` и `run_shell` внутри
  управляемой копии разработки. Управляемая граница копирует однофайловый цикл
  в приватный каталог control и запускает его через `python -I` в разреженном
  пространстве имён с изолированной сетью; трафик провайдера идёт только через
  фиксированный relay `api.deepseek.com`, точно как у CLI-harness.
- Цикл выдаёт один result-объект в форме Claude, поэтому разбор результата,
  проверки, capture, жизненный цикл и журнал одинаковы для всех runtime.
  Реализация никогда не повторяется автоматически; transient-повторы read-only
  принадлежат внешнему раннеру.
- `worker --runtime auto` пробует Codex, затем Claude Code, затем нативный цикл
  qwen — существующие координаторы тоже получают fallback без CLI. `--qwen`
  явно фиксирует интерпретатор (по умолчанию — текущий интерпретатор).

Модель и авторизация самого координатора (например, `qwen4-max`) полностью
остаются в вашей конфигурации DSH — пакет их не читает и не меняет.

## Установка

```bash
# 1. Подготовить bridge для обнаруженного профиля DSH.
deepseek-team setup --runtime qwen --no-key
# 2. Сохранить ключ DeepSeek для воркеров (скрытый ввод; ключ не печатается).
deepseek-team auth set
# 3. Подключить проект. Qwen делит управляемый блок AGENTS.md с Codex.
deepseek-team init --coordinator qwen /path/to/project
# 4. Опционально: разрешить реализацию в изолированной копии разработки.
deepseek-team config set --project --access full-access
# 5. Офлайн-проверка готовности.
deepseek-team doctor --runtime qwen --offline
```

Выбор профиля: `DEEPSEEK_TEAM_DSH_PROFILE=<name>` выбирает профиль в
`$DSH_HOME/profiles` (по умолчанию `~/.dsh`); без переменной используется
единственный существующий профиль. Несколько профилей — это понятная ошибка,
а не угадывание.

**Загрузка плагина.** Поставляемый профиль `web` перечитывает изменения
конфигурации через host-HMR; остальные профили применяют патчи при старте.
После `hooks install --runtime qwen` перезагрузите профиль или перезапустите
DSH, затем начните новую сессию. Проверка:

```bash
deepseek-team hooks status --runtime qwen
```

Пакет не может проверить или подтвердить нативную загрузку профиля;
подтверждение остаётся за вами, пока вы не перезагрузите/перезапустите DSH и не
начнёте новую сессию.

## Общий блок AGENTS.md

DSH нативно читает `AGENTS.md` (и `CLAUDE.md`), поэтому Qwen делит управляемый
блок `AGENTS.md` с Codex: один файл — один блок. `init --coordinator qwen`
рендерит его для Qwen; последующее подключение Codex перерендеривает его для
Codex, а `--coordinator all` рендерит общий файл один раз для первого выбранного
координатора (Codex). Это безопасно: хуки каждого координатора инжектируют
полную актуальную политику для своего runtime при старте сессии и при каждом
запросе — снимок общего файла никогда не управляет сессией единолично. `both`
сохраняет историческое значение (Codex + Claude Code); `all` добавляет Qwen.

## Обновление, refresh и удаление

```bash
deepseek-team hooks refresh --runtime qwen --notify --json   # после обновления пакета
deepseek-team hooks remove --runtime qwen                    # только свой bridge-блок
deepseek-team reset --runtime qwen                           # то же через reset
deepseek-team detach --coordinator qwen /path/to/project     # только управляемый блок
```

Refresh мигрирует только принадлежащий блок маркеров (включая устаревший
entrypoint, записанный старой установкой, и строку в прежней «голой» форме без
`insert:`) и сообщает о перезагрузке/перезапуске профиля как о действии,
ожидающем пользователя. Чужие строки патча, бандлы, учётные данные и настройки
DSH сохраняются.

## Тесты

- Python: `tests/test_qwen_registration.py`, `tests/test_qwen_hooks.py`,
  `tests/test_qwen_cli.py`, `tests/test_qwen_native_worker.py` (часть стандартного
  unittest discovery). Набор нативного воркера включает реальные managed-прогоны
  цикла в песочнице против локального фикстурного провайдера — публичный endpoint
  и настоящие учётные данные не используются.
- Bridge: `node --test tests/dsh_bridge/bridge.test.mjs` — фейковый cordis-контекст
  и фикстурный hook-entrypoint; установка DSH не требуется.
  `tests/test_qwen_bridge.py` запускает его из unittest discovery, когда Node есть
  в PATH или `DEEPSEEK_TEAM_NODE` указывает на исполняемый Node; иначе пропускает.