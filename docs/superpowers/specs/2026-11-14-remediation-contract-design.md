# Контракт исправлений восстановления и подготовки DeepSeek Team

Статус: принят к исполнению. Пользователь 2026-11-14 поручил реализовать план
в полном объёме и опубликовать новый релиз. Решения ниже определяют объём реализации
и условия её приёмки.

База: 0.10.5, `ffc48b354f2a5d7bb2b8d067b119f824714bb3ed`.
Исполнение: [план работ](../plans/2026-11-14-remediation-contract.md).

## Цель и состав

Обеспечить восстановление после подтверждённых отказов без отключения координации,
скрытой потери результатов и ослабления sandbox. Пункт означает deliverable,
назначение — assignment, работа — job со стабильной копией, попытка — attempt.

| Записи отчёта | Обязательство |
|---|---|
| 2 | C3: воспроизводимость сохранённого рецепта с общим родителем целей |
| 3, 6 | C1–C2: проверяемый Git и явный выход при невозможном capture |
| 3 | C3: отдельно объявленный изменяемый кэш не становится результатом |
| 4 | C4: отдельный лимит каждой managed-проверки |
| 7 | C5: явная отмена назначения до запуска |
| 9, 11, 19 и подсказка рецепта | C6: адресные и причинные ошибки |
| 10, 14, 15, 16, 17, `&&` | C6–C7: точные инструкции при сохранении правил |
| 1 | Сохранить исправление 0.10.5 и отрицательные контроли |
| 5, 8, 12, 13, 18, внешние журналы | Отдельные направления в конце документа |

Выбран адресный ремонт с явной операцией отказа от результата. Только документация
не устраняет воспроизведённые тупики. Исключение всего index, автоматическая смена
HEAD и общие ограничения «машина непригодна» нарушают происхождение доказательств;
эти альтернативы отвергнуты.

## Общие инварианты

- Linux OS sandbox, read-only Git воркера и приватная сеть с фиксированным relay обязательны.
- Не ослаблять AppArmor, доступ, off, правила профиля или проверки принадлежности копии.
- Сохранять исходный checkout, пользовательский AGENTS/config, секреты и чужие retained copies.
- Не переписывать `base_head`, прежние attempts, disposition и quality. Новая база — новая задача.
- Не запускать модель, retries, adoption или удаление автоматически для устранения отказа.
- До удаления проверять owner, inode/device, ссылки, mounts, leases и живые операции.
  Невозможность сохранить патч не отменяет эти проверки.
- Публиковать durable результат и cleanup intent до удаления; повтор после сбоя идемпотентен.
- Python 3.11+, стандартная библиотека. Полный check:
  `PYTHONPATH=src python3 -m unittest discover -s tests -v`.

## C1. Безопасный Git index и независимое построение результата

Новые копии используют `git_integrity` version 2. Все административные данные
проверяются как прежде, а index — по нормализованному представлению. Разрешены
изменения только stat-полей записей: ctime, mtime, dev, ino, uid, gid, размера.
Сохраняются mode, object ID, path, stage, обычные и extended flags, включая
assume-valid/assume-unchanged, skip-worktree, intent-to-add. Полезная нагрузка
расширений сравнивается точно; изменение расширения не считается безопасным само собой.

Первый формат поддерживает index v2/v3, SHA-1/SHA-256, с проверкой checksum,
структуры и границ, лимитом чтения 64 MiB. Split/sparse index, v4, неизвестное
обязательное расширение, повреждение и превышение лимита дают отказ с сохранением
копии. Новые managed copies создают полный index v2. Непрочитанный хвост не игнорируется.
SHA-256 здесь означает поддержку формата в parser/canonicalizer и temporary-index
механизме при уже совместимом Git repository. Создание managed copy из SHA-256
source не становится новой обещанной возможностью: изменение `git init`/fetch
для такой поддержки требует отдельного объёма. Приёмка явно различает эти уровни.

**Одной нормализации недостаточно.** Поддельные stat-поля тоже способны заставить
Git пропустить изменённый файл. Поэтому diff/changes/capture, используемые для
приёмки и attribution, строятся через временный доверенный index, заново заполненный
из проверенного `base_head`, без stat-кэша рабочего index. Временный index находится
в приватном ephemeral каталоге, не в `.git` копии; capture по-прежнему работает в
непривилегированной namespace. Нельзя доверять рабочему index для доказательства
отсутствия изменений. Prepared-source delta по-прежнему отделяется от worker delta.

Metadata содержит version, object_format и digest нормализованных административных
данных. Одна реализация обслуживает `Workspace.changes`, diff, capture, import,
prepare и verification. Перед разрешённым обновлением metadata проверяется прежнее
свидетельство. Objects не начинают повторно хешироваться: прежняя граница доверия
read-only Git сохраняется, гарантия против произвольной подмены object database не добавляется.

Технические первоисточники: [формат индекса Git](https://git-scm.com/docs/index-format)
и [git read-tree](https://git-scm.com/docs/git-read-tree). Лимиты и политика поддержки
выше — решения этого контракта, а не дополнительные требования Git.

Legacy `git_digest` проверяется по старым правилам. Upgrade под owner lock разрешён
только после точного совпадения старого digest. Уже расходящаяся legacy-копия не
переаттестуется автоматически; C2 позволяет явно отказаться от её результата.

Приёмка: настоящий `git status`, изменивший stat-кэш, не блокирует новую копию.
Изменения значимых flags/entries, HEAD/refs/config/exclude блокируются; forged stat
cache не скрывает реальные байты. Raw Git index, source и prepared provenance не меняются
в результате read-only проверки. Неисправный или неизвестный формат сохраняет данные.

## C2. Явная отмена с возможной потерей несохраняемого результата

Проектируемые команды, отсутствующие в 0.10.5:

```bash
deepseek-team workspace cancel WORKSPACE_ID --discard-result --reason TEXT
deepseek-team coordination finalize --path PROJECT --task TASK_ID \
  --assignment ASSIGNMENT_ID --cancel --discard-result --evidence TEXT
```

Без флага сохраняется прежний обязательный capture. Флаг разрешён только для отмены,
не для принятия результата. Нужен явно переданный непустой reason/evidence, не default
`cancelled`. Координатор не подставляет destructive flag автоматически после отказа;
его применение требует действующего разрешения пользователя потерять работу этой копии.

Даже с флагом выполняются все проверки безопасного удаления. Живая операция,
неизвестный owner, изменённый inode, foreign ownership, symlink или mount запрещают
очистку. Завершённая работа не переоткрывается и её опубликованный результат не заменяется.

Сначала предпринимается обычный capture. Только типизированный отказ после проверок
удаления разрешает зафиксировать потерю: `git_integrity_mismatch`, `capture_too_large`,
`capture_unavailable`, `capture_failed`. Произвольные исключения не разрешают удаление.
Успешный capture сохраняет прежний полный result schema 1 даже при указанном флаге.

Классификация закрытая; каждый отказ формируется в конкретной проверенной ветке:

| Код | Допустимый источник |
|---|---|
| `git_integrity_mismatch` | Проверенное несовпадение административного digest/HEAD либо повреждённая структура index |
| `capture_too_large` | Превышение фактического лимита encoded patch внутри capture или при проверке его результата |
| `capture_unavailable` | Диагностированный отказ обязательного sandbox, ENOENT/EACCES/EPERM при запуске capture, неподдерживаемый формат index или непустое дерево без доверенной baseline |
| `capture_failed` | Контролируемый timeout capture либо структурированный отказ конкретной Git-команды внутри успешно запущенного capture |

Пустое дерево с незавершённой baseline сохраняет прежний успешный empty capture.
Для непустого такого дерева discard явно допускается; `base_head` остаётся null,
а `capture.baseline_status="unavailable"`. Для записанной baseline значение
`baseline_status="recorded"` не означает успешной текущей integrity-проверки.
Неизвестный exit/signal, отсутствующий или повреждённый протокол ответа, TypeError,
неожиданный OSError и ошибка публикации manifest/ledger не становятся этими кодами.
Не классифицировать по тексту exception или только по exit 73/78. Неизвестное
состояние сохраняется. Контрольный ответ capture ограничен и отделён от patch bytes.

При несохранённом результате публикуется `result.json` schema 2: прежние source/job/
workspace/base identities, `capture.status="discarded"`, `capture.reason_code`,
обоснование и `patch=null`. Не записывать строку ошибки вместо `changes.patch`.
Прежние проверенные артефакты сохраняются с исходным происхождением, а не выдаются
за полный актуальный патч. Не сохранять произвольный exception text, секреты или raw logs.

Существующий `changes.patch` остаётся на прежнем пути и не перезаписывается при
discard. Schema 2 перечисляет его в `historical_artifacts` с `file`, `bytes`,
`sha256`, `attempt_id` и `provenance`. Если прежняя durable запись связывает эти
байты с попыткой, provenance равен `verified_attempt`; иначе `legacy_unattributed`
и `attempt_id=null`. Новый hash сам по себе не доказывает старое происхождение.
Новые успешные captures записывают attempt/hash связь для будущего сохранения.
Наличие файла по имени никогда не заменяет `result.patch`; schema 2 с `patch=null`
не предоставляет актуальный патч. Уже сохранённые checks/diagnostics/history остаются
на своих путях по прежним правилам; произвольные файлы не объявляются evidence.

Job cancellation request, result, terminal lease и cleanup intent становятся durable до удаления.
Затем существующий безопасный путь удаляет execution tree, `.git`, tools и owned
runtime directories. Сбой после публикации возобновляет очистку, не модель/capture.
Постоянные архивы execution tree и TTL cleanup не добавляются.
«Общая транзакция» означает recoverable publication через единый идентификатор
`job_finalization_request`, а не атомарную запись нескольких файлов. Request запрещает
повторный запуск и принятие job; после очистки ledger reconciliation публикует
окончательные dispositions. Сбой на любой границе оставляет достаточно durable
данных для повтора без перезаписи уже подтверждённого result или ранних оценок.

Для связанной работы нужны остановка и отклонение. Если повреждённый Git мешает
обычному `use rejected`, `coordination finalize --cancel --discard-result` сам
записывает отклонение и потерю результата общей транзакцией. Новая quality нейтральна:
`unassessable/unknown`; прежние оценки не стираются и не повышаются. Все assignments/
corrections одной работы получают согласованное завершение. `workspace cancel` для
связанной работы проходит через тот же механизм либо отказывает до удаления.

Status/history/GC/complete/finalize понимают schemas 1/2. Discarded может закрыть
отменённую работу, но не удовлетворяет приёмку или integration provenance. Читатель
неизвестной схемы отказывает безопасно. Повтор операции не переписывает историю.
Это требование относится к новым readers: старый бинарный файл не становится
совместимым со schema 2. Возврат на старую версию с таким state не поддерживается;
инструкции обновления явно предупреждают об этой границе и не предлагают downgrade
как способ восстановить или удалить такую работу.

## C3. Родители staged-целей и явно объявленные cache directories

Проверка environment распознаёт существующий каталог, staged-цель, существующего
потомка staged-цели и будущего родителя корректной цели. Родитель цели-файла тоже
создаётся публикацией. Несуществующий потомок/посторонний путь отвергается. Проверки
containment, symlink и ancestry выполняются до filesystem publication.

Добавляется повторяемое `workspace prepare --cache-dir REL_DIR`: объявляет
изменяемый cache root строго внутри копии, создаёт его и точное anchored exclusion,
сохраняет декларацию в recipe. Env-переменные сами по себе не дают исключений.

Root не может быть корнем проекта, `.git`, `.deepseek-tools`, credential-like путём
или содержать tracked files. Новая декларация допускается до execution для нового/
пустого каталога или дерева, покрытого заявленными prepared copy destinations.
Неподготовленные существующие файлы сохраняются с отказом; совет — новая копия.
После первого execution нельзя расширять набор roots. Повтор прежнего набора допускается.
При удалении декларации до execution снимаются только собственные точно записанные
exclusion patterns; чужие правила сохраняются. После execution набор фиксирован.
Вложенные roots нормализуются к минимальному набору; сами данные не удаляются.
Сохранение чужих правил включает исходные байты, CRLF, отсутствие конечного LF
и file mode существующего exclude. Добавление собственного правила использует
границу LF, сохраняя прежний префикс. Удаляется только точно записанное собственное
вхождение. Незнакомые байты не декодируются с заменой.

Prepared-toolchain metadata и recipe v2 добавляют `cache_dirs: list[str]`; reader поддерживает v1 как
`[]` и v2, неизвестную версию не удаляет. Save атомарен, failed save сохраняет
предыдущую recipe. Cache contents исключаются из результата, но не из входов
check/preparation fingerprints: большие/неизвестные входы остаются непереиспользуемыми.

Будущий пример:

```bash
deepseek-team workspace prepare WORKSPACE_ID \
  --copy /prepared/wrapper=gradle-home/wrapper \
  --copy /prepared/modules-2=gradle-home/caches/modules-2 \
  --cache-dir gradle-home \
  --env 'GRADLE_USER_HOME={workspace}/gradle-home' --save-project
```

Существующий `--copy SNAPSHOT=gradle-home` сохраняет смысл. One-pass prepare/save/
fresh replay должен работать. Большой cache sibling не попадает в patch, новый
исходник вне cache попадает. Большой non-cache результат сохраняет лимит 64 MiB;
при невозможном capture есть только обычная отмена с сохранением либо явный C2.

## C4. Отдельный лимит одной managed-проверки

`worker --check-timeout SECONDS`: конечное число `0 < SECONDS <= 86400`, по умолчанию 1800;
fractional seconds допустимы. Zero/negative/NaN/infinity отвергаются до slot/copy/
credential. Новую env-переменную или persistent config пока не добавляем.
`worker --timeout` остаётся total deadline, включая очередь.

Baseline/post получают одно значение. Предел команды — минимум этого значения и
оставшегося total deadline. Readiness/security probes его автоматически не наследуют.
Check timeout записывается в attempt/check provenance и reuse identity.
Пользователь 2026-11-14 указал на недостаточность прежних 120 секунд. Новая job
без execution history получает 1800 секунд на проверку. Выбор 30 минут учитывает
наблюдаемый baseline около 18 минут. Явное значение позволяет задать подходящий
лимит конкретной сборки.

Источник истины — `check_timeout_seconds` последней начатой execution attempt этой
job, включая linked correction, но исключая verification runs. Значение фиксируется
до baseline checks. Metadata хранит только согласованное зеркало этой attempt;
при конфликте двух записей операция отказывает, а не выбирает удобное значение.
Для legacy attempt без поля значение равно 120. Новый explicit override создаёт
новую запись; `workspace verify` не меняет источник последующего наследования.
Baseline остаётся перед чтением provider credential. Durable workspace execution
attempt уже существует при входе в baseline и сохраняет лимит. Task assignment
переходит в running после успешной baseline и чтения ключа, используя ту же
попытку. Нейтральный baseline failure сохраняет failure_stage=preparation и
реальные job/attempt/timeout references, не создавая вторую попытку. Prompt brief
публикуется с redaction до модели, после доступности ключа. Источник истины
лимита до/после baseline остаётся в истории job, а не в статусе task assignment.
Таким образом, legacy history отражает исторические 120 секунд. Для продолжения
такой работы с новым лимитом используется `--check-timeout 1800`.

Resume без нового значения наследует записанное, явный override относится к новой
попытке и инвалидирует reuse. `workspace verify --timeout` явно переопределяет лимит;
без флага использует записанное значение работы, для legacy — 120. Проверка значений
одинакова. Verification не меняет историю предыдущей execution attempt.
Разделение probes явное: новый worker `--check-timeout` не меняет их лимиты.
В `workspace verify` без флага checks наследуют execution timeout, а toolchain
`verify-probe` остаётся на 120 секундах. Явный существующий `verify --timeout`
сохраняет прежнее действие на checks и toolchain verify-probes; runtime/security
preflight остаётся под собственными лимитами. Verification override не наследуется
следующим resume/correction. Все использованные лимиты отражаются в evidence.
`check_timeout_seconds` продолжает хранить настроенный лимит и участвовать в reuse
identity. Отдельное `effective_timeout_seconds` отражает применённый предел после
ограничения общим дедлайном. При истёкшем дедлайне значение равно нулю, команда
не запускается. Оба значения проверяются и сохраняются в ledger/evidence.
Неудачная или timed-out проверка не становится переиспользуемым успехом.

Приёмка включает реальный процесс дольше 120 секунд с лимитом 180, короткие реальные
timeout-контроли, total-deadline clamp, resume и отсутствие ошибочного cache hit.

## C5. Отмена назначения до запуска

```bash
deepseek-team coordination cancel --path PROJECT --task TASK_ID \
  --assignment ASSIGNMENT_ID --evidence TEXT
```

Новая операция действует только на `planned` без workspace/job/attempt/start,
prepared_changes и worker_changes. Живой launch, активный slot или queue ticket
дают отказ 75 с инструкцией сначала остановить ожидающий процесс. Аттестация
пользователя не заменяет проверки процесса. Cancel и начало allocation/preparation
сериализуются: ровно один победитель, никаких provider/copy после успешного cancel.

До очереди launcher под task lock регистрирует reservation: task ID, assignment ID,
уникальный launch token, generation и identity процесса (PID/start time/boot ID).
Queue ticket и slot несут эту же привязку. Чужие tickets не блокируют отмену;
непривязанный legacy ticket с неопределимой принадлежностью даёт отказ 75 с причиной.
Task lock не держится во время ожидания и не захватывается под queue lock:
reservation → освобождение task lock → queue/slot → освобождение queue lock →
повторная проверка reservation под task lock. Перед первой allocation/import/binding
публикуется durable `launch_phase="allocation"`; после него C5 уже отказывает даже
при ещё отсутствующем workspace ID, и требуется обычное recovery/finalize.

Cancel под тем же task lock проверяет запись и liveness именно связанного launch.
Живой или непроверяемый owner блокирует cancel. Достоверно мёртвый pre-allocation
reservation можно закрыть и оставить terminal cancellation как tombstone. Launcher
повторно проверяет token/generation/status перед allocation и provider; сменившаяся
или отменённая запись запрещает продолжение. Старт после успешного cancel сначала
видит tombstone и отказывает, не создавая queue ticket или copy. Legacy assignment
без reservation остаётся отменяемым только при доказанном отсутствии связанного
или неоднозначного живого запуска. Независимая заранее созданная копия не удаляется.

История сохраняется: `status="failed"`, `error_kind="cancelled"`,
`failure_stage="before_start"`, `queue_state="finished"`, rejected disposition,
evidence и timestamp. Не создавать фиктивные execution/checks/cost и не обучать
quality на неисполненной работе. Повтор идемпотентен; pending scope освобождается.

Queue-time HEAD/access failure сохраняет причину и путь этой отмены. Полная ревизия
всё ещё может убрать wholly unstarted пункт; новая команда сохраняет историю.
Подготовленные/начатые работы используют существующие review/finalize и при нужде C2.

`base_head` остаётся immutable. Завершить/отменить assignments, записать outcomes,
вызвать complete, затем следующий UserPromptSubmit создаёт новую задачу на текущем
HEAD. Rebase существующей задачи и ручное создание task ID не добавляются.
Счётчики профилей сохраняются; отмена не изображается выполненной работой.

## C6. Причинная диагностика

- Изменение started definition: ID и реальные изменённые keys, без дампа значений.
  Охвачены все поля canonical logical definition: kind, scope, acceptance,
  dependencies, checks, checks_reusable, retention, features, requested_executor,
  integration_of, write_scope, decision_artifacts, correction_of, delegation_reason
  и native_exception. Новые logical поля должны расширять диагностику. Derived
  routing/assignment/results не объявляются изменениями определения.
- Собрать независимые structural/enum errors всех пунктов в стабильном порядке.
  Неверная форма родителя не порождает каскад ошибок потомков. Никаких partial
  plan/routing writes до успешной structural validation.
  Новые acceptance и checks entries должны быть непустыми строками без NUL.
  Пустой список `checks=[]` сохраняет прежний смысл.
  Внутренний `CoordinationError.problems` содержит список объектов
  `{deliverable_id, field, code, message}`; для ошибки уровня плана ID равен null.
  CLI сохраняет текстовый stderr и exit 64, выводя список сообщений. Новый публичный
  JSON-протокол ошибок или флаг вывода в этот объём не входит.
- Объяснить наличие ключа checks отдельно от непустого списка; manual checks=[]
  допустимы. Не предлагать проверки-пустышки.
- Recipe path error называет переменную/путь и будущего родителя; не советует
  обновлять исправный cache. Missing/stale source сохраняет отдельную подсказку.
- Queue access error: `Current effective access no longer permits writes.`
  Различимы explicit read-only, missing layer/default и ошибка чтения. Указываются
  access и безопасные имена источников без содержимого настроек.
- Missing settings сохраняет fallback. Invalid TOML/EACCES/symlink/hardlink/
  directory/special file дают SettingsError до provider, без traceback и fd leak.
- Advice для manual 75 объясняет, что router admission не отменяет retention rule.
  Нельзя превращать обычную implementation в protected kind ради обхода.
- Schema enums, checks=[], `&&`, exact-file vs directory destination и стандартные
  output sinks документируются как уже существующее поведение. External writes не разрешаются.

## C7. Ворота приёмки и выпуска

Каждый блок имеет положительный и отрицательный public CLI/state control; одних
mock assertions недостаточно. Native Codex/Claude fixtures и real namespaces
обязательны для изменённых границ. Worker skip покрывает координатор. HOME,
CODEX_HOME, CLAUDE_CONFIG_DIR, XDG_CONFIG_HOME/state изолированы, внешние canaries сохранены.

После reviewed integration: полный unittest discovery, CI supported Python matrix
и Python 3.14 из исходного сообщения, реальные capture/cleanup и JVM fixtures,
подготовленный offline Gradle fixture, wheel/sdist, strict Twine, pip/pipx/uv
install/upgrade/uninstall с сохранением внешних конфигураций и инструкций.
Gradle fixture подтверждает механизм, не выполнение чужого проекта.

Версия релиза определяется при исполнении. Commit/push/tag/GitHub/PyPI принадлежат
координатору и выполняются только в разрешённом пользователем объёме после gate.
Пользователь разрешил реализацию и выпуск. Для выпуска
обязательны совпадение registry/artifact hash и чистая установка.

## Отдельные направления и запреты расширения объёма

№5 (CoW/reflink/shared binds), №8 (adoption автономного результата), №12 (scratch/
log writes вне проекта), №13 (новые preparation fingerprints для ./gradlew) требуют
отдельных контрактов. Не переносить runner constraint на несвязанные пункты.
Не обещать работу чужого Gradle-проекта без его dependencies/artifacts.
№1/16/17 и `&&` не реализуются заново.

Исполнитель выбирает private helper names и fixture layout. Изменения data-loss,
access, baseline, cache или result-schema контракта сначала отражаются здесь.
