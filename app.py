"""Image Build: a private, offline image watermark and archive workbench."""
from __future__ import annotations

import argparse
import io
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import sys
import threading
from datetime import date, datetime, timedelta, time as datetime_time
import uuid
import webbrowser

from flask import Flask, abort, jsonify, request, send_file, render_template
from PIL import Image, ImageDraw, ImageFont, ImageOps, UnidentifiedImageError
from PIL.PngImagePlugin import PngInfo

ROOT = Path(__file__).resolve().parent
DATA = Path(os.environ.get('IMAGE_BUILD_DATA', ROOT / '.local')).resolve()
DATA.mkdir(parents=True, exist_ok=True)
CONFIG = DATA / 'settings.json'
STATE = DATA / 'state.json'
KINDS = ('original', 'ready', 'archive')
EXTENSIONS = {'.jpg', '.jpeg', '.png', '.webp', '.bmp', '.tif', '.tiff'}
LOCK = threading.RLock()
TOKEN = secrets.token_urlsafe(32)
app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 25 * 1024 * 1024


def read_json(path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding='utf-8-sig'))
    except (ValueError, OSError) as exc:
        raise RuntimeError(f'无法读取 {path}，请恢复或修复该文件，避免丢失已有记录。') from exc


def save_json(path, value):
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    os.replace(temporary, path)


settings = read_json(CONFIG, {'folders': {}, 'auto_archive': True,
    'watermark': {'mode': 'text', 'text': '@Alexvhyn', 'position': 'bottom-right', 'size': 6,
                  'opacity': 75, 'color': '#ffffff'}})
state = read_json(STATE, {'ready_dates': {}, 'processed': {}})
state.setdefault('archive_dates', {})
state.setdefault('ready_timestamps', {})
JOBS = DATA / 'codex-jobs'
RUNNING = {}
ENVIRONMENT = {}


def codex_argv():
    command = codex_command()
    if not command:
        raise ValueError('未安装 Codex CLI，请双击 start.bat 安装环境。')
    if sys.platform == 'win32' and Path(command).suffix.lower() in ('.cmd', '.bat', '.ps1'):
        entry = Path(command).parent / 'node_modules' / '@openai' / 'codex' / 'bin' / 'codex.js'
        node = shutil.which('node')
        if not node or not entry.is_file():
            raise ValueError('Codex CLI 启动文件不可用，请运行 start.bat 修复安装。')
        return [node, str(entry)]
    return [command]


def environment_status(force=False):
    import time
    if not force and ENVIRONMENT.get('checked_at', 0) > time.monotonic() - 60:
        return ENVIRONMENT
    info = {'installed': bool(codex_command()), 'logged_in': False, 'image_generation': False,
            'version': '', 'error': '', 'checked_at': time.monotonic()}
    try:
        kwargs = dict(capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=15,
                      creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0)
        command = codex_argv()
        version = subprocess.run([*command, '--version'], **kwargs)
        info['version'] = version.stdout.strip()
        login = subprocess.run([*command, 'login', 'status'], **kwargs)
        info['logged_in'] = login.returncode == 0
        features = subprocess.run([*command, 'features', 'list'], **kwargs)
        info['image_generation'] = any(line.split()[0] == 'image_generation' and line.split()[-1] == 'true'
                                      for line in features.stdout.splitlines() if line.split())
        if not info['logged_in']:
            info['error'] = 'Codex 尚未登录，请运行 start.bat 完成登录。'
        elif not info['image_generation']:
            info['error'] = 'CLI 生图功能未开启，请运行 start.bat 更新并启用。'
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        info['error'] = str(exc)
    ENVIRONMENT.clear()
    ENVIRONMENT.update(info)
    return ENVIRONMENT


def consume_original(path, expected, output):
    """Remove the queue entry only after its complete replacement is durable."""
    if not path.exists():
        return
    if signature(path) != expected:
        raise ValueError('原图在处理期间被修改，已保留，请重新处理。')
    if path.resolve() == output.resolve():
        raise ValueError('源图片和成品路径不能相同。')
    with Image.open(output) as image:
        image.verify()
    path.unlink()


def clean_completed_originals():
    """Migrate the previous copy-based workflow without touching incomplete sources."""
    if not settings['folders'].get('original'):
        return
    for path in image_paths('original'):
        key = signature(path)
        output = state['processed'].get(key)
        if not output:
            continue
        replacement = Path(output)
        if not replacement.is_file():
            previous = next((entry.rsplit('|', 2)[1:] for entry in state['ready_dates']
                             if entry.rsplit('|', 2)[0] == str(replacement)), None)
            if previous:
                for kind in ('ready', 'archive'):
                    for candidate in image_paths(kind):
                        stat = candidate.stat()
                        if candidate.name == replacement.name and [str(stat.st_size), str(stat.st_mtime_ns)] == previous:
                            replacement = candidate
                            state['processed'][key] = str(candidate)
                            break
                    if replacement.is_file():
                        break
        if replacement.is_file():
            consume_original(path, key, replacement)


def archive_date(path):
    recorded = state['archive_dates'].get(signature(path))
    if recorded:
        return recorded
    parts = path.relative_to(folder('archive')).parts
    if len(parts) > 1:
        try:
            return date.fromisoformat(parts[0]).isoformat()
        except ValueError:
            pass
    return datetime.fromtimestamp(path.stat().st_mtime).date().isoformat()


def folder(kind):
    if kind not in KINDS or not settings['folders'].get(kind):
        raise ValueError('请先在设置中选择三个文件夹。')
    path = Path(settings['folders'][kind]).resolve()
    if not path.is_dir():
        raise ValueError(f'文件夹不可用，请检查磁盘连接或修改设置：{path}')
    return path


def image_paths(kind):
    base = folder(kind)
    # Never follow linked folders or files outside the selected directory.
    return sorted((p for p in base.rglob('*') if p.is_file() and not p.is_symlink()
                   and p.suffix.lower() in EXTENSIONS and p.resolve().is_relative_to(base)),
                  key=lambda p: (p.stat().st_mtime_ns, str(p)), reverse=True)


def resolve_image(kind, name):
    base = folder(kind)
    path = (base / name).resolve()
    if not path.is_relative_to(base) or not path.is_file() or path.suffix.lower() not in EXTENSIONS:
        raise ValueError('图片不存在或路径无效，请刷新列表。')
    return path


def signature(path):
    stat = path.stat()
    return f'{path.resolve()}|{stat.st_size}|{stat.st_mtime_ns}'


def unique_path(directory, name):
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    stem, suffix = path.stem, path.suffix
    count = 1
    while path.exists():
        path = directory / f'{stem}_{count}{suffix}'
        count += 1
    return path


def local_now():
    return datetime.now().astimezone()


def timestamped_name(stem):
    return f'{stem}__{local_now():%Y%m%d_%H%M%S_%f}.png'


def write_stamped_png(image, path, ready_at=None, created_at=None):
    now = ready_at or local_now().isoformat(timespec='microseconds')
    metadata = PngInfo()
    metadata.add_text('ImageBuildCreatedAt', created_at or now)
    metadata.add_text('ImageBuildReadyAt', now)
    image.save(path, 'PNG', pnginfo=metadata)


def ready_timestamp(path):
    key = signature(path)
    if key in state['ready_timestamps']:
        return state['ready_timestamps'][key]
    with Image.open(path) as image:
        embedded = image.info.get('ImageBuildReadyAt')
    if embedded:
        try:
            datetime.fromisoformat(embedded)
            stamp = embedded
        except ValueError:
            embedded = None
    if not embedded:
        legacy = state['ready_dates'].get(key)
        stamp = (datetime.combine(date.fromisoformat(legacy), datetime_time.min).astimezone().isoformat()
                 if legacy else datetime.fromtimestamp(path.stat().st_mtime).astimezone().isoformat())
    state['ready_timestamps'][key] = stamp
    state['ready_dates'][key] = datetime.fromisoformat(stamp).astimezone().date().isoformat()
    return stamp


def ready_date(path):
    return datetime.fromisoformat(ready_timestamp(path)).astimezone().date().isoformat()


def register_ready(path, stamp=None):
    if stamp is None and path.suffix.lower() == '.png':
        with Image.open(path) as image:
            stamp = image.info.get('ImageBuildReadyAt')
    value = stamp or local_now().isoformat(timespec='microseconds')
    state['ready_timestamps'][signature(path)] = value
    state['ready_dates'][signature(path)] = datetime.fromisoformat(value).astimezone().date().isoformat()


def archive_files(names=None, automatic=False):
    moved, errors = [], []
    today = local_now().date().isoformat()
    paths = image_paths('ready') if names is None else [resolve_image('ready', n) for n in names]
    for path in paths:
        try:
            day = ready_date(path)
            if automatic and day >= today:
                continue
            target = unique_path(folder('archive') / day, path.name)
            shutil.move(str(path), str(target))
            for key, output in state['processed'].items():
                if output == str(path):
                    state['processed'][key] = str(target)
            state['archive_dates'][signature(target)] = day
            moved.append(target.name)
        except (OSError, ValueError) as exc:
            errors.append(f'{path.name}：{exc}')
    save_json(STATE, state)
    return moved, errors


def watermark_options(raw):
    options = dict(settings['watermark'])
    options.update({k: v for k, v in raw.items() if k in options})
    if options['mode'] not in ('text', 'logo'):
        raise ValueError('请选择文字或 Logo 水印。')
    if options['position'] not in ('bottom-right', 'bottom-left', 'top-right', 'top-left', 'center'):
        raise ValueError('水印位置无效。')
    options['size'] = max(2, min(30, float(options['size'])))
    options['opacity'] = max(5, min(100, float(options['opacity'])))
    options['text'] = str(options['text']).strip()[:160]
    if options['mode'] == 'text' and not options['text']:
        raise ValueError('请输入水印文字。')
    if options['mode'] == 'logo' and not (DATA / 'logo.png').exists():
        raise ValueError('请先上传 Logo 图片。')
    color = str(options['color'])
    if len(color) != 7 or color[0] != '#' or any(c not in '0123456789abcdefABCDEF' for c in color[1:]):
        raise ValueError('水印颜色无效。')
    return options


def load_font(size):
    candidates = [ROOT / 'fonts' / 'watermark.ttf', Path('C:/Windows/Fonts/msyh.ttc'),
                  Path('C:/Windows/Fonts/arial.ttf'),
                  Path('/System/Library/Fonts/PingFang.ttc'),
                  Path('/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf')]
    for path in candidates:
        if path.exists():
            return ImageFont.truetype(str(path), size)
    return ImageFont.load_default(size=size)


def compose(path, options, preview=False):
    with Image.open(path) as source:
        if getattr(source, 'n_frames', 1) > 1:
            raise ValueError('暂不处理动态或多页图片，请使用静态图片。')
        image = ImageOps.exif_transpose(source).convert('RGBA')
    if preview:
        image.thumbnail((1200, 1200))
    width, height = image.size
    size = max(10, round(min(width, height) * options['size'] / 100))
    opacity = round(255 * options['opacity'] / 100)
    if options['mode'] == 'logo':
        with Image.open(DATA / 'logo.png') as logo:
            mark = logo.convert('RGBA')
        mark.thumbnail((max(1, round(width * options['size'] / 100 * 3)), size * 3))
        mark.putalpha(mark.getchannel('A').point(lambda p: p * opacity // 255))
    else:
        font = load_font(size)
        stroke = max(1, size // 24)
        box = font.getbbox(options['text'], stroke_width=stroke)
        mark = Image.new('RGBA', (box[2] - box[0] + 8, box[3] - box[1] + 8))
        ImageDraw.Draw(mark).text((4 - box[0], 4 - box[1]), options['text'], font=font,
            fill=options['color'] + f'{opacity:02x}', stroke_width=stroke,
            stroke_fill=(0, 0, 0, round(opacity * 0.6)))
    margin = max(4, round(min(width, height) * .025))
    max_w, max_h = max(1, width - margin * 2), max(1, height - margin * 2)
    mark.thumbnail((max_w, max_h))
    position = options['position']
    x = margin if 'left' in position else width - mark.width - margin
    y = margin if 'top' in position else height - mark.height - margin
    if position == 'center':
        x, y = (width - mark.width) // 2, (height - mark.height) // 2
    image.alpha_composite(mark, (max(0, x), max(0, y)))
    return image


@app.before_request
def local_only():
    if request.host.split(':')[0] not in ('127.0.0.1', 'localhost'):
        abort(403)
    if request.method == 'POST' and request.headers.get('X-Workspace-Token') != TOKEN:
        abort(403)


@app.errorhandler(Exception)
def failure(exc):
    from werkzeug.exceptions import HTTPException
    code = exc.code if isinstance(exc, HTTPException) else 400
    return jsonify(error=str(exc)), code


@app.get('/')
def index():
    return render_template('index.html', token=TOKEN)


@app.get('/api/health')
def health():
    return jsonify(app='image-build', root=str(ROOT))


@app.get('/api/state')
def get_state():
    with LOCK:
        import_codex_results()
        try:
            clean_completed_originals()
            cleanup_error = ''
        except (ValueError, OSError) as exc:
            cleanup_error = str(exc)
        items, errors = {}, []
        if cleanup_error:
            errors.append(cleanup_error)
        for kind in KINDS:
            items[kind] = []
            if not settings['folders'].get(kind):
                continue
            try:
                for path in image_paths(kind):
                    day = ready_date(path) if kind == 'ready' else archive_date(path) if kind == 'archive' else datetime.fromtimestamp(path.stat().st_mtime).date().isoformat()
                    items[kind].append({'name': path.relative_to(folder(kind)).as_posix(),
                        'filename': path.name, 'day': day, 'bytes': path.stat().st_size,
                        'processed': signature(path) in state['processed'],
                        'timestamp': ready_timestamp(path) if kind == 'ready' else None})
            except (ValueError, OSError) as exc:
                errors.append(str(exc))
        save_json(STATE, state)
        return jsonify(settings=settings, items=items, errors=errors,
                       today=date.today().isoformat(), logo=(DATA / 'logo.png').exists(),
                       codex_available=bool(codex_command()), environment=environment_status(), jobs=codex_jobs())


@app.post('/api/environment')
def check_environment():
    return jsonify(environment_status(force=True))


def codex_command():
    return shutil.which('codex.cmd') or shutil.which('codex')


def codex_jobs():
    if not JOBS.exists():
        return []
    jobs = []
    for path in sorted(JOBS.glob('*/job.json'), reverse=True):
        job = read_json(path, {})
        log_path = path.parent / 'events.jsonl'
        if job.get('status') == 'running' and log_path.exists():
            with log_path.open('rb') as log:
                log.seek(max(0, log_path.stat().st_size - 20000))
                lines = log.read().decode('utf-8', errors='replace').splitlines()
            for line in lines:
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if event.get('type') == 'error' and 'Reconnecting' in event.get('message', ''):
                    job['progress'] = '模型连接暂时超时，CLI 正在自动重试…'
                item = event.get('item', {})
                if item.get('type') == 'agent_message':
                    job['progress'] = item.get('text', '')[:250]
        jobs.append({k: job.get(k) for k in ('id', 'created', 'status', 'count', 'imported', 'error', 'progress')})
    return jobs[:15]


def import_codex_results():
    if not JOBS.exists():
        return
    for manifest in JOBS.glob('*/job.json'):
        job = read_json(manifest, {})
        if job.get('status') not in ('waiting', 'running', 'partial'):
            continue
        directory = manifest.parent
        result_file = directory / 'output' / 'result.json'
        if not result_file.exists():
            error_file = directory / 'error.txt'
            if error_file.exists():
                job['error'] = error_file.read_text(encoding='utf-8', errors='replace')[:2000]
                save_json(manifest, job)
            continue
        try:
            result = read_json(result_file, {})
            entries = result.get('outputs', [])
            sources = {item['id']: item for item in job['sources']}
            done = job.setdefault('imported', {})
            job['error'] = ''
            for entry in entries:
                identifier = entry['id']
                if identifier in done:
                    continue
                if identifier not in sources:
                    raise ValueError('成品清单含未知原图编号。')
                source = (directory / 'output' / entry['file']).resolve()
                if not source.is_relative_to((directory / 'output').resolve()) or not source.is_file():
                    raise ValueError('成品路径无效或图片还未保存完成。')
                with Image.open(source) as image:
                    image.verify()
                # Never redirect a pending job to folders selected after it was launched.
                destination = Path(job['destination'])
                if not destination.is_dir():
                    raise ValueError('任务启动时选择的成品文件夹不可用。')
                target = unique_path(destination / local_now().date().isoformat(), timestamped_name(sources[identifier]['stem'] + '_codex'))
                with Image.open(source) as image:
                    image = ImageOps.exif_transpose(image).convert('RGBA')
                    temporary = target.with_suffix('.tmp')
                    write_stamped_png(image, temporary)
                    os.replace(temporary, target)
                original = Path(sources[identifier].get('path', sources[identifier]['signature'].rsplit('|', 2)[0]))
                consume_original(original, sources[identifier]['signature'], target)
                done[identifier] = str(target)
                register_ready(target)
                state['processed'][sources[identifier]['signature']] = str(target)
                staged_input = (directory / sources[identifier]['input']).resolve()
                if staged_input.is_relative_to((directory / 'input').resolve()):
                    staged_input.unlink(missing_ok=True)
                source.unlink(missing_ok=True)
                # Persist the per-image receipt before the next polling cycle.
                save_json(manifest, job)
                save_json(STATE, state)
            job['status'] = 'completed' if len(done) == job['count'] else 'partial'
        except (ValueError, OSError, KeyError, TypeError, RuntimeError) as exc:
            job['error'] = str(exc)
        save_json(manifest, job)


def launch_codex(directory):
    # Read the prompt from stdin; no shell parsing of user text or file paths.
    log = (directory / 'events.jsonl').open('wb')
    try:
        process = subprocess.Popen([*codex_argv(), '-a', 'never', 'exec', '--skip-git-repo-check',
                    '--sandbox', 'workspace-write', '--enable', 'image_generation', '--json',
                    '--output-last-message', str(directory / 'last-message.txt'), '-'],
                    cwd=str(directory), stdin=subprocess.PIPE, stdout=log, stderr=log,
                    creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0)
        process.stdin.write((directory / 'prompt.txt').read_bytes())
        process.stdin.close()
    finally:
        log.close()
    RUNNING[directory.name] = process
    threading.Thread(target=monitor_codex, args=(directory, process), daemon=True).start()


def monitor_codex(directory, process):
    code = process.wait()
    with LOCK:
        import_codex_results()
        job = read_json(directory / 'job.json', {})
        if job.get('status') != 'completed':
            job['status'] = 'failed'
            message = directory / 'last-message.txt'
            detail = message.read_text(encoding='utf-8', errors='replace')[-2000:] if message.exists() else ''
            if not detail:
                detail = (directory / 'events.jsonl').read_text(encoding='utf-8', errors='replace')[-2000:]
            job['error'] = job.get('error') or f'Codex 已结束（退出码 {code}），未交付完整成品。{detail}'
        job['progress'] = '成品已导入' if job.get('status') == 'completed' else '任务结束，请查看原因'
        save_json(directory / 'job.json', job)
        RUNNING.pop(directory.name, None)


@app.post('/api/codex/start')
def start_codex():
    with LOCK:
        raw = request.json
        options = watermark_options(raw['watermark'])
        names = list(dict.fromkeys(raw['names']))
        if not names:
            raise ValueError('请先选择图片。')
        environment = environment_status()
        if environment['error']:
            raise ValueError(environment['error'])
        destination = folder('ready')
        paths = [resolve_image('original', name) for name in names]
        identifier = datetime.now().strftime('%Y%m%d-%H%M%S-') + uuid.uuid4().hex[:6]
        directory = JOBS / identifier
        (directory / 'input').mkdir(parents=True)
        (directory / 'output').mkdir()
        sources = []
        for index, path in enumerate(paths, 1):
            number = f'{index:03d}'
            target = directory / 'input' / (number + path.suffix.lower())
            shutil.copy2(path, target)
            sources.append({'id': number, 'input': target.relative_to(directory).as_posix(),
                            'stem': path.stem, 'path': str(path), 'signature': signature(path)})
        if options['mode'] == 'logo':
            shutil.copy2(DATA / 'logo.png', directory / 'logo.png')
        creative = str(raw.get('creative', '')).strip()[:4000]
        prompt = ('请完成图片编辑任务，使用当前 Codex 环境可用的生图/改图工具。\n'
                  '这是创意改图，不要用 Pillow、Canvas 或直接文字叠加冒充 AI 改图。\n'
                  '先查看 input/ 中的每张原图，保留主要人物、主体和构图；按用户要求融合设计水印。\n'
                  f'水印配置：{json.dumps(options, ensure_ascii=False)}。图片水印参考 logo.png（如存在）。\n'
                  f'用户的改图要求：{creative or "根据各图片的风格设计协调、精致、可辨识的水印，不遮挡主体。"}\n'
                  '文字水印必须逐字准确。每张原图输出一张独立成品，保持画幅，不覆盖输入文件。\n'
                  f'原图编号与路径：{json.dumps([{k: s[k] for k in ("id", "input")} for s in sources], ensure_ascii=False)}\n'
                  '将最终成品保存到 output/<编号>.png。确认每张图片完整写入后，最后写 output/result.json：\n'
                  '{"outputs":[{"id":"001","file":"001.png"}]}，每张成功图片一项。\n'
                  '工作台会根据此清单自动导入，未完成时不要提前写清单。\n'
                  '如果环境没有生图能力，或生成失败，请把实际原因写入 error.txt 并告诉用户；'
                  '不要假装完成，不要自行请求 API Key 或切换付费 API，不要修改任务目录外的项目文件。')
        (directory / 'prompt.txt').write_text(prompt, encoding='utf-8')
        (directory / 'AGENTS.md').write_text('仅操作当前图片任务目录。具体需求见 prompt.txt。保留 input/ 原图。', encoding='utf-8')
        import shlex
        script = '#!/bin/sh\ncd ' + shlex.quote(str(directory)) + '\nexec codex --no-alt-screen "Read prompt.txt and complete this image editing task using available image generation tools."\n'
        for name in ('launch.sh', 'launch.command'):
            path = directory / name
            path.write_text(script, encoding='utf-8')
            path.chmod(0o755)
        job = {'id': identifier, 'created': datetime.now().isoformat(timespec='seconds'),
               'status': 'running', 'count': len(sources), 'imported': {}, 'sources': sources,
               'destination': str(destination), 'error': '', 'progress': 'Codex 正在自动执行改图'}
        save_json(directory / 'job.json', job)
        try:
            launch_codex(directory)
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            job['status'] = 'failed'
            job['error'] = str(exc)
            save_json(directory / 'job.json', job)
            raise
        settings['watermark'] = options
        save_json(CONFIG, settings)
        return jsonify(id=identifier, count=len(sources))


@app.post('/api/codex/open')
def open_codex_job():
    identifier = str(request.json['id'])
    directory = (JOBS / identifier).resolve()
    if not directory.is_relative_to(JOBS.resolve()) or not (directory / 'job.json').is_file():
        raise ValueError('任务不存在。')
    if sys.platform == 'win32':
        os.startfile(str(directory))
    elif sys.platform == 'darwin':
        subprocess.Popen(['open', str(directory)])
    else:
        subprocess.Popen(['xdg-open', str(directory)])
    return jsonify(ok=True)


@app.post('/api/codex/retry')
def retry_codex_job():
    with LOCK:
        directory = (JOBS / str(request.json['id'])).resolve()
        if not directory.is_relative_to(JOBS.resolve()) or not (directory / 'job.json').is_file():
            raise ValueError('任务不存在。')
        job = read_json(directory / 'job.json', {})
        if job['status'] not in ('failed', 'waiting', 'partial') or directory.name in RUNNING:
            raise ValueError('任务正在执行或已经完成。')
        if environment_status()['error']:
            raise ValueError(environment_status()['error'])
        job.update(status='running', error='', progress='Codex 正在自动执行改图')
        (directory / 'error.txt').unlink(missing_ok=True)
        save_json(directory / 'job.json', job)
        launch_codex(directory)
        return jsonify(ok=True)


@app.post('/api/settings')
def update_settings():
    with LOCK:
        raw = request.get_json()
        if 'folders' in raw:
            folders = {}
            for kind in KINDS:
                path = Path(str(raw['folders'].get(kind, '')).strip()).expanduser()
                if not str(raw['folders'].get(kind, '')).strip() or not path.is_absolute() or not path.is_dir():
                    raise ValueError('请为三个分类分别选择已存在的文件夹。')
                folders[kind] = str(path.resolve())
            paths = [Path(p) for p in folders.values()]
            for i, left in enumerate(paths):
                for right in paths[i + 1:]:
                    if left.is_relative_to(right) or right.is_relative_to(left):
                        raise ValueError('三个文件夹不能相同，也不能互相包含，请选择三个独立文件夹。')
            settings['folders'] = folders
        if 'auto_archive' in raw:
            settings['auto_archive'] = bool(raw['auto_archive'])
        if 'watermark' in raw:
            options = watermark_options(raw['watermark'])
            settings['watermark'] = options
        save_json(CONFIG, settings)
        return jsonify(ok=True)


@app.post('/api/pick-folder')
def pick_folder():
    result = subprocess.run([sys.executable, str(ROOT / 'choose_folder.py')],
                            capture_output=True, timeout=180)
    if result.returncode:
        raise ValueError('无法打开文件夹选择器，请直接粘贴文件夹的完整路径。')
    return jsonify(path=result.stdout.decode('utf-8').strip())


@app.post('/api/open-folder')
def open_folder():
    path = folder(request.json['kind'])
    if sys.platform == 'win32':
        os.startfile(str(path))
    elif sys.platform == 'darwin':
        subprocess.Popen(['open', str(path)])
    else:
        subprocess.Popen(['xdg-open', str(path)])
    return jsonify(ok=True)


@app.get('/api/image/<kind>/<path:name>')
def get_image(kind, name):
    with LOCK:
        path = resolve_image(kind, name)
        if request.args.get('thumb'):
            with Image.open(path) as original:
                image = ImageOps.exif_transpose(original).convert('RGB')
                image.thumbnail((480, 480))
                output = io.BytesIO()
                image.save(output, 'JPEG', quality=82)
            output.seek(0)
            return send_file(output, mimetype='image/jpeg', max_age=60)
        return send_file(path, as_attachment=request.args.get('download') == '1')


@app.post('/api/logo')
def upload_logo():
    with LOCK:
        with Image.open(request.files['file'].stream) as image:
            if image.width * image.height > 25_000_000:
                raise ValueError('Logo 过大，请使用小于 2500 万像素的图片。')
            image = ImageOps.exif_transpose(image).convert('RGBA')
            image.thumbnail((2400, 2400))
            image.save(DATA / 'logo.png', 'PNG')
        return jsonify(ok=True)


@app.post('/api/preview')
def preview():
    with LOCK:
        raw = request.json
        image = compose(resolve_image('original', raw['name']), watermark_options(raw['watermark']), True)
        output = io.BytesIO()
        image.save(output, 'PNG')
        output.seek(0)
        return send_file(output, mimetype='image/png')


@app.post('/api/watermark')
def watermark():
    with LOCK:
        raw = request.json
        options = watermark_options(raw['watermark'])
        names = list(dict.fromkeys(raw['names']))
        results, errors = [], []
        for name in names:
            temporary = None
            try:
                path = resolve_image('original', name)
                key = signature(path)
                if key in state['processed']:
                    continue
                image = compose(path, options)
                target = unique_path(folder('ready') / local_now().date().isoformat(), timestamped_name(path.stem))
                temporary = target.with_suffix('.' + uuid.uuid4().hex + '.tmp')
                write_stamped_png(image, temporary)
                os.replace(temporary, target)
                consume_original(path, key, target)
                register_ready(target)
                state['processed'][key] = str(target)
                save_json(STATE, state)
                results.append(target.name)
            except (ValueError, OSError, UnidentifiedImageError) as exc:
                errors.append(f'{name}：{exc}')
            finally:
                if temporary and temporary.exists():
                    temporary.unlink()
        settings['watermark'] = options
        save_json(CONFIG, settings)
        return jsonify(count=len(results), errors=errors)


@app.post('/api/archive')
def archive():
    with LOCK:
        moved, errors = archive_files(list(dict.fromkeys(request.json['names'])))
        return jsonify(count=len(moved), errors=errors)


@app.post('/api/restore')
def restore():
    with LOCK:
        moved, errors = [], []
        for name in dict.fromkeys(request.json['names']):
            try:
                source = resolve_image('archive', name)
                target = unique_path(folder('ready') / local_now().date().isoformat(), source.name)
                shutil.move(str(source), str(target))
                for key, output in state['processed'].items():
                    if output == str(source):
                        state['processed'][key] = str(target)
                # Reset the queue timestamp on restore while preserving generation time.
                if target.suffix.lower() == '.png':
                    with Image.open(target) as image:
                        created = image.info.get('ImageBuildCreatedAt')
                        image.load()
                        temporary = target.with_suffix('.restore.tmp')
                        write_stamped_png(image, temporary, created_at=created)
                    os.replace(temporary, target)
                register_ready(target)
                moved.append(name)
            except (ValueError, OSError) as exc:
                errors.append(f'{name}：{exc}')
        save_json(STATE, state)
        return jsonify(count=len(moved), errors=errors)


def maintenance_delay():
    now = local_now()
    midnight = datetime.combine(now.date() + timedelta(days=1), datetime_time.min).astimezone()
    return min(30, max(.05, (midnight - now).total_seconds()))


def archive_worker():
    # Owned by the launcher/server process, independent of browser tabs or requests.
    while not stop_worker.wait(maintenance_delay()):
        with LOCK:
            import_codex_results()
            if settings.get('auto_archive') and all(settings['folders'].get(k) for k in KINDS):
                try:
                    archive_files(automatic=True)
                except (ValueError, OSError) as exc:
                    print(f'自动归档暂未完成：{exc}', flush=True)


stop_worker = threading.Event()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--no-browser', action='store_true')
    parser.add_argument('--port', type=int, default=8765)
    args = parser.parse_args()
    # Reopening the launcher reuses the running workspace instead of creating two writers.
    import urllib.request
    from waitress import create_server
    for port in range(args.port, args.port + 20):
        url = f'http://127.0.0.1:{port}'
        try:
            with urllib.request.urlopen(url + '/api/health', timeout=.3) as response:
                existing = json.load(response)
            if existing.get('root') == str(ROOT):
                if not args.no_browser:
                    webbrowser.open(url)
                print(f'工作台已运行：{url}')
                return
        except Exception:
            pass
        try:
            server = create_server(app, host='127.0.0.1', port=port, threads=6)
            break
        except OSError:
            continue
    else:
        raise RuntimeError('没有可用的本地端口，请关闭之前的工作台后重试。')
    with LOCK:
        if settings.get('auto_archive') and all(settings['folders'].get(k) for k in KINDS):
            try:
                archive_files(automatic=True)
            except (ValueError, OSError) as exc:
                print(f'自动归档暂未完成：{exc}')
    threading.Thread(target=archive_worker, daemon=True).start()
    print(f'工作台已启动：{url}', flush=True)
    if not args.no_browser:
        webbrowser.open(url)
    try:
        server.run()
    finally:
        stop_worker.set()
        server.close()


if __name__ == '__main__':
    main()
