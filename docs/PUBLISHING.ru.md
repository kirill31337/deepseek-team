# Публикация DeepSeek Team

[English](https://github.com/kirill31337/deepseek-team/blob/main/docs/PUBLISHING.md) | **Русский**

Пакет **deepseek-team** устанавливает одноимённую команду `deepseek-team`. Версия **0.8.1** — первый выпуск в PyPI под этим названием. Публиковать пакет может только координатор; воркеры готовят изменения и выполняют проверки. API-ключи, данные авторизации, локальные настройки и полные журналы работы модели не должны попадать в репозиторий, обсуждения задач, логи или вывод CI.

Поддерживаются два способа публикации:

- **Вручную через Twine с токеном PyPI.** Координатор загружает заранее проверенные файлы выпуска. Этот способ выбран для первой публикации версии 0.8.1.
- **Через GitHub Actions с OIDC (Trusted Publishing).** Сценарий `publish-release.yml` может публиковать пакет без сохранённого API-токена. Для этого владелец должен связать проект PyPI с репозиторием GitHub и включить публикацию, как описано ниже. Сейчас этот способ **выключен**. Задание публикации в PyPI пропускается, пока не задано `PYPI_PUBLISHING_ENABLED=true`. Для TestPyPI нужна отдельная настройка; публикация туда запускается только вручную через `workflow_dispatch` с явным выбором TestPyPI.

## Публикация вручную через Twine

Этот способ подходит для публикации с вашего компьютера или сервера без настройки GitHub OIDC.

1. В PyPI откройте **Account settings → API tokens** и создайте API-токен с доступом только к проекту `deepseek-team`. Токен с доступом ко всему аккаунту допустим лишь для первой публикации, пока проект ещё не создан. Храните токен в секрете.
2. Запишите его **только** в файл `~/.pypirc`, доступный лишь вашему пользователю. Этот файл не должен попадать в Git:

   ```bash
   chmod 600 ~/.pypirc
   ```

   ```ini
   # ~/.pypirc  (права 0600, вне репозитория)
   [distutils]
   index-servers =
       pypi
       testpypi

   [pypi]
   repository = https://upload.pypi.org/legacy/
   username = __token__
   password = pypi-REPLACE-WITH-YOUR-TOKEN

   [testpypi]
   repository = https://test.pypi.org/legacy/
   username = __token__
   password = pypi-REPLACE-WITH-THE-TESTPYPI-TOKEN
   ```

   - Имя пользователя должно быть ровно `__token__`. В поле пароля укажите сам токен, а не пароль от аккаунта PyPI.
   - Используйте официальный адрес загрузки PyPI `https://upload.pypi.org/legacy/`, а для TestPyPI — `https://test.pypi.org/legacy/`. Не отправляйте токен через Twine на другие серверы.
   - Храните файл вне репозитория; стандартный путь — `~/.pypirc`. Не выводите токен на экран, не вставляйте его в логи, обсуждения задач или чат и не передавайте аргументом командной строки. Twine должен прочитать его из закрытого файла `~/.pypirc`. При утечке отзовите токен в PyPI и создайте новый.
3. Подготовьте и загрузите проверенные файлы по инструкции в следующем разделе.
4. Убедитесь, что файлы появились в PyPI, и проверьте установку в чистом окружении:

   ```bash
   pipx install deepseek-team==0.8.1
   deepseek-team --version
   ```

## Проверка и загрузка файлов выпуска

Эта последовательность подходит для любой версии. В PyPI загружаются уже собранный пакет wheel и архив исходников sdist, приложенные к выпуску с соответствующим тегом на GitHub. Пересобирать их не нужно. Укажите версию и выполните команды по порядку.

```bash
VERSION=0.8.1
mkdir -p "/tmp/dst-$VERSION" && cd "/tmp/dst-$VERSION"
gh release download "v$VERSION" --repo kirill31337/deepseek-team \
  --pattern '*.whl' --pattern '*.tar.gz'
ls -l
```

Проверьте скачанные файлы перед загрузкой:

```bash
# 1. Проверьте метаданные перед загрузкой в PyPI.
python -m twine check --strict *.whl *.tar.gz

# 2. Убедитесь, что в обоих файлах указаны правильные название пакета и версия.
VERSION="$VERSION" python - <<'PY'
import email.parser, glob, os, tarfile, zipfile
version = os.environ['VERSION']
wheel_path, = glob.glob(f'*{version}*.whl')
sdist_path, = glob.glob(f'*{version}*.tar.gz')
with zipfile.ZipFile(wheel_path) as archive:
    metadata_name, = [n for n in archive.namelist() if n.endswith('.dist-info/METADATA')]
    wheel = archive.read(metadata_name)
with tarfile.open(sdist_path) as archive:
    metadata_name, = [n for n in archive.getnames() if n.count('/') == 1 and n.endswith('/PKG-INFO')]
    sdist = archive.extractfile(metadata_name).read()
for raw in (wheel, sdist):
    metadata = email.parser.BytesParser().parsebytes(raw)
    assert metadata['Name'] == 'deepseek-team', metadata['Name']
    assert metadata['Version'] == version, metadata['Version']
print(f'Both distributions identify deepseek-team {version}.')
PY

# 3. Сверьте контрольные суммы скачанных файлов с данными выпуска на GitHub.
gh api "repos/kirill31337/deepseek-team/releases/tags/v$VERSION" \
  --jq '.assets[] | select(.name | test("\\.(whl|tar\\.gz)$")) | "\(.digest | sub("^sha256:"; ""))  \(.name)"' \
  > SHA256SUMS
sha256sum --check SHA256SUMS
```

После проверки загрузите файлы с помощью токена из `~/.pypirc`:

```bash
python -m twine check --strict *.whl *.tar.gz
python -m twine upload --repository pypi *.whl *.tar.gz
```

Параметр `--repository pypi` позволяет Twine прочитать токен из `~/.pypirc`, не раскрывая его в командной строке и истории команд. PyPI не позволяет заменить уже опубликованный файл с тем же именем. Если файл существует, сравните контрольные суммы; не пытайтесь загрузить его повторно, переименовать или пересобрать. Для изменённого кода выпустите новую версию с новым тегом.

## Настройка публикации через GitHub Actions с OIDC

Эта настройка необязательна и сейчас **выключена**. Если вы публикуете пакет вручную с токеном, пропустите раздел. Одного сценария GitHub Actions недостаточно: владелец аккаунта PyPI должен разрешить публикацию из этого репозитория через настройку Trusted Publisher.

1. Войдите в [PyPI](https://pypi.org/) и откройте [Publishing](https://pypi.org/manage/account/publishing/). Для проекта, который ещё не создан, добавьте **pending publisher** — разрешение на первую публикацию из GitHub — со значениями из таблицы ниже. Если проект уже существует и принадлежит вам, откройте Publishing в его настройках.
2. Создайте окружение `pypi` в разделе [Environments репозитория](https://github.com/kirill31337/deepseek-team/settings/environments). Разрешите публикацию в PyPI только из тегов версий (`v*`). При необходимости включите обязательное подтверждение публикации другим участником проекта.
3. Повторите настройку в [TestPyPI Publishing](https://test.pypi.org/manage/account/publishing/) для окружения `testpypi`. У TestPyPI отдельные аккаунты и разрешения на публикацию.
4. В [переменных GitHub Actions](https://github.com/kirill31337/deepseek-team/settings/variables/actions) задайте `PYPI_PUBLISHING_ENABLED` со значением ровно `true`. Без этой переменной выпуск на GitHub создаётся, но загрузка в PyPI пропускается. Переменная не содержит секрета и только включает задание публикации. Настройку Trusted Publisher в PyPI всё равно нужно выполнить.

| Поле | PyPI | TestPyPI |
| --- | --- | --- |
| Имя проекта в PyPI | `deepseek-team` | `deepseek-team` |
| Владелец репозитория в GitHub | `kirill31337` | `kirill31337` |
| Репозиторий | `deepseek-team` | `deepseek-team` |
| Файл сценария GitHub Actions | `publish-release.yml` | `publish-release.yml` |
| Окружение | `pypi` | `testpypi` |

В поле имени сценария на PyPI указывайте только имя файла, без `.github/workflows/`. Настройка pending publisher не резервирует название будущего проекта. После настройки GitHub OIDC выдаёт временные данные авторизации для каждого запуска, поэтому копировать API-токен вручную не требуется. Подробнее: [первая публикация через OIDC](https://docs.pypi.org/trusted-publishers/creating-a-project-through-oidc/) и [настройка Trusted Publishing](https://docs.pypi.org/trusted-publishers/adding-a-publisher/).

## Проверка версии перед выпуском

Создайте отдельное виртуальное окружение с Python 3.11 или новее:

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install build twine pipx uv
PYTHONPATH=src python3 -m unittest discover -s tests -v
python -m build
python -m twine check --strict dist/*
for installer in pip pipx uv; do
  python3 scripts/check_installation.py --installer "$installer" \
    --wheel dist/deepseek_team-0.8.1-py3-none-any.whl
done
```

Для каждого выпуска используйте пустой каталог `dist`. Скрипт проверяет установку, повторную установку и удаление текущей версии пакета в отдельных временных домашних каталогах. Он проверяет команду `deepseek-team` и файлы, входящие в пакет, а после удаления убеждается, что контрольные пользовательские данные сохранились. Ваши настоящие ключи и настройки координаторов при этом не используются; воркеры не запускаются.

Локальная сборка нужна для проверки версии, которую вы готовите к выпуску. Если выпуск с тегом уже существует, скачайте и опубликуйте приложенные к нему файлы по инструкции выше. Новая локальная сборка может отличаться от уже проверенной.

В CI установка проверяется на Python 3.11 и 3.13, а модульные тесты и сборка — также на Python 3.12. Эти проверки подтверждают работоспособность установки, замены и удаления пакета. Они не проверяют API-ключ и наличие всех системных компонентов песочницы. Готовность вашей машины с Linux проверьте командой `deepseek-team setup`.

## Пробная публикация в TestPyPI

Если OIDC настроен и сценарий уже находится в основной ветке, откройте [Publish release](https://github.com/kirill31337/deepseek-team/actions/workflows/publish-release.yml). Нажмите **Run workflow**, выберите ветку или тег проверяемой версии и укажите `target: testpypi`. Сценарий соберёт и проверит файлы, затем опубликует их только в TestPyPI.

Без OIDC используйте отдельный токен TestPyPI и команду `python -m twine upload --repository testpypi dist/*`.

Проверьте установку в новом временном окружении, скачивая пакет только из TestPyPI:

```bash
python3 -m venv /tmp/deepseek-team-testpypi
/tmp/deepseek-team-testpypi/bin/python -m pip install \
  --index-url https://test.pypi.org/simple/ --only-binary=:all: --no-deps \
  deepseek-team==0.8.1
/tmp/deepseek-team-testpypi/bin/deepseek-team --version
```

## Публикация в PyPI по тегу версии

Создание выпуска запускается при отправке тега версии. Обычные коммиты в `main` публикацию не запускают. Выпуски на GitHub уже создаются этим способом; для загрузки в PyPI нужно дополнительно настроить OIDC, как описано выше. Обновите номер версии в обоих местах, где он объявлен, и заметки в `docs/releases/VERSION.md`. После проверки и включения изменений в основную ветку создайте тег на проверенном коммите:

```bash
git tag -a v0.8.1 -m 'DeepSeek Team 0.8.1'
git push origin v0.8.1
```

Сценарий сверяет тег с номером версии, запускает тесты, собирает wheel и sdist, проверяет их метаданные и устанавливает полученный wheel через pip, pipx и uv. Затем отдельные задания публикуют эти же проверенные файлы на GitHub и в PyPI. Права `id-token: write` выдаются только заданиям публикации в PyPI и TestPyPI, а `contents: write` — только заданию создания выпуска на GitHub. Сами задания публикации не скачивают исходный код репозитория и не исполняют его. Шаг PyPA создаёт подтверждения происхождения файлов (attestations) через Trusted Publishing.

Если тег уже отправлен, а публикация в PyPI тогда была выключена, включите её и запустите сценарий вручную для **того же тега** с параметром `target: pypi`. Запуск из ветки или с тегом, не совпадающим с версией пакета, завершится ошибкой до загрузки файлов. Существующие файлы выпуска на GitHub сохранятся. Пока OIDC выключен, загружайте проверенные файлы вручную через Twine с токеном, без пересборки.

После публикации проверьте установку из PyPI в чистом окружении:

```bash
pipx install deepseek-team==0.8.1
deepseek-team --version
```

Убедитесь, что в README установка из PyPI указана как основной способ. Сообщайте о доступности пакета в PyPI только после успешной загрузки и проверки установки.

## Сбои и повторные попытки

- **Ошибка авторизации при ручной публикации:** проверьте, что токен действует, права `0600` у файла `~/.pypirc`, адрес `https://upload.pypi.org/legacy/` и имя пользователя `__token__`. Не выводите токен на экран при диагностике.
- **PyPI не принимает данные OIDC:** сверьте владельца, репозиторий, точное имя сценария и окружение со значениями в таблице. Настройки PyPI и TestPyPI проверяйте отдельно.
- **Задание публикации в PyPI пропущено:** проверьте `PYPI_PUBLISHING_ENABLED=true`, настройку Trusted Publisher, выбранный тег и параметр `target` при ручном запуске. Создание выпуска на GitHub ещё не означает, что пакет загружен в PyPI.
- **Версия уже опубликована:** проверьте существующие файлы и сравните контрольные суммы. Заменить их нельзя; для изменённого кода нужна новая версия.
- **Загрузка прервалась или оборвалось соединение:** сначала проверьте, какие файлы уже появились в PyPI. Twine и сценарий GitHub Actions не перезаписывают существующие файлы. Продолжайте загрузку только после проверки опубликованного содержимого.
- **Не прошли проверки установки:** исправьте ошибки до создания следующего тега выпуска. Не ослабляйте требования к песочнице воркеров ради прохождения проверок.
