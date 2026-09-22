# DeepSeek Team

[English](README.md) | **Русский**

Один Linux-пакет для **координаторов Codex и/или Claude Code**, которые делегируют ограниченные задачи по разработке воркерам DeepSeek. Текущая версия поддерживает настраиваемые **профили делегирования 25/50/75%**, независимую политику `read-only/full-access`, переиспользуемые изолированные рабочие копии и устаревший режим точечной записи в заранее разрешённые файлы.

**Координатор отвечает за:** границы задачи, архитектуру, решения по безопасности, финальную проверку diff, интеграцию, коммиты и действия в production. **DeepSeek выполняет:** сфокусированные исследования/ревью, а при выбранном full-access — независимую реализацию, локальные тесты/сборки и документацию внутри отдельной рабочей копии. Воркеры DeepSeek всегда используют `deepseek-flash`; effort по умолчанию равен `auto`, поэтому frontier-координатор выбирает `low`/`medium`/`high` отдельно для каждого задания, если только не настроено принудительное сохраняемое значение effort. Codex/Claude также могут использовать собственных нативных субагентов, если могут указать конкретную причину; нативные субагенты дополняют, а не заменяют обязательные задания DeepSeek. Координатор должен проверять полезные результаты, а не автоматически повторять всё делегированное исследование или переписывать корректный код.

Проценты — это **целевые профили распределения работы**, а не измеряемые квоты по токенам/времени/строкам и не обещание точной полезной доли вклада. Маленькие или неделимые задачи могут делегироваться меньше. Без новых настроек сохраняется совместимость: по умолчанию используется **25% + access=auto → read-only**.

Версия **0.6.0** добавляет выбор effort для DeepSeek со стороны frontier-координатора с сохраняемой политикой `auto|low|medium|high`, сохраняет возможность обоснованно использовать нативных субагентов Codex/Claude параллельно с воркерами DeepSeek и оставляет DeepSeek-воркеров изолированными leaf-worker'ами на `deepseek-flash`. Версия 0.5.0 добавила сохраняемое состояние координации и контроль жизненного цикла Codex. Релиз поддерживает **Linux, Python 3.11+, Git, Bubblewrap и Codex CLI и/или Claude Code CLI**. Для Ubuntu предусмотрена штатная настройка AppArmor под ограничение unprivileged user namespaces. Для реальной работы требуется API-ключ DeepSeek. Python runtime-зависимостей нет; Bubblewrap/AppArmor являются системными компонентами.

## Установка в Ubuntu — рекомендуемый вариант

Установите CLI координатора/координаторов, которые собираетесь использовать, затем:

~~~bash
git clone https://github.com/kirill31337/deepseek-team.git
cd deepseek-team
python3 install.py --with-sandbox
export PATH="$HOME/.local/bin:$PATH"
deepseek-team sandbox status
~~~

`--with-sandbox` — это **явный привилегированный путь настройки**. В Ubuntu он устанавливает пакеты `bubblewrap` и `apparmor`, устанавливает/перезагружает принадлежащий пакету именованный профиль `deepseek-team-bwrap` и проверяет получившийся sandbox. Он **не** отключает AppArmor и **не** изменяет `kernel.apparmor_restrict_unprivileged_userns`.

Далее настройте нужного координатора:

~~~bash
# Только Codex
deepseek-team setup --runtime codex
deepseek-team hooks status
deepseek-team doctor --runtime codex --offline
deepseek-team init --coordinator codex
# В Codex один раз проверьте/доверьте стабильному hook через /hooks.

# Только Claude Code
deepseek-team setup --runtime claude
deepseek-team doctor --runtime claude --offline
deepseek-team init --coordinator claude

# Или оба
deepseek-team setup --runtime both
deepseek-team doctor --runtime both --offline
deepseek-team init --coordinator both
~~~

Установщик создаёт отдельный venv в `~/.local/share/codex-deepseek-team/venv` и публикует две эквивалентные команды:

~~~text
deepseek-team
codex-deepseek-team   # legacy-алиас для совместимости
~~~

Старое имя Python-дистрибутива/namespace намеренно сохраняется, чтобы существующие установки и автоматизация продолжали работать.

### Lifecycle hooks Codex

Начиная с **0.5.0**, стандартный установщик обнаруживает Codex в `PATH` и устанавливает либо обновляет стабильные пользовательские lifecycle hooks DeepSeek Team в `$CODEX_HOME/hooks.json`. Команда `deepseek-team setup --runtime codex` устанавливает ту же hook-конфигурацию, сохраняя основную модель Codex и существующую OpenAI-аутентификацию.

Полезные команды:

~~~bash
deepseek-team hooks install
deepseek-team hooks status
deepseek-team hooks remove
~~~

Пользовательский hook намеренно ничего не делает в посторонних репозиториях. Контроль координатора активируется только в репозитории, который явно подключён командой:

~~~bash
deepseek-team init --coordinator codex /path/to/project
~~~

Нативным доверием к hooks управляет Codex. Один раз проверьте/доверьте стабильному hook DeepSeek Team через `/hooks` в Codex; DeepSeek Team не обходит и не пытается угадывать это решение. Обычные обновления пакета сохраняют ту же hook-команду и **не требуют повторного запуска `init` для уже подключённых проектов**.

`hooks remove` удаляет только обработчики DeepSeek Team, принадлежащие пакету, и сохраняет посторонние пользовательские hooks.

## Установка с помощью промпта для агента

Можно попросить Codex установить или обновить DeepSeek Team в репозитории, с которым он сейчас работает. Вставьте следующий короткий промпт в Codex из проекта, который хотите подключить.

> Промпт намеренно оставлен на английском как каноническая исполняемая инструкция, чтобы русская и английская документация не расходились по поведению.

~~~text
Install or update DeepSeek Team in this Linux project from https://github.com/kirill31337/deepseek-team. Use the repository's standard install.py; on Ubuntu use --with-sandbox unless a working DeepSeek Team sandbox is already configured. Preserve my existing Codex model/auth, DeepSeek Team settings, and credential. Configure Codex support without asking me to paste secrets into this prompt; if no DeepSeek key exists, leave secret entry to "deepseek-team auth set". Ensure the stable Codex lifecycle hooks are installed and healthy with "deepseek-team hooks install" and "deepseek-team hooks status". If this repository does not already contain the DeepSeek Team managed block in AGENTS.md, attach it with "deepseek-team init --coordinator codex ."; if it is already attached, do not re-run init just because the package was updated. Verify "deepseek-team --version", "deepseek-team sandbox status", "deepseek-team hooks status", and "deepseek-team doctor --runtime codex --offline". Do not use --os-sandbox off and do not weaken AppArmor/Bubblewrap. If the hooks require native Codex trust/review, use the Codex "/hooks" interface and approve/trust the DeepSeek Team package-owned hooks yourself when the current Codex environment permits it. Do not bypass Codex hook trust or modify trust state outside the native Codex mechanism. Afterwards verify with "deepseek-team hooks status". Only ask me to approve the hooks manually if native approval cannot be completed from the current Codex session.
~~~

Промпт намеренно **не содержит API-ключ** и не меняет ваш уровень делегирования, политику доступа или сохранённую политику effort. Приватный DeepSeek-ключ настраивайте отдельно через `deepseek-team auth set`, а `delegation_level` / `access` / `effort` задавайте явно, если нужны значения, отличные от текущей конфигурации или значений по умолчанию.

### Rootless/ручная установка

Обычная установка никогда не вызывает `sudo`:

~~~bash
python3 install.py
export PATH="$HOME/.local/bin:$PATH"
deepseek-team sandbox status
~~~

Если `sandbox status` проходит успешно, менять AppArmor не нужно. Если Ubuntu блокирует Bubblewrap при `kernel.apparmor_restrict_unprivileged_userns=1`, установите профиль пакета явно:

~~~bash
deepseek-team sandbox install-apparmor
deepseek-team sandbox status
~~~

или повторно запустите `python3 install.py --with-sandbox`.

В других Linux-дистрибутивах установите Bubblewrap через пакетный менеджер дистрибутива и выполните `deepseek-team sandbox status`. Профиль AppArmor из пакета предназначен именно для Ubuntu/AppArmor mediation пользовательских namespace.

**Не исправляйте ошибки Ubuntu командой** `sudo sysctl -w kernel.apparmor_restrict_unprivileged_userns=0`. DeepSeek Team намеренно оставляет ограничение хоста включённым и разрешает создание user namespace только через свой именованный AppArmor-путь, когда это требуется.

В качестве альтернативы можно использовать `pipx install .` или venv, но до запуска воркеров всё равно должен работать системный Bubblewrap backend.

## Зачем AppArmor + Bubblewrap

Ubuntu может запрещать непривилегированным приложениям доступ к пользовательским namespace, если профиль AppArmor явно это не разрешает. Поэтому DeepSeek Team поставляет следующий **именованный, не привязанный автоматически** профиль:

~~~text
profile deepseek-team-bwrap flags=(unconfined) {
  userns,
}
~~~

Он выбирается явно через `aa-exec -p deepseek-team-bwrap -- ...`. Профиль не привязывается глобально к `/usr/bin/bwrap`, поэтому не заменяет и не конфликтует с профилями дистрибутива или администратора. Его задача — только разрешить создание исходного user namespace; **фактическую политику изоляции файловой системы и процессов определяет Bubblewrap**.

Запуск воркера по умолчанию работает по принципу fail-closed:

1. найти `bwrap` и проверить необходимые опции;
2. выполнить прямую проверку user namespace;
3. если Ubuntu AppArmor блокирует прямой Bubblewrap и ограничение активно, повторить через `aa-exec -p deepseek-team-bwrap`;
4. если оба пути не работают, остановиться **до чтения DeepSeek-ключа**.

Автоматического fallback в режим без sandbox нет.

## Режимы изоляции

Существующие read-only и legacy exact-file writer режимы сохраняют гибридную границу, зависящую от runtime. Управляемые рабочие копии нового full-access режима используют более строгую разреженную внешнюю Bubblewrap-границу для **обоих** runtime.

### Legacy/read-only граница runtime

### Воркер Codex

Текущий Codex в Linux уже имеет собственный Bubblewrap-backed sandbox `read-only` / `workspace-write`. Если вложить Codex в ещё один Bubblewrap namespace и запретить создание дополнительных user namespace, его нативный sandbox сломается.

Поэтому DeepSeek Team:

- проверяет доступный системный/AppArmor-aware Bubblewrap backend до чтения credentials воркера;
- создаёт приватный временный shim `bwrap` внутри worker-сессии;
- ставит этот shim первым в `PATH` воркера;
- позволяет **самому Codex** создать обычный Linux sandbox через проверенный путь `bwrap`;
- сохраняет существующую политику Codex `read-only` / `workspace-write` и сетевые ограничения writer;
- запускает общий Git-верификатор `WriteScope` перед принятием результата writer.

Родительские Codex auth/history/rules/plugins/apps/memories не копируются; воркер получает временные `HOME`/`CODEX_HOME` и только необходимую конфигурацию DeepSeek provider.

### Воркер Claude Code

Claude Code не предоставляет аналогичную нативную Linux Bubblewrap-границу для встроенных файловых инструментов, поэтому DeepSeek Team запускает весь изолированный harness Claude внутри **внешнего Bubblewrap namespace**:

- корневая файловая система read-only;
- новые process/user/IPC/UTS namespace;
- сброшенные capabilities;
- приватные `/tmp` и `/var/tmp`;
- настоящий HOME пользователя скрыт, а необходимые runtime-пути для запуска CLI повторно доступны только на чтение;
- распространённые credential stores снова маскируются после runtime-mounts;
- временный HOME воркера доступен на запись;
- repository/worktree монтируется read-only для review либо read-write для writer;
- `--disable-userns` запрещает worker payload создавать ещё один user namespace.

Сам Claude по-прежнему запускается с `--bare` без сохранения сессии. Встроенные инструменты ограничены `Read,Glob,Grep` для review и `Read,Glob,Grep,Edit,Write` для writer; Bash, web-инструменты и agents отсутствуют, MCP-инструменты явно запрещены, а правила approval для writer ограничены путями вида `Edit(./exact/file)`.

### Граница managed full-access

Управляемые development-копии — не legacy writer sandbox. И для Codex, и для Claude DeepSeek Team окружает runtime разреженным Bubblewrap namespace, который открывает только принадлежащую пакету рабочую копию, необходимые runtime-префиксы, временный HOME и per-run control directory. Административные Git-файлы перемонтируются read-only. Namespace использует `--unshare-net`; воркер не может обращаться к произвольным сервисам хоста или сети.

Доступ к DeepSeek API предоставляется только через host-relay с фиксированным назначением, подключённый к namespace через per-run Unix socket и namespace-local loopback bridge. Настоящий provider credential остаётся в relay на стороне хоста; изолированный runtime видит только синтетический локальный credential. Relay не является универсальным proxy и принимает только provider endpoints, необходимые поддерживаемым runtime-протоколам.

Таким образом, full-access означает **полный доступ разработчика к назначенной копии**, а не полный доступ к хосту. Он не открывает другие checkout пользователя, dirty source checkout, secrets, production-базы, системные сервисы, deployment credentials, публикацию или Git integration/commits.

### Legacy-сетевая граница

Legacy-путь Claude для review/exact-file всё ещё должен обращаться к DeepSeek напрямую, поэтому старый внешний Claude sandbox намеренно **не** делает unshare network namespace. Сетевые model tools при этом исключены из Claude tool surface, а legacy Codex сохраняет собственную sandbox/network политику.

## DeepSeek credential

Оба runtime используют один приватный DeepSeek credential:

~~~bash
deepseek-team auth set       # скрытый ввод в терминале
deepseek-team auth status    # только наличие/отсутствие
~~~

Сохранённый ключ находится в `~/.config/codex-deepseek/api-key`, права каталога `700`, файла `600`. `DEEPSEEK_API_KEY` имеет приоритет. Для автоматизации передавайте секрет из secret manager в `deepseek-team auth set --stdin`. Никогда не размещайте ключ в аргументах команд, файлах репозитория или worker prompts.

## Профили делегирования и доступ

Настройки накладываются независимо друг от друга:

`CLI arguments > project settings > global settings > defaults`.

| Профиль | `access=auto` | Предполагаемый режим |
| --- | --- | --- |
| **25%** | `read-only` | Ограниченное исследование, диагностика и ревью; основную реализацию выполняет координатор. |
| **50%** | `full-access` | Делегируйте независимые части реализации и их тесты до выполнения той же работы локально; архитектура/интерфейсы и интеграция остаются у координатора. |
| **75%** | `full-access` | Делегируйте большую часть отделимой реализации, тестов, документации и независимого ревью; используйте до трёх воркеров только когда задания действительно независимы. |

Доступ не зависит от целевого процента. Явный `read-only` остаётся read-only при 50/75, а явный `full-access` можно выбрать и при 25. Изменение процента не перезаписывает явно выбранный access; верните access в `auto`, если снова хотите использовать профильные значения по умолчанию.

### Модель DeepSeek, effort и нативные субагенты координатора

Сейчас DeepSeek Team направляет каждый управляемый воркер на **`deepseek-flash`**. Намеренно отсутствует роутер между Flash/Pro. Сохраняемая политика effort по умолчанию — **`auto`**. В `auto` frontier-координатор Codex/Claude выбирает `--effort low|medium|high` для каждого задания DeepSeek: `low` для ограниченной/механической работы или широких сканирований, `medium` для обычного случая и `high` для сложной отладки, межфайлового reasoning или требовательного независимого review. Если прямой запуск воркера доходит до runner без конкретного выбора frontier-координатора, `medium` используется только как execution fallback.

Основной координатор Codex/Claude сохраняет возможность нативных субагентов. Для существенной работы deliverable нативного субагента представлен в coordination plan как `"executor": "native-agent"` с конкретным `"delegation_reason"`. Нативные субагенты полезны для реально параллельной работы, изолированного контекста или возможностей родного runtime, но они **не засчитываются** как DeepSeek worker assignment, обязательный для профилей 50/75. Защищённые обязанности координатора остаются у координатора. У самих DeepSeek-воркеров инструменты agents/delegation отключены — они остаются leaf workers.

Примеры:

~~~bash
# Политика проекта
deepseek-team config set --project --delegation-level 50 --access auto

# Политика пользователя
deepseek-team config set --global --delegation-level 25 --access read-only

# Эффективные значения и источник каждого поля
deepseek-team config show --effective
deepseek-team config show --effective --json

# Зафиксировать effort для проекта (frontier больше не выбирает его для каждого задания)
deepseek-team config set --project --effort high

# Или задать его глобально
deepseek-team config set --global --effort high

# Вернуть автоматический выбор frontier-координатором
deepseek-team config set --project --effort auto

# Override для одного задания
deepseek-team worker --runtime codex --delegation-level 75 --access full-access --effort high
~~~

Настройки проекта находятся в `.deepseek-team.toml`; глобальные — в XDG config directory пользователя. Для `effort` действует тот же приоритет, что и для других полей политики: one-job CLI override > project > global > default (`auto`). Настройки фиксируются snapshot'ом при старте нового job и не меняют уже запущенный процесс.

`config show --effective --instructions --runtime codex|claude` выводит актуальные инструкции координатора. Управляемые блоки AGENTS.md/CLAUDE.md указывают координатору разрешать текущую политику перед каждым заданием, а не полагаться на устаревший процент, записанный в файл.

`doctor` разрешает и выводит ту же эффективную политику. При effective access=full-access он проверяет реальную surface capability управляемого runtime и отказывается валидировать full-access с `--os-sandbox off`.

## Контроль процесса координатора

DeepSeek Team 0.5.0 добавляет небольшой сохраняемый coordination ledger вне репозитория. Он записывает session/task ids, deliverables, worker assignments, workspace ids, объявленные dependencies/checks, worker-only file deltas, результаты, dispositions и технические ограничения. Это намеренно **не** scheduler и не project-management system.

Для Codex `deepseek-team setup --runtime codex` и стандартный installer размещают одно стабильное определение user-level lifecycle hook в `$CODEX_HOME/hooks.json`. Hook ничего не делает, пока текущий репозиторий явно не подключён через `deepseek-team init --coordinator codex`. Нативным доверием к hook управляет Codex: один раз проверьте/доверьте его через Codex `/hooks`. DeepSeek Team не обходит и не угадывает это решение.

Поддерживаемое поведение hooks Codex намеренно разделено:

- `SessionStart` и `UserPromptSubmit` восстанавливают/инжектируют текущее coordination state, в том числе после compaction;
- `PreToolUse` технически может запретить изменение исходников координатором до выполнения, если распределение отсутствует/не соответствует правилам, новый scope не был запланирован или путь всё ещё принадлежит ожидающему worker assignment;
- `Stop` предотвращает тихое завершение, пока задания ожидают выполнения либо завершённые worker results не получили disposition.

При **75/full-access** обычная отделимая реализация, тесты, fixtures, документация и несекретные metadata по умолчанию считаются worker-eligible. Сам факт запуска одного implementation/review worker не удовлетворяет профиль, если координатор затем оставляет всю оставшуюся worker-eligible работу себе без поддерживаемого ограничения. При **50/full-access** review-only worker не заменяет делегирование доступной части implementation/test/docs. Access остаётся независимым: явный read-only override никогда не становится writable.

До изменения исходников в существенной задаче Codex координатор регистрирует конкретные deliverables:

~~~bash
deepseek-team coordination plan --task TASK_ID <<'JSON'
{
  "classification": "substantial",
  "deliverables": [
    {
      "id": "implementation",
      "kind": "implementation",
      "scope": ["src/example.py"],
      "executor": "worker",
      "acceptance": ["focused behavior implemented"],
      "dependencies": [{"kind": "command", "value": "python3"}],
      "checks": ["python3 -m unittest tests.test_example -q"]
    }
  ]
}
JSON
~~~

Assignment, возвращённый планом, привязывается к runner:

~~~bash
deepseek-team worker --runtime codex --effort medium \
  --coord-task TASK_ID --coord-assignment ASSIGNMENT_ID <<'TASK'
Implement the assigned deliverable and satisfy its registered acceptance criteria.
TASK
~~~

Старт/завершение runner, workspace id, worker-only delta и объявленные checks записываются автоматически. После проверки координатором:

~~~bash
deepseek-team coordination use --task TASK_ID --assignment ASSIGNMENT_ID \
  --disposition incorporated --evidence "reviewed diff and accepted result"
~~~

Если assignment требует выбранных незакоммиченных исходников, импортируйте только необходимые файлы:

~~~bash
deepseek-team workspace import WORKSPACE_ID --include path/to/needed.py
~~~

Эти файлы записываются как подготовленный координатором input, а не как авторство воркера. Объявленные dependencies также проверяются **внутри реального worker sandbox до чтения provider credential**. Наличие JDK/SDK/tools только на хосте не предполагается для full-access. Недостающие зависимости нужно явно подготовить через `workspace prepare`; подмена их stubs не считается эквивалентной проверкой.

Для Claude Code доступны тот же ledger/runner accounting и управляемые инструкции, но enforcement распределения работы координатора в 0.5.0 **основан на инструкциях**. DeepSeek Team не заявляет о техническом Claude PreToolUse gate. Оба координатора могут использовать обоснованных нативных субагентов; такие агенты используют модель/permissions/sandbox родного host runtime и находятся вне DeepSeek worker sandbox.

Значение 25/50/75 остаётся **целевой политикой, а не измеренным процентом продуктивности**. DeepSeek Team записывает наблюдаемые факты; он не превращает число вызовов, файлов, строк, токенов, пунктов задачи или субъективные результаты в искусственную метрику «actual contribution %».

## Интеграция с проектом

`init` управляет одним размеченным блоком инструкций в нативном для координатора файле:

- Codex: `AGENTS.md`
- Claude Code: `CLAUDE.md`
- `--coordinator both`: оба файла

Байты вне управляемого блока и permissions файла сохраняются. Управляемые инструкции требуют OS sandbox и явно говорят координатору **не** добавлять `--os-sandbox off`; если sandbox недоступен, его нужно исправить или продолжить локально. При 50/75 full-access руководство явно требует делегировать независимую часть реализации **до** того, как координатор самостоятельно реализует ту же часть; архитектура, финальная проверка и интеграция остаются у координатора.

Изменение настроек проекта обновляет принадлежащие пакету управляемые блоки, если они присутствуют, не затрагивая окружающий пользовательский текст. Блок по-прежнему разрешает текущую политику перед каждым новым assignment.

Начиная с **0.5.0+**, обычное обновление пакета **не требует** повторного `init` для уже подключённого проекта. Стабильный user-level Codex hook обновляется installer/setup-путём, а существующий managed project block остаётся маркером активации. Запускайте `init` только при подключении нового репозитория, включении дополнительного координатора или намеренном восстановлении managed block после detach.

## Read-only делегирование

Codex:

~~~bash
deepseek-team worker --runtime codex <<'TASK'
Inspect src/parser.py and tests/test_parser.py.
Find the cause of the empty-input failure. Do not implement changes.
Return concise evidence, suggested fix, risks and tests.
TASK
~~~

Claude Code:

~~~bash
deepseek-team worker --runtime claude <<'TASK'
Inspect src/parser.py and tests/test_parser.py.
Find the cause of the empty-input failure. Do not implement changes.
Return concise evidence, suggested fix, risks and tests.
TASK
~~~

`--runtime auto` предпочитает Codex, когда доступны оба CLI, иначе Claude Code. Инструкции, управляемые пакетом, используют явный runtime, чтобы поведение координатора не переключалось незаметно.

## Управляемая full-access разработка

При effective `full-access` команда `deepseek-team worker` создаёт принадлежащую пакету изолированную копию из **закоммиченного HEAD** исходного репозитория, если не передан существующий owned workspace. Воркер может создавать, изменять и удалять ранее не перечисленные файлы проекта и запускать локальные тесты/сборки с зависимостями, подготовленными внутри этой копии.

Исходный checkout никогда не очищается и не «усыновляется». Dirty, untracked и ignored пользовательские файлы остаются нетронутыми и **не копируются молча** в worker environment. Если задача от них зависит, координатор должен осознанно предоставить безопасный source context, а не просить пользователя очистить checkout.

Типичный one-shot запуск:

~~~bash
deepseek-team worker --runtime claude --delegation-level 50 --access full-access <<'TASK'
Implement the bounded parser fix.
Acceptance criteria:
- preserve the public parser API;
- add a regression test for empty input;
- run the focused local tests;
- report changed files and checks actually run.
TASK
~~~

Для подготовленных зависимостей или последовательных итераций явно создайте/переиспользуйте owned workspace:

~~~bash
deepseek-team workspace create /path/to/project
# запомните выведенный workspace ID

deepseek-team workspace prepare WORKSPACE_ID -- python3 -m venv .venv
deepseek-team workspace prepare WORKSPACE_ID -- .venv/bin/pip install -r requirements.txt

deepseek-team worker --runtime codex --workspace WORKSPACE_ID --access full-access <<'TASK'
Implement the assigned change and run the relevant local tests.
TASK

deepseek-team workspace diff WORKSPACE_ID
~~~

Успешно изменённый workspace можно переиспользовать для следующей итерации. Если выполнение завершилось ошибкой после частичных изменений, файлы и записанный diff сохраняются. Следующая реализация **не** запускается автоматически; сначала проверьте workspace, затем явно продолжите с `--resume-after-failure --workspace WORKSPACE_ID`.

Одновременно могут работать до трёх воркеров. Каждый full-access assignment получает отдельную копию и lock; один воркер не видит копию другого и исходный checkout пользователя. Координатор остаётся ответственным за финальное ревью, интеграцию, commit, push и deployment.

## Legacy exact-file writer

Существующий режим `--write --allow-write` остаётся доступен, когда нужен точный allowlist файлов. Он сохраняет требование чистого linked worktree, запрещает worker-run tests/builds и после выполнения проверяет точные разрешённые пути. Это намеренно более узкий режим, чем managed full-access, и его нельзя сочетать с `--access` или `--workspace`.

Начните с закоммиченной базы и используйте **чистый linked worktree** на отдельной ветке `codex/` или `deepseek/`:

~~~bash
git worktree add -b deepseek/parser-fix ../project-deepseek-parser HEAD
cd ../project-deepseek-parser

deepseek-team worker --runtime claude --write \
  --allow-write src/parser.py \
  --allow-write tests/test_parser.py <<'TASK'
Implement the agreed empty-input behavior and add a focused regression test.
Modify only the allowed files. Do not run tests/builds or touch Git state.
Return a brief summary, risks and suggested checks.
TASK
~~~

Тот же writer-flow работает с `--runtime codex`.

Writer получает одну попытку, одного владельца на файл, точные разрешённые пути, per-worktree lock и post-run Git verification. Hidden/credential targets, symlinks, hardlinks, unsafe index state и неподдерживаемые Git filter/submodule конфигурации отклоняются. Верификатор проверяет tracked, untracked и ignored изменения, а также index, HEAD, branch и Git pointer linked worktree. Неудачные/отклонённые запуски могут оставить частичную работу для проверки координатором; DeepSeek Team никогда молча её не сбрасывает.

**Координатор обязан проверить фактический diff/новые файлы, запустить осмысленные тесты и выполнить интеграцию.** Воркеры никогда не stage/commit/push/deploy. Writers никогда автоматически не повторяют попытку после частичных изменений.

## Команды sandbox

~~~bash
deepseek-team sandbox status
deepseek-team sandbox install-apparmor
deepseek-team sandbox remove-apparmor
~~~

`install-apparmor` отказывается перезаписывать отличающийся/symlink/non-file `/etc/apparmor.d/deepseek-team-bwrap`. `remove-apparmor` удаляет policy только когда установленные байты всё ещё точно совпадают с копией пакета; изменённая администратором policy сохраняется.

Только для диагностики воркеры принимают:

~~~bash
deepseek-team worker --os-sandbox off ...
~~~

Команда выводит предупреждение и намеренно обходит новый OS-layer requirement. Она **не** используется управляемыми инструкциями проекта и не должна применяться как исправление сломанной production-настройки.

## Надёжность и границы безопасности

- Не более трёх воркеров используют общие user-level locks; writer также имеет per-worktree lock.
- Total timeout по умолчанию равен `0` (без ограничения). Медленный/молчащий воркер не считается упавшим.
- Read-only transient failures могут повторяться в пределах настроенного числа попыток. Writers используют ровно одну попытку.
- Результат воркера должен быть завершённым структурированным результатом. Malformed JSON, invalid UTF-8, terminal failure events и пустые успешные ответы отклоняются.
- `DEEPSEEK_TEAM_DISABLED=1` отключает делегирование. `CODEX_DEEPSEEK_DISABLED=1` сохраняется для совместимости.
- Имена DeepSeek/моделей в prompts — это запрошенная конфигурация, а не доказательство реально обслуживаемой удалённой модели; `doctor --live` выполняет доступную проверку routing.

Bubblewrap + AppArmor существенно усиливают изоляцию хоста, но DeepSeek Team **не является полной границей конфиденциальности для произвольно враждебных репозиториев или бинарников координатора**. Claude sandbox намеренно открывает worktree и runtime-файлы, необходимые для запуска CLI; Codex опирается на нативную Bubblewrap-политику Codex. Если сам репозиторий содержит credentials, модели может быть разрешено читать их как project files. Для более сильной изоляции используйте отдельного OS-пользователя/container/VM.

## Проверки

~~~bash
deepseek-team sandbox status
deepseek-team config show --effective
deepseek-team doctor --runtime codex --offline
deepseek-team doctor --runtime claude --offline
deepseek-team doctor --runtime both --offline

# Проверить managed full-access capability surface, выбранную политикой
deepseek-team doctor --runtime both --offline --delegation-level 50 --access auto

PYTHONPATH=src python3 -m unittest discover -s tests -v
~~~

`doctor --offline` проверяет локальные sandbox/runtime capabilities без DeepSeek API request и без валидации ключа. `doctor --live` делает платные вызовы DeepSeek, использует синтетический репозиторий и проверяет, что выбранные read-only workers его не изменяют.

Offline tests используют синтетические credentials/transports. GitHub Actions запускает полный unittest suite, собирает/устанавливает wheel и проверяет оба console alias на Python 3.11, 3.12 и 3.13. Отдельный Ubuntu sandbox job проверяет Bubblewrap/AppArmor. Специальный workflow **Delegation verification** дополнительно устанавливает pinned реальные версии Codex/Claude и прогоняет их настоящие tool/protocol surfaces через offline local provider fixture: проверяются запрет записи в read-only, создание не перечисленного заранее файла и локальные тесты в full-access, параллельные изолированные копии, явное восстановление и классификация provider-vs-execution failures — без реального DeepSeek-ключа и платного запроса. Зелёная CI-матрица не является доказательством live DeepSeek inference request.

## Обновление и удаление

Checkout, которым управляет installer:

~~~bash
git pull --ff-only
python3 install.py --with-sandbox   # рекомендуется в Ubuntu
deepseek-team hooks status
deepseek-team doctor --runtime codex --offline
~~~

Если репозиторий уже был подключён до обновления, **не** запускайте `init` повторно только ради обновления. Новый репозиторий подключается один раз через `deepseek-team init --coordinator codex /path/to/project` (или `--coordinator both`, если нужны оба файла инструкций координаторов). В Codex один раз проверьте/доверьте стабильному hook через `/hooks`.

Для pipx: `pipx upgrade codex-deepseek-team`, затем выполните `deepseek-team hooks install`, `deepseek-team hooks status` и `deepseek-team sandbox status`.

Отключение project instructions / принадлежащей пакету конфигурации координатора:

~~~bash
deepseek-team detach --coordinator both /path/to/project
deepseek-team reset --runtime both
deepseek-team auth remove       # необязательно
~~~

Если нужно также удалить только неизменённую принадлежащую пакету AppArmor policy:

~~~bash
deepseek-team sandbox remove-apparmor
~~~

`reset --runtime codex` удаляет только неизменённый DeepSeek provider block этого пакета и сохраняет основную auth/model и посторонние providers. Для Claude runtime нет принадлежащей пакету постоянной provider-конфигурации, поэтому reset Claude ничего не делает. Environment keys и приватные backup-файлы Codex config сохраняются.

Удалите Python-пакет своим package manager либо удалите принадлежащие installer symlink'и `~/.local/bin/deepseek-team`, `~/.local/bin/codex-deepseek-team` и каталог `~/.local/share/codex-deepseek-team` после detach проектов.

## Область поддержки

Текущий релиз остаётся **только для Linux**. Поддержка Windows сознательно отложена, чтобы не ослаблять гарантии writer.

## Лицензия

[MIT](LICENSE), copyright 2026 kirill31337.
