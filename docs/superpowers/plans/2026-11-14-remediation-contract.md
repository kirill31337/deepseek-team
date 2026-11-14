# DeepSeek Team Remediation Contract Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development
> or superpowers:executing-plans task-by-task. Project DeepSeek routing takes precedence:
> workers remain leaf agents; native agents need the recorded exception. Checkboxes
> describe future implementation, not completed work.

**Goal:** устранить подтверждённые тупики восстановления/подготовки без ослабления гарантий.
**Architecture:** versioned Git integrity плюс независимый diff; явный discard;
cache declarations; check-timeout; отмена неначатой работы; причинные ошибки.
**Tech Stack:** Python 3.11+, standard library, unittest, Git, Bubblewrap, native fixtures.
**Spec:** [контракт C1–C7](../specs/2026-11-14-remediation-contract-design.md).
База: `ffc48b354f2a5d7bb2b8d067b119f824714bb3ed` / 0.10.5.
Статус: принят к исполнению пользователем 2026-11-14 вместе с публикацией релиза.

## Global Constraints

- Linux sandbox, read-only worker Git и fixed relay обязательны; AppArmor не ослабляется.
- Сохраняются base_head, attempts/quality, user source/config/AGENTS и чужие copies.
- Нет automatic retry/adoption/off bypass/access widening или worker commit/push.
- Python 3.11+, стандартная библиотека; полный check:
  `PYTHONPATH=src python3 -m unittest discover -s tests -v`.
- Новые CLI/state формы в этом плане — проект, не возможности текущей 0.10.5.
- Пользователь поручил реализацию и публикацию после выполнения условий приёмки.

## Review Focus

| Риск | Контроль |
|---|---|
| Индекс скрывает изменённое содержимое flags или подделкой stat | T1: оба adversarial controls и независимый capture |
| Сбой между manifest, cleanup и ledger | T2: crash points и идемпотентный повтор одной job |
| Cache declaration скрывает исходники/готовую работу | T3: tracked/unprepared data, late expansion и source outside cache |
| Resume возвращает 120 или использует неверный cache hit | T4: inherited/override timeout и provenance |
| Cancel конкурирует с очередью/allocation | T5: process barriers, один победитель, no provider after cancel |

## Порядок и ответственность

T0 → T1 → T2; T3/T4/T6 можно готовить после T0. Интеграция T3 зависит от T1,
T5 использует terminal-history правила T2; T7 зависит от T1–T6.
Изменения coordination.py в T2/T5/T6 интегрируются последовательно.

Координатор владеет architecture/security, integration, final verification и release.
Обычные slices маршрутизируются по CURRENT effective config с features и executor:auto;
не обещать заранее worker/coordinator долю. Перед каждым worker — status/config,
перед первым —doctor. Runtime соответствует координатору, модель deepseek-flash.
Owned copies/dependencies готовит координатор; uncommitted context импортирует точными
файлами. Worker не делегирует, не stage/commit/push. До изменения HEAD закончить волну
назначений; после commit новые jobs регистрируются на новой базе новой задачи.

## T0. Подготовить базу исполнения

**Files:** read-only src/tests/docs и существующая CI matrix.
**Interfaces:** consumes C1–C7; produces verified baseline, fixtures/dependencies и план распределения.

- [x] Проверить HEAD/status, текущие retained jobs и config; сохранить user changes.
- [x] Зарегистрировать bounded slices и doctor offline до запуска workers.
- [x] Подготовить legacy/result/index/cache/queue fixtures без чужого кода и секретов.
- [x] Выполнить полный baseline; различить pre-existing failures и обязательные skips.
- [x] Проверить dependencies/checks внутри настоящего worker sandbox до provider.

## T1. Git integrity и независимый diff

**Files:** create `src/codex_deepseek_team/git_integrity.py`, `tests/test_git_integrity.py`;
modify `src/codex_deepseek_team/workspace.py`, `workspace_gc.py`, `toolchains.py`,
`verification.py` в местах integrity/diff. Неполные имена модулей ниже относительны
`src/codex_deepseek_team/`.
**Interfaces:** `snapshot(root: Path) -> dict`, `verify(root: Path, evidence: dict) -> None`,
`canonical_index(data: bytes, object_format: str) -> bytes` в git_integrity;
общий legacy adapter и внутренний temporary-index механизм diff по C1.

- [x] Добавить `test_status_stat_refresh_preserves_integrity`,
  `test_assume_unchanged_cannot_hide_result`, `test_forged_stat_cache_cannot_hide_content`,
  `test_flags_stage_mode_and_git_admin_changes_rejected`.
- [x] Добавить v2/v3 SHA1/SHA256, checksum/truncation/bounds, split/sparse/v4,
  unknown-required-extension, точный legacy upgrade и отказ при legacy mismatch.
  SHA256 проверяется на уровне parser/canonicalizer и совместимого repository;
  новый end-to-end SHA256 source clone/init/fetch не входит в T1.
- [x] Запустить `PYTHONPATH=src python3 -m unittest discover -s tests -p 'test_git_integrity.py' -v`;
  зафиксировать падающие новые положительные случаи до implementation.
- [x] Реализовать checksum/structure validation и canonicalization только разрешённых stat fields.
- [x] Протянуть единый evidence adapter и trusted temporary index во все result/attribution paths;
  сохранить raw index/source, prepared delta и cleanup temporary index при каждом exit.
- [x] Повторить focused tests и workspace/toolchain/GC regressions, настоящий status→diff→capture.
- [x] Координатор принимает после security review обоих способов скрытия изменённых байтов.

## T2. Явный discard результата

**Files:** modify `workspace_gc.py`, `workspace_lifecycle.py`, `workspace.py`,
`coordination.py`, `coordination_cli.py`, `delegation_cli.py`;
create `tests/test_discard_result.py`; extend `tests/test_review_lifecycle.py`,
`tests/test_preparation_review_regressions.py`.
**Interfaces:** keyword `discard_result: bool = False` в GC cancel/finalize и
coordination.finalize_assignment; CLI и schema 2 строго по C2.

- [x] Public CLI failing tests: ordinary cancel preserves on Git mismatch/64MiB overflow;
  explicit discard records truthful schema 2 and removes only owned execution data.
- [x] Tests: explicit reason; no accepted/integration from discarded; unknown exception preserves;
  previous artifacts/quality survive; all linked assignments of one job agree.
- [x] Таблица C2: отдельный контроль каждого typed failure, unknown exit/protocol/OSError;
  incomplete baseline empty/nonempty; historical patch с подтверждённым и неизвестным
  attempt provenance. Старый файл не становится актуальным из-за своего имени.
- [x] Canary tests: symlink, swapped inode, foreign owner, mount, active operation/lease.
- [x] Crash points: before manifest, after manifest, after rename, before ledger publication;
  repeat repairs permitted phase without model/repeated capture/history rewrite.
  Recovery использует durable job_finalization_request; до удаления опубликованы
  result, terminal lease и cleanup intent, после него согласуется ledger.
- [x] Запустить `PYTHONPATH=src python3 -m unittest discover -s tests -p 'test_discard_result.py' -v`.
- [x] Ввести typed capture failures и durable cancellation path; catch-all не разрешает deletion.
- [x] Обновить result readers, complete/history/GC/integration для schemas 1/2;
  связанная workspace cancel не оставляет pending ledger после очистки.
- [x] Реальные namespace/capture tests и coordinator security review перед принятием.

## T3. Staged parents и cache directories

**Files:** modify `toolchains.py`, `toolchain_cache.py`, `delegation_cli.py`,
`preparation_guard.py`, `verification.py`, `docs/TOOLCHAINS.md`;
create `tests/test_toolchain_cache_dirs.py`; extend `tests/test_toolchains.py`,
`tests/test_toolchain_recipe_recovery.py`.
**Interfaces:** `toolchains.prepare(..., cache_dirs: list[str] | None = None)`;
CLI `--cache-dir REL_DIR`; metadata/recipe v2 cache_dirs, v1→[]; T1 integrity adapter.

- [x] Tests: target/descendant/future parent/file-parent/unrelated/missing descendant;
  использовать разрешённую env GRADLE_USER_HOME, не выдуманный ключ CFG.
- [x] One-pass→save→fresh-replay с двумя nested copies и common env parent.
- [x] Negative controls: root/Git/tools/traversal/symlink/credential/tracked/unprepared data,
  late expansion; immutable after execution, idempotent before/after execution.
- [x] v1/v2 replay, failed-save rollback, root normalization/removal, preservation чужих excludes;
  cache changes остаются inputs fingerprint, stale exclusions не скрывают данные.
- [x] Запустить `PYTHONPATH=src python3 -m unittest discover -s tests -p 'test_toolchain_cache_dirs.py' -v`.
- [x] Реализовать recognition, safe staged publication и versioned declarations без implicit env exclusions.
- [x] Real capture controls: большой cache sibling исключён, новый source вне root включён,
  большой non-cache result по-прежнему ограничен; existing whole-root recipe работает.
- [x] Обновить примеры и причинную подсказку, принять diff/checks.

## T4. Check timeout

**Files:** modify `worker.py`, `managed.py`, `development.py`, `check_evidence.py`,
`check_reuse.py`, `verification.py`, `workspace.py`, `workspace_lifecycle.py`,
`coordination.py`, `delegation_cli.py`; create `tests/test_managed_check_timeout.py`
и `tests/test_managed_check_timeout_long.py`; extend `tests/test_check_reuse.py`.
**Interfaces:** parsed `args.check_timeout: float | None`; effective
`check_timeout_seconds` в attempt/metadata/evidence; resolve None as inherited/1800.
Историческая legacy execution attempt без поля отражает прежний лимит 120.

- [x] Tests: новая job по умолчанию 1800, legacy history 120, explicit 900,
  fractional, zero/negative/NaN/infinity/86401;
  invalid input before allocation/provider. Программный bool тоже не число timeout.
- [x] Both baseline/post, min(total remaining), queue expiry и сохранение failed-check history.
- [x] Проверить durable attempt/metadata значение непосредственно при входе в baseline,
  включая прерывание baseline до запуска модели и последующий явный resume.
- [x] Resume inheritance/override, verify default/override, changed timeout causes reuse miss.
- [x] Linked correction наследует последнюю execution attempt, verification не источник;
  конфликт metadata/attempt даёт отказ. Verify без флага оставляет probes=120,
  явный verify --timeout действует на checks и toolchain probes, не на runtime preflight.
- [x] Запустить `PYTHONPATH=src python3 -m unittest discover -s tests -p 'test_managed_check_timeout.py' -v`.
- [x] Протянуть resolved timeout в checks/provenance/reuse; не менять readiness probe limits.
- [x] Реальные короткие timeout tests и process > 120s при лимите 180; меньший total deadline прерывает.
  Длинный regression находится в отдельном discoverable test-файле, чтобы старый
  120-секундный supervisor мог выполнять короткий declared check. Полный discovery
  и отдельный coordinator run с timeout 180/360 выполняют настоящий длинный процесс.
- [x] Public help/example и приёмка T4.

## T5. Отмена неначатого назначения

**Files:** modify `coordination.py`, `coordination_cli.py`, `worker_admission.py`,
`worker_slots.py` при необходимости queue locking, `coordinator_hooks.py`;
create `tests/test_cancel_unstarted.py`; extend `tests/test_worker_configuration.py`,
`tests/test_coordination_outcomes.py` и native hook integration tests.
**Interfaces:** `cancel_unstarted_assignment(root: Path, task_id: str,
assignment_id: str, evidence: str) -> dict`; CLI coordination cancel по C5.

- [x] Tests: untouched planned cancellation/history/pending-scope release/repeat idempotency.
- [x] Отказ для workspace/job/attempt/start/prepared/worker changes и active queue/slot;
  отмена active queued launch сама не останавливает процесс и возвращает 75.
- [x] Process/barrier race cancel vs admission/allocation: один победитель, no provider/copy after cancel.
- [x] Reservation/token/generation связывают ticket/slot с assignment: foreign ticket,
  ambiguous legacy ticket, dead/live/uninspectable owner, cancel-before-enqueue,
  grant-before-allocation и crash после durable allocation marker проверяются отдельно.
- [x] Stale-head queue→cancel→complete→new turn/current HEAD; all profiles/access controls.
- [x] Запустить `PYTHONPATH=src python3 -m unittest discover -s tests -p 'test_cancel_unstarted.py' -v`.
- [x] Реализовать reservation/fence и последовательность lock acquisition по C5;
  truthful cancelled-before-start state без fictitious execution/quality.
- [x] Оба native runtime hooks: scope и Stop до/после cancel, unrelated writes остаются denied; принять T5.

## T6. Диагностика

**Files:** modify `coordination.py`, `coordination_cli.py`, `routing_models.py`,
`worker_admission.py`, `settings.py`; согласовать recipe errors с T3;
create `tests/test_remediation_diagnostics.py`; extend `tests/test_feature_diagnostics.py`,
`tests/test_settings_transaction.py`, `tests/test_routing_cli.py`.
**Interfaces:** внутренний `CoordinationError.problems` по C6; существующий текстовый
stderr и exit 64 сохраняются, сообщения агрегируются без нового JSON-протокола.
Admission/config errors — controlled exit 78.

- [x] Tests: точные changed keys/ID для scope/features/checks/acceptance/dependencies,
  canonical medium/high и legacy defaults не считаются ложным изменением.
- [x] Проверить остальные logical поля C6: kind, checks_reusable, retention,
  requested_executor, integration/write/decision/correction и native metadata;
  derived routing/results не создают ложные изменения.
- [x] Несколько invalid enums в стабильном порядке, malformed-parent без каскада,
  no partial routing/ledger writes; invalid enum проверяется до immutable comparison.
- [x] Settings missing/read-only/TOML/EACCES/symlink/hardlink/directory/FIFO:
  controlled error, no fd leak/traceback/provider, no false fallback.
- [x] Existing controls: checks=[], schema enums, && exact/directory targets,
  declared/undeclared/external logs и /dev/null, без расширения прав.
- [x] Запустить `PYTHONPATH=src python3 -m unittest discover -s tests -p 'test_remediation_diagnostics.py' -v`.
- [x] Реализовать validation collection, causal messages и controlled settings read;
  пояснить admission vs manual 75 без ложного protected kind.
- [x] Принять T6 по фактическим public outputs и diff.

## T7. Интеграция и ворота выпуска

**Files:** README/docs/shipped guidance и существующие тесты ресурсов;
`.github/workflows/test.yml` для Python 3.14 после сверки current matrix;
version declarations/release note только на разрешённом этапе выпуска.
**Interfaces:** accepted T1–T6 → verified package, recovery instructions и gate evidence.

- [x] Интегрировать reviewed deltas в порядке зависимостей; проверить old/new records,
  сохранение user config/AGENTS и отсутствие secrets/raw logs/unrelated product code.
- [x] Обновить shipped guidance/help/docs, сохранив unrelated инструкции; явно описать
  destructive discard и запрет его автоматического выбора агентом.
- [ ] Полный unittest discovery, CI supported matrix+3.14, native Codex/Claude fixtures;
  обязательный skip покрыть coordinator execution.
- [x] Real namespace capture/cleanup, Java/JDK и подготовленный offline Gradle fixture
  с закреплёнными dependencies; результат не приписывать чужому проекту.
- [x] Независимое review, устранение подтверждённых дефектов, quality/rework ledger.
- [ ] Только при разрешённом выпуске: wheel/sdist→strict Twine→pip/pipx/uv lifecycle→CI→
  tag/release→registry hashes→clean install. Версию выбрать при исполнении.
- [ ] Принятые/cancelled owned jobs финализировать, чужие/непринятые сохранить;
  GC сначала dry-run. Закрыть все coordination outcomes.

## Определение готовности

T1–T6 принимаются отдельно по reproducer, negative controls и actual diff.
Зелёный общий suite без нового reproducer не закрывает дефект. Выпуск требует T7;
написание плана, теста или ответ модели не означает исправление. Отложенный объём
перечислен в spec и не добавляется незаметно в эти этапы.

## Журнал исполнения

- 2026-11-14 создана цель реализации и публикации. Task `task-b0285561c4d909a83a67`
  использует базу `ffc48b3`, Auto/full-access и runtime codex.
- T1, T3, T4 и T6 назначены изолированным DeepSeek workers с effort max.
  Отдельный worker готовит Python 3.14 CI и настоящий offline Gradle fixture.
- Версия кандидата выбрана 0.11.0 с учётом новых CLI и версий persistent state.
- Для проверки подготовлены Gradle 8.14.3 с официальным SHA-256
  `bd71102213493060956ec229d946beee57158dbd89d0e62b91bca0fa2c5f3531`
  и отдельная среда Python 3.14.10. Результаты baseline и интеграции добавляются
  после завершения соответствующих проверок.
- Pre-flight T1/T3/T4 совместно меняют workspace/toolchain integrity и verification.
  Сначала принимается T1, затем в его интерфейсы интегрируются cache и timeout.
  T6 предшествует T2/T5 в coordination.py. Изменения HEAD откладываются до закрытия
  текущей волны, новые этапы получают точно импортированные принятые исходники.
- Baseline 0.10.5: 1932 теста, OK, 12 skips, 1074.494 секунды. Дополнительно
  обязательные реальные CLI/permissions 4/4, runtime environment 2/2,
  Claude lifecycle 9/9 и Codex lifecycle 5/5 прошли с CI pins 0.155.1/2.1.316.
- Для изолированной npm-поставки Codex используется её собственный native binary,
  Claude запускается через штатный npm entrypoint. Подготовка alias с лишним
  symlink-hop была исправлена до успешной проверки среды.
- Метаданные 0.11.0 интегрированы после фактического diff review и provider-free
  namespace verification. Release notes пока описывают кандидат.
- Первый cache attempt прервался из-за provider stream disconnect без изменений
  реализации. После проверки diff и нейтрального environment disposition выполнен
  явный resume того же assignment и той же копии.
- User guidance интегрирован после фактического diff и namespace verification.
  Координатор исправил entity name отмены с attempt на assignment и уточнил
  отсутствие поддержки schema 2 в старой версии. Проверки workspace hygiene 5/5.
- Python 3.14 CI и Gradle fixture интегрированы. Реальный Gradle обнаружил ошибку
  admission по первой строке вывода версии. Координатор воспроизвёл RED на проверенном
  дистрибутиве, изменил проверку на отдельную строку версии и получил GREEN.
  Полный namespace Gradle gate ожидает интеграции T3 и остаётся незакрытым.
- T1 прошёл coordinator security review и все 23 новых теста на хосте, включая
  настоящий unprivileged forged-stat capture. Интеграция ожидает завершения
  пересекающихся worker scopes в workspace.py и toolchains.py.
- T6 передан на bounded linked correction после трёх воспроизведений координатора:
  прежний scalar scope должен нормализоваться, list/dict kind должен давать
  контролируемую ошибку, post-open settings I/O должен давать SettingsError.
- T4 independently verified с supervisor timeout 360, включая настоящий 121s check.
  Граница перед baseline выявила отсутствие durable timeout/attempt до команды.
  Явный resume исправляет эту границу и учитывает пожелание пользователя о 1800s
  для новых jobs. Старые attempts без поля отражают исторический лимит 120s.
- Ruling: baseline относится к execution attempt после readiness/dependency
  preflight. Это обеспечивает durable timeout до baseline и правдивый resume
  при прерывании проверки. Ошибка до provider не приписывает запуск модели или
  стоимость. Риск неверной реализации — потеря причинной истории попытки.
- Ruling: scalar scope string сохраняет прежнюю нормализацию в один путь.
  Контролируемая структурная диагностика применяется к остальным неверным типам.
  Риск неверной реализации — несовместимость существующего plan API.
- T1/C3/C4/C6 совместно проверены в собственном detached worktree координатора
  `/tmp/deepseek-remediation-build-bkxzi_fv/validation` на исходной базе. Дерево
  создано для проверки принятой композиции, пока независимые T2/T5 работают.
  Подготовлены только выбранные package-файлы и принятые helper deltas. Временных
  веток нет. Рабочие копии DeepSeek и пользовательский checkout сохранены.
- Совместный focused suite: 216 тестов, OK, 129.600 секунды. Обязательный настоящий
  offline Gradle 8.14.3/JDK17 fixture: 1 тест, OK, 73.389 секунды, включая
  объявленный большой кэш, capture нового source и replay сохранённого рецепта.
- T3 linked correction прошла независимые 24/24 public tests координатора.
  В metadata v2 добавлена приватная `exclude_ownership` map pattern→owned count.
  Чужие идентичные правила и дубликаты сохраняются. У legacy без записи ownership
  существующие строки сохраняются. Перед разрешённой публикацией exclude проверяется
  прежний C1 evidence и затем обновляются canonical и legacy mirrors.
- T4 завершил явный resume. Worker short suite 21/21 и long suite 3/3, включая
  реальный 121-секундный процесс при лимите 180. До baseline записываются attempt
  identity и effective timeout. Новая работа получает 1800, legacy без поля — 120.
- Недостающие в первоначальных scopes routing/lifecycle helpers и отдельный long
  fixture зарегистрированы самостоятельным ограниченным implementation slice.
  DeepSeek применил рассмотренные предложения в новой копии и проверил focused
  105, adjacent 96, discovery lifecycle 11 и real-duration 3 теста. Исходные
  scopes начатых пунктов сохранены. Это исправление карты файлов координатора.
- T2 и T5 зарегистрированы и запущены независимо с точным подготовленным контекстом.
  Политика перед каждым назначением Auto/full-access, deepseek-flash/max, runtime codex.
  Независимый DeepSeek review охватывает только уже принятую композицию C1/C3/C4/C6.
- Lessons review version 13 сохранён как обоснованный no-change по новым cases.
  Хеш исходных пользовательских изменений AGENTS.md по-прежнему совпадает.
- Python 3.14.10 в проверочном дереве: 155 тестов, OK, 185.999 секунды,
  включая parser/canonicalizer, cache/public capture, timeout/provenance,
  реальный длинный процесс и diagnostics/settings/routing.
- Предварительные wheel/sdist прошли strict Twine и проверку identity. Lifecycle
  install/replacement/uninstall через pip, pipx и uv прошёл с сохранением внешнего
  config. Это preflight принятой композиции, окончательные артефакты строятся после T2/T5.
- Независимый review C1/C3/C4/C6 принят. Три воспроизведения переданы отдельными
  linked corrections: foreign exclude bytes/CRLF/mode, недостающее effective
  timeout evidence и неверные entry types acceptance/checks. Соответствующие
  уточнения внесены в C3/C4/C6. Worker focused batches 93/131 прошли, три namespace
  skips уже покрыты хостовыми C1/C3 проверками координатора.
- Adjudication: configured timeout остаётся частью stable reuse identity.
  Failed/timed-out evidence уже исключено из reuse. Effective deadline limit
  добавляется отдельным полем, не подменяя источник наследования попытки.
- Adjudication: hard interruption между exclude publication и durable metadata
  оставляет копию и исходные данные с отказом C1 integrity. Контракт C3 требует
  атомарного recipe save, но не вводит transaction journal для всех prepared
  файлов. Состояние сохраняется для осмотра и явного C2 cancel, автоматическая
  переаттестация расходящегося Git не добавляется. Более сильное journal recovery
  требует собственного контракта. Ошибку обычного сохранения нельзя считать
  разрешением удалить результат.
- C3 byte/mode correction принята и интегрирована после полного diff review.
  Координатор выполнил 50 C1/C3 тестов на хосте, все прошли без skips.
- C4 effective-timeout correction принята: 110 focused evidence/reuse тестов
  координатора прошли. Сохранён настроенный лимит, добавлен проверяемый фактический
  предел, старые evidence rows читаются, старые reuse entries могут дать miss.
  Локальная prerequisite routing_models правка воркера не интегрирована: в root
  уже есть канонический C6 helper. Контекстный пробел принадлежит координатору.
- C6 entry correction принята после host 33/33 и сохранения C4 при интеграции.
  Исторический malformed acceptance завершается обычным accepted/complete без
  переписывания истории. В root интегрированы оба проверенных набора C4/C6.
- T5 actual source diff проверен и интегрирован. Host 25/25, включая настоящие
  process barriers enqueue/grant/allocation, прошёл. Совместный root прогон
  C4/C5/C6: 91 тест, OK, 61.841 секунды.
- T2 попытка оборвалась provider stream disconnect через 2175 секунд, изменения
  сохранены. Координатор осмотрел partial delta и выполнил 46 public controls,
  обнаружив незавершённое crash reconstruction и mount fixture с неразличимым owner.
  Записан neutral environment outcome и запущен явный resume той же assignment/copy.
  Это незавершённая работа; качество и факт полной реализации не приписываются.
- Пробное применение патча к внешнему проверочному worktree через apply_patch
  отклонено hook из-за attached-project boundary. Патч сохранён и затем корректно
  применён в основном checkout после завершения пересекающихся назначений.
- Дополнительное изменение CLI discovery test вне исходного scope T5 оказалось
  избыточным: новый C5 public test уже проверяет команду. Оно не интегрировано.
  Неначатый вспомогательный пункт будет отменён через C5 без worker/model/quality.
  Настоящий CLI сейчас правдиво возвращает 75 из-за активного непривязанного
  legacy slot текущего установленного раннера; независимая заранее созданная копия
  сохранена. После выхода процесса повтор проверит положительный исход отмены.
- Root C1/C3: 50 тестов, OK, 25.440 секунды. Python 3.14 C4/C5/C6 повтор:
  70 тестов, OK, 37.984 секунды после test-only исправления координатора:
  bounded polling принимает документированный transient75 allocator contention,
  другие ошибки распространяются; ранний assertion закрывает child/streams.
- Нативный CI-pattern прогон (29 тестов) выявил C4 regression в прежнем контроле
  baseline-before-credential: ключ читался до истечения baseline. Результат не
  объявлен GREEN. Создан linked correction с приоритетом сохранения прежнего API.
- Уточнено прежнее ruling T4: durable identity/timeout до baseline относятся к job
  workspace attempt. Условие running task assignment до baseline было выбором
  реализации координатора, оно не требуется контракту. Task running/redacted brief
  идут после успешной baseline и доступности ключа, используя ту же actual attempt.
  Нейтральная ошибка baseline сохраняет failure_stage=preparation, job/attempt/
  timeout references и последующее наследование. Чтение ключа до baseline запрещено
  существующим проверяемым поведением. Архитектура и источник наследования сохранены.

- Повторный независимый review воспроизвёл два дефекта текущей истории. При discard
  связанного job ранее принятый predecessor сохранял incorporated. При таймауте
  baseline во время resume task ledger сохранял прежний attempt и timeout.
  Координатор устранил оба, сохранив прежние decisions/quality в истории и добавив
  отдельное рассмотрение каждой неудачной baseline до следующего resume.
- Schema 2 readers проверяют наличие retained patch в manifest и принадлежность
  verified_attempt durable истории job. Отдельные corruption controls проходят.
- Исправлена граница планирования последующих corrections после coordinator-only
  исправления. Для продолжения worker lineage учитывается реальное assignment.
  Два worker successors одного predecessor по-прежнему отклоняются.
- Security review координатора выявил ошибочную классификацию неизвестного Git I/O
  как проверенного mismatch. EIO чтения index и ENOSPC temporary index теперь
  сохраняют копию. Два public discard controls воспроизвели RED и затем GREEN.
- Host recovery batch прошёл 125 тестов за 184.009 секунды. Итоговый focused набор
  на Python 3.14.10 прошёл 192 теста за 205.861 секунды без skips.
- Настоящие Codex 0.155.1 и Claude Code 2.1.316 прошли дополнительный provider-free
  сценарий cancel. Проверены pending-scope denial, Stop до отмены, явная отмена,
  разрешённая правка прежнего scope, отказ для постороннего файла и завершающий Stop.
  Для Claude использован штатный Bash entrypoint после выявленного native требования
  предварительного Read перед Write существующего файла.
- Полный discovery окончательной композиции запущен с обязательными реальными
  runtime и Gradle fixtures. Параллельно выполняется повторный независимый DeepSeek
  review поздних recovery/security исправлений. Эти два результата пока ожидаются.
- Полный host discovery выполнил 2162 теста за 1361.244 секунды. Выявлены
  устаревшая empty-check readiness fixture после новой C6 admission validation и
  race test_dead_waiter_ticket_is_reclaimed между on_wait и публикацией ticket.
  Оба исправления маршрутизированы в отдельный test-only DeepSeek slice.
  Этот прогон завершился с ошибкой и не закрывает ворота выпуска.
- Обычный host namespace не содержит same-device nested mount внутри checkout.
  Условный storage-budget control отдельно выполнен без skip в приватной namespace
  на выбранной копии самого теста и каталоге mode 700 с настоящим bind mount.
  Код production и исходный checkout при этом не изменялись. Контроль прошёл.
- Для чтения GitHub Actions зарегистрировано native_capability исключение.
  Нативный агент имеет authenticated GitHub API, недоступный worker network с fixed
  provider relay. Его фактическая работа ограничена read-only CI monitoring.
  На точном baseline успешные GitHub Tests занимали 2–4 минуты, publish build
  3м55с. Запас существующего 20-минутного CI лимита подтверждён этими run records.
- Дополнительный test-only worker исправил readiness fixtures с раздельными
  current-plan и historical-ledger controls. Dead-waiter test ожидает настоящее
  flocked ticket перед SIGTERM. Controlled allocator proof и 100 повторений
  прошли. Actual delta содержит только два зарегистрированных test files.
  Координатор принял diff и выполнил оба полных модуля на Python 3.12 и 3.14,
  по 74 теста без skips. После интеграции запущен новый полный discovery.
- Повторный recovery review завершился с neutral verification failure в nested
  namespace. Подтверждён fixture-order defect в добавленном координатором I/O
  контроле. Production branch сохраняет копию при достижимом backend. Unit fixture
  теперь изолирует неиспользуемый до ошибки backend и проверяет факт обоих
  injected errors. Настоящие capture/mount tests сохраняют отдельную проверку.
  После осмотра результата выполнена явная подготовка и продолжение той же review
  assignment в той же копии. Новый brief ограничивает проверку поздними changes.
- Продолжение review завершилось. Принят semantic defect schema 2. После RED
  reader проверяет соответствие baseline_status/base_head, capture reason и durable
  finalization request. Host recovery101 и Python3.14 C2/C4 79 прошли без skips.
  Вывод о запрете recovery flag для planned row отклонён после воспроизведения
  обычного managed entrypoint. Первая failed baseline правдиво имеет одну lease
  attempt и пустую предыдущую ledger history. Ограничение reviewed predecessor
  применяется к переходу из уже failed row. Добавлен compatibility regression.
- Все принятые worker jobs явно финализированы после dry-run safe collector.
  Сохранены patches, attempt/rework/quality history. Финальный integration envelope
  проверен на 50 фактически backed files. Две соседние fixtures отдельно отражены
  в исходных feedback. Saved-copy schema fixture принадлежит исходному DeepSeek
  delta, пропущенному первоначальной картой файлов и затем включённому в scope
  correction как prepared source. Live-delegation fault fixtures адаптированы
  координатором в зарегистрированном scope с сохранением post-start controls.
- Полный итоговый discovery выполнил 2166 тестов за 1259.558 секунды. Единственный
  отказ сохранился в abrupt-death fixture. Controlled inherited SIGTERM handler
  воспроизвёл удаление ticket при мягком завершении. Координатор применил SIGKILL
  и проверку точного exit code. Это локальное fixture rework в исходном scope;
  новая minor/shared quality assessment добавлена без изменения прежних событий.
- При проверке integration backing воспроизведён C2 defect. После настоящего Git
  mismatch и discard логический job продолжал предоставлять integration files
  из исторического worker_changes. Новый security slice маршрутизирован Auto и
  оставлен координатору по high-risk. Pure authorization теперь отклоняет
  discarded/pending-discard evidence. До capture intent публикуется на всех
  связанных assignments с их собственными task/assignment IDs. Cross-task recovery
  следует actual latest lease binding. Worker delta и прежние решения сохранены.
- Новый public state regression проверяет completed capture как положительный
  контроль, отсутствие provenance после discard, interruption до ledger publication,
  parent-task complete без recapture и итоговое закрытие обеих задач. Host126 и
  Python3.14 new2 прошли. Полный discovery последней композиции запущен заново.
- Финальный host discovery на Python 3.12.3 прошёл 2167 тестов за 1246.878 секунды,
  включая обязательные реальные native runtimes и offline Gradle. Skips относятся
  к отдельному opt-in stress100 и отсутствующему внутри checkout nested mount.
  Последний control дополнительно прошёл в настоящей private mount namespace.
- Финальные wheel/sdist прошли strict Twine. Все текущие package Python/resource
  bytes сверены с wheel. Pip, pipx и uv install/replacement/uninstall lifecycle
  и установленный native-hook protocol canary прошли с сохранением внешнего config.
  Коммит, supported CI matrix, tag и проверка опубликованных registry artifacts
  выполняются после этих локальных gates.
