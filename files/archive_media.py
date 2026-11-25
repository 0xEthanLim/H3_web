"""Archive completed Toybox media and retain legacy originals on Drive."""

import hashlib
import mimetypes
import os
import re
import shutil
import subprocess
import threading
import time
from collections import Counter
from functools import lru_cache
from contextlib import contextmanager, nullcontext
from pathlib import Path
from uuid import uuid4

MEDIA = {'.mp4': 'videos', '.webm': 'videos', '.mkv': 'videos', '.mov': 'videos', '.png': 'images',
         '.jpg': 'images', '.jpeg': 'images', '.webp': 'images'}


def _kind(path):
    if path.name.startswith('_') or path.stem.lower().endswith(('_part', '_src')):
        return None
    return MEDIA.get(path.suffix.lower())


def _signature(path):
    stat = path.stat()
    return stat.st_size, stat.st_mtime_ns


def _digest(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


@contextmanager
def media_lock(path):
    # A local file makes the archive thread and the HTTP process share one lock.
    with path.open('a+b') as handle:
        if os.name == 'nt':
            import msvcrt
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl
            fcntl.flock(handle, fcntl.LOCK_EX)
        yield


def _archive_variant(target, digest):
    return target.with_name(f'{target.stem[:80]}--{digest}{target.suffix}')


def _copy_complete(source, target, signature):
    """Publish only a complete copy of an unchanged source, on the target volume."""
    temporary = target.with_name(f'.h3-{uuid4().hex}.tmp')
    try:
        shutil.copyfile(source, temporary)
        if _signature(source) != signature or temporary.stat().st_size != signature[0]:
            raise OSError('源文件在复制期间变化，将在以后重试')
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)


# ── mp4 faststart：把 moov 索引从文件末尾挪到开头 ──
# ComfyUI 的 SaveVideo 用 ffmpeg 默认设置封装 mp4，moov 落在文件尾部。
# 浏览器必须先读到 moov 才知道每一帧在哪，所以索引在尾部 = 必须把整个文件
# 下完才能出第一帧。实测同一部 4.56 MB 的片子：默认 moov 偏移 4,552,974（末尾），
# 加 -movflags +faststart 后偏移 32（开头）。前者要下完，后者读几 KB 就能播。
#
# -c copy 只搬数据、不重新编码：画质零损失、体积基本不变，耗时约 1 秒。
def _ffmpeg_exe():
    exe = shutil.which('ffmpeg')
    if exe:
        return exe
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None


def _moov_is_late(path):
    """回 (需要处理, 说明)。读不出 box 结构时保守地当作需要处理。"""
    try:
        with path.open('rb') as handle:
            total = path.stat().st_size
            position = 0
            while position < total:
                handle.seek(position)
                head = handle.read(8)
                if len(head) < 8:
                    return True, 'box 结构读不完'
                size = int.from_bytes(head[0:4], 'big')
                kind = head[4:8]
                if size == 1:                      # 64 位长度
                    size = int.from_bytes(handle.read(8), 'big')
                if size < 8:
                    return True, 'box 长度异常'
                if kind == b'moov':
                    # 索引在前 4 KB 内就算已经 faststart
                    return (position > 4096), 'moov@%d/%d' % (position, total)
                position += size
        return True, '找不到 moov'
    except OSError:
        return True, '读不了文件'


def faststart_in_place(path, lock_path=None):
    """把 mp4 的 moov 挪到文件开头。返回 (是否改动, 说明)。

    写临时文件再原子替换：绝不原地改写正在被读的文件（归档线程和网页都可能
    同时在读它）。任何一步失败都保留原文件 —— 播放慢一点，总比片子坏掉好。
    """
    if path.suffix.lower() not in ('.mp4', '.mov'):
        return False, '不是 mp4/mov，跳过'
    late, detail = _moov_is_late(path)
    if not late:
        return False, '已经是 faststart（%s）' % detail
    exe = _ffmpeg_exe()
    if not exe:
        return False, '找不到 ffmpeg'
    temporary = path.with_name('.h3fast-%s.tmp' % uuid4().hex)
    try:
        if lock_path:
            with media_lock(lock_path):
                result = subprocess.run(
                    [exe, '-y', '-loglevel', 'error', '-i', str(path),
                     '-map', '0', '-c', 'copy', '-movflags', '+faststart', '-f', 'mp4', str(temporary)],
                    capture_output=True, timeout=600)
        else:
            result = subprocess.run(
                [exe, '-y', '-loglevel', 'error', '-i', str(path),
                 '-map', '0', '-c', 'copy', '-movflags', '+faststart', '-f', 'mp4', str(temporary)],
                capture_output=True, timeout=600)
        if result.returncode != 0 or not temporary.is_file() or not temporary.stat().st_size:
            return False, 'ffmpeg 失败：%s' % (result.stderr.decode('utf-8', 'ignore')[:120] or '无输出')
        # 复核：新文件的 moov 必须真的到前面了，否则不换
        still_late, why = _moov_is_late(temporary)
        if still_late:
            return False, 'remux 后 moov 仍在后面（%s）' % why
        temporary.replace(path)
        return True, 'moov 已前置（%s → faststart）' % detail
    except (OSError, subprocess.SubprocessError) as error:
        return False, '%s: %s' % (type(error).__name__, str(error)[:100])
    finally:
        temporary.unlink(missing_ok=True)


class MediaArchive:
    def __init__(self, root, lock_path=None):
        self.root = root
        self.lock_path = lock_path
        self.seen = {}
        self.completed = {}
        self.last_scan_failed = False
        for kind in set(MEDIA.values()):
            (root / kind).mkdir(parents=True, exist_ok=True)

    def scan(self, roots, *, require_stable=True, stop=None):
        counts = dict(videos=0, images=0)
        seen = {}
        self.last_scan_failed = False
        for root in roots:
            if stop is not None and stop.is_set():
                return counts['videos'], counts['images']
            if not root.is_dir() or root.is_symlink():
                continue
            for source in root.rglob('*'):
                if stop is not None and stop.is_set():
                    return counts['videos'], counts['images']
                kind = _kind(source)
                if not kind or source.is_symlink() or not source.is_file():
                    continue
                try:
                    with media_lock(self.lock_path) if self.lock_path else nullcontext():
                        if stop is not None and stop.is_set():
                            return counts['videos'], counts['images']
                        if not source.is_file() or source.is_symlink():
                            continue
                        signature = _signature(source)
                        seen[source] = signature
                        if not signature[0] or (require_stable and self.seen.get(source) != signature):
                            continue
                        previous = self.completed.get(source)
                        if previous and previous[0] == signature and previous[1].is_file() and _signature(previous[1])[0] == signature[0]:
                            continue
                        target, added = self._archive(source, kind, signature)
                        self.completed[source] = signature, target
                        counts[kind] += added
                except OSError as error:
                    self.last_scan_failed = True
                    print('⚠️ 归档失败，将保留原文件：', source.name, error, flush=True)
        if require_stable:
            self.seen = seen
            self.completed = {path: result for path, result in self.completed.items()
                              if self.seen.get(path) == result[0]}
        return counts['videos'], counts['images']

    def _archive(self, source, kind, signature):
        target = self.root / kind / source.name
        if target.exists():
            digest = _digest(source)
            if target.is_file() and _signature(target)[0] == signature[0] and _digest(target) == digest:
                return target, False
            # 同名不同内容各存一份。哈希后缀让重跑仍然找到同一份成品。
            target = _archive_variant(target, digest)
            if target.is_file() and _signature(target)[0] == signature[0] and _digest(target) == digest:
                return target, False
            if target.exists():
                raise OSError('同名归档冲突，请检查云盘目标文件')
        _copy_complete(source, target, signature)
        # 刚写完就立刻让缓存失效：不然网页最多要晚 ARCHIVE_TTL 秒才看到「已归档」
        _forget_archived(target)
        return target, True


def _name(text):
    if (not isinstance(text, str) or not text or text in ('.', '..')
            or any(c in text for c in '/\\:\x00')):
        raise ValueError('任务的成品文件名无效。')
    return text


def _safe_file(root, relative):
    path = root.joinpath(*relative)
    # Check the configured root and nested paths before following or deleting anything.
    if path.resolve() != root.resolve().joinpath(*relative) or root.is_symlink():
        raise ValueError('成品路径指向其他位置，无法安全删除。')
    if path.exists() and not path.is_file():
        raise ValueError('成品路径不是文件。')
    return path


OUTPUT_FIELDS = ('image', 'video', 'full_video')
FINISHED = ('done', 'error', 'interrupted')


def _job_file(job, field):
    item = job.get(field)
    if item is None or (field == 'full_video' and item == ''):
        return None
    if field == 'full_video':
        name = _name(item)
        if name != _name(job['id']) + '_full.mp4':
            raise ValueError('无法确认拼接视频属于这个任务。')
        return 'outputs', (name,), True
    if not isinstance(item, dict) or item.get('type', 'output') != 'output':
        raise ValueError('只支持生成任务的输出文件。')
    folder = item.get('subfolder', '')
    if folder not in ('', 'H3Toybox'):
        raise ValueError('这个任务的输出目录无法安全确认。')
    name = _name(item.get('filename'))
    token = job.get('h3_output_token')
    unique = (isinstance(token, str) and re.fullmatch(r'[0-9a-f]{32}', token)
              and name.startswith(('h3t_' + token + '_', 'img_' + token + '_')))
    return 'output', ((folder,) if folder else ()) + (name,), bool(unique)


def _job_files(job):
    for field in OUTPUT_FIELDS:
        descriptor = _job_file(job, field)
        if descriptor is not None:
            yield descriptor


@lru_cache(maxsize=512)
def _checked_digest(path, signature):
    digest = _digest(path)
    if _signature(path) != signature:
        raise OSError('校验期间文件发生变化')
    return digest


def _matching_digest(path):
    return _checked_digest(path, _signature(path))


def _present_file(root, relative):
    try:
        path = _safe_file(root, relative)
        return path if path.is_file() and path.stat().st_size else None
    except (OSError, ValueError, RuntimeError):
        return None


# ── 云盘 stat 缓存 ──
# 云盘（DriveFS）的一次 stat 要 20~50 ms，本机只要 0.3 ms。而 /api/jobs 每 2 秒
# 就问一遍每个已完成任务的归档状态：20 个任务 × 3 次云盘 stat ≈ 1.2~3 秒，
# 比轮询间隔还长，请求就开始排队 —— 表现就是「显示成功了但画面迟迟不出来」。
#
# 归档是后台线程每 30 秒才干一次的事，答案在这 30 秒里基本不变。缓存 10 秒
# 完全不损伤正确性（最坏情况：归档刚完成，卡片晚 10 秒显示「已归档」）。
# 只缓存云盘那几次探测；本机 stat 便宜，照旧实时打。
ARCHIVE_TTL = 10.0
_archive_cache = {}          # (root, relative) -> (到期时刻, 路径或 None)
_cache_lock = threading.Lock()


def _cached_present(root, relative, ttl=None):
    """云盘专用：同样的 (root, relative) 在 ttl 秒内复用上次结果。"""
    key = (str(root), tuple(relative))
    now = time.monotonic()
    with _cache_lock:
        hit = _archive_cache.get(key)
        if hit and hit[0] > now:
            return hit[1]
    # 缓存未命中：真去问云盘。放在锁外，避免一次慢 stat 卡住所有请求。
    found = _present_file(root, relative)
    with _cache_lock:
        _archive_cache[key] = (now + (ARCHIVE_TTL if ttl is None else ttl), found)
    return found


def _forget_archived(path):
    """归档刚落地时调用：把所有指向这个文件的缓存项作废，网页立刻能看到。"""
    try:
        name = path.name
    except AttributeError:
        return
    with _cache_lock:
        for key in [k for k in _archive_cache if k[1][-1] == name]:
            _archive_cache.pop(key, None)


class JobMedia:
    """Locate a finished job's files using only server-owned metadata."""

    def __init__(self, jobs, output, outputs, legacy_local, archive, data, drive_mount):
        self.output = output
        self.outputs = outputs
        self.legacy_local = legacy_local
        self.data = data
        self.drive_expected = drive_mount is not None
        self.archive = archive if drive_mount and drive_mount.is_dir() else None
        self.names = Counter(
            name for job in jobs
            for field in ('image', 'video')
            if isinstance(item := job.get(field), dict)
            if isinstance(name := item.get('filename'), str)
        )

    def locate(self, job, field):
        """Return (local preview, published archive, verified old Drive original)."""
        if job.get('status') not in FINISHED or field not in OUTPUT_FIELDS:
            return None, None, None
        try:
            return self._locate(job, field)
        except (OSError, ValueError, RuntimeError):
            return None, None, None

    def _locate(self, job, field):
        descriptor = _job_file(job, field)
        if descriptor is None:
            return None, None, None
        root, relative, unique = descriptor
        name = relative[-1]
        kind = _kind(Path(name))
        if kind is None:
            return None, None, None
        local_root = self.output if root == 'output' else self.outputs
        nearby = [_present_file(local_root, relative)]
        if root == 'output' and len(relative) == 2:
            nearby.append(_present_file(self.legacy_local, (name,)))
        originals = []
        if self.archive is not None:
            originals.append(_cached_present(self.data, (root, *relative)))
            if root == 'output' and len(relative) == 2:
                originals.append(_cached_present(self.data, ('videos', name)))
        local = next((path for path in nearby if path), None)
        legacy = next((path for path in originals if path), None)
        # A publisher only exposes complete copies. UUID-based names identify the job
        # after Colab loses its copy. Numbered outputs require independent evidence.
        digest = None
        if not unique:
            key = '/'.join((root, *relative))
            digest = job.get('h3_delete_digests', {}).get(key)
            if not isinstance(digest, str) or not re.fullmatch(r'[0-9a-f]{64}', digest):
                candidates = [path for path in (*nearby, *originals) if path]
                created, finished = job.get('created', 0), job.get('finished')
                # Prefer the current configured output and be conservative about name reuse.
                if self.names[name] > 1 or not created or not candidates:
                    return None, None, None
                for path in candidates:
                    modified = path.stat().st_mtime
                    if modified + 5 < created or (finished and modified > finished + 5):
                        return None, None, None
                # Avoid hashing a legacy source until there are several candidates to compare.
                if len(candidates) > 1:
                    digest = _matching_digest(candidates[0])
                    if any(_matching_digest(path) != digest for path in candidates[1:]):
                        return None, None, None
                else:
                    digest = None
            else:
                if local and _matching_digest(local) != digest:
                    local = None
                if legacy and _matching_digest(legacy) != digest:
                    legacy = None
        if self.archive is None:
            return local, None, legacy
        base = self.archive / kind / name
        archived = _cached_present(self.archive, (kind, name))
        source = local or legacy
        if unique and not source:
            return local, archived, legacy
        if unique and source and archived and _signature(archived)[0] != _signature(source)[0]:
            archived = None
        if not digest and source and (archived or unique):
            digest = _matching_digest(source)
        if digest:
            if archived and _matching_digest(archived) == digest:
                return local, archived, legacy
            variant = _archive_variant(base, digest)
            archived = _cached_present(self.archive, (kind, variant.name))
            if archived and _matching_digest(archived) == digest:
                return local, archived, legacy
        return local, None, legacy

    def status(self, job):
        files = {}
        for field in OUTPUT_FIELDS:
            if job.get(field):
                local, archived, legacy = self.locate(job, field)
                files[field] = {'available': bool(local or archived or legacy), 'archived': bool(archived)}
        if not files:
            return None
        copies = sum(file['archived'] for file in files.values())
        if copies == len(files):
            status = 'archived'
        elif copies:
            status = 'partial'
        elif not any(file['available'] for file in files.values()):
            status = 'missing'
        elif self.archive is not None:
            status = 'pending'
        else:
            status = 'offline' if self.drive_expected else 'local'
        return {'status': status, 'files': files}


def send_media(handler, path):
    try:
        stream = path.open('rb') if path else None
    except FileNotFoundError:
        stream = None
    if stream is None:
        return handler.send_error(404, 'Media unavailable')
    with stream:
        size = os.fstat(stream.fileno()).st_size
        raw = handler.headers.get('Range')
        start, end = 0, size - 1
        if raw:
            match = re.fullmatch(r'bytes=([0-9]{0,20})-([0-9]{0,20})', raw.strip())
            if match and match[1]:
                start = int(match[1])
                end = min(end, int(match[2])) if match[2] else end
            elif match and match[2] and int(match[2]):
                start = max(0, size - int(match[2]))
            else:
                end = -1
            if start > end:
                handler.send_response(416)
                handler.send_header('Content-Range', f'bytes */{size}')
                handler.send_header('Content-Length', '0')
                return handler.end_headers()
        remaining = end - start + 1
        handler.send_response(206 if raw else 200)
        handler.send_header('Content-Type', mimetypes.guess_type(path.name)[0] or 'application/octet-stream')
        handler.send_header('Accept-Ranges', 'bytes')
        handler.send_header('Cache-Control', 'private, no-store')
        handler.send_header('Content-Length', str(remaining))
        if raw:
            handler.send_header('Content-Range', f'bytes {start}-{end}/{size}')
        handler.end_headers()
        if handler.command == 'HEAD':
            return
        stream.seek(start)
        try:
            while remaining:
                block = stream.read(min(remaining, 1024 * 1024))
                if not block:
                    break
                handler.wfile.write(block)
                remaining -= len(block)
        except (BrokenPipeError, ConnectionResetError):
            pass  # Browser changed tabs or requested a different video offset.


def delete_job_media(job, jobs, output, outputs, legacy_local, archive, data, drive_mount, save_pending=None):
    """Resolve all paths first, then delete only files owned by this job."""
    files = list(_job_files(job))
    if job.get('status') not in FINISHED or not files:
        raise ValueError('只有已结束且有成品的任务可以删除成品。')
    if archive is None and drive_mount is not None:
        raise ValueError('云盘未连接。请挂载云盘并重跑 ③ 格，再删除这项成品。')
    if archive is not None and (drive_mount is None or not drive_mount.is_dir() or not archive.is_dir()):
        raise ValueError('无法访问云盘归档目录，请先恢复挂载再重试。')

    local_to_delete, cloud_to_delete = set(), set()
    saved_digests = job.get('h3_delete_digests', {})
    digests = dict(saved_digests)
    for root_key, relative, unique in files:
        name = relative[-1]
        kind = _kind(Path(name))
        if not kind:
            raise ValueError('成品格式无法确认。')
        local = [_safe_file(output if root_key == 'output' else outputs, relative)]
        if root_key == 'output' and len(relative) == 2 and not legacy_local.is_symlink():
            local.append(_safe_file(legacy_local, (name,)))
        legacy = []
        archived = None
        if archive is not None:
            archived = _safe_file(archive, (kind, name))
            legacy.append(_safe_file(data, (root_key, *relative)))
            if root_key == 'output' and len(relative) == 2:
                legacy.append(_safe_file(data, ('videos', name)))
        present_local = [path for path in local if path.is_file()]
        present_legacy = [path for path in legacy if path.is_file()]
        if unique:
            local_to_delete.update(present_local)
            cloud_to_delete.update(present_legacy)
            if archived is not None:
                cloud_to_delete.add(archived)
                stem = Path(name).stem[:80]
                suffix = Path(name).suffix
                for candidate in archived.parent.glob(f'*{suffix}'):
                    if re.fullmatch(re.escape(stem) + r'--[0-9a-f]{64}' + re.escape(suffix), candidate.name):
                        cloud_to_delete.add(_safe_file(archive, (kind, candidate.name)))
            continue

        # Historical filenames were reused between Colab sessions. A recorded digest
        # also lets a request resume when the file removals succeeded but saving failed.
        key = '/'.join((root_key, *relative))
        digest = digests.get(key)
        if not digest:
            for other in jobs:
                if not isinstance(other, dict) or other.get('id') == job['id']:
                    continue
                for field in ('image', 'video'):
                    item = other.get(field)
                    if isinstance(item, dict) and item.get('filename') == name:
                        raise ValueError('多条旧任务使用相同文件名，请在云盘中确认后手动删除。')
            originals = present_local or present_legacy
            if not originals:
                if archived is not None and (archived.is_file() or any(
                        archived.parent.glob(f'{archived.stem[:80]}--*{archived.suffix}'))):
                    raise ValueError('旧任务的原文件已不在当前 Colab，无法确认云盘里的同名成品。请在云盘中确认后手动删除。')
                continue
            created, finished = job.get('created', 0), job.get('finished')
            times = [path.stat().st_mtime for path in originals]
            if any(mtime + 5 < created or (finished and mtime > finished + 5) for mtime in times):
                raise ValueError('旧任务的文件时间与记录不符，请确认成品后手动删除。')
            digest = _digest(originals[0])
            if any(_digest(path) != digest for path in originals[1:]):
                raise ValueError('旧任务有多个同名文件，无法确认要删除哪一个。')
            digests[key] = digest
        if not isinstance(digest, str) or not re.fullmatch(r'[0-9a-f]{64}', digest):
            raise ValueError('旧任务的成品校验信息无效。')
        local_to_delete.update(path for path in present_local if _digest(path) == digest)
        cloud_to_delete.update(path for path in present_legacy if _digest(path) == digest)
        if archived is not None:
            variant = _archive_variant(archived, digest)
            for candidate in (archived, _safe_file(archive, (kind, variant.name))):
                if candidate.is_file() and _digest(candidate) == digest:
                    cloud_to_delete.add(candidate)

    if save_pending is not None and digests != saved_digests:
        job['h3_delete_digests'] = digests
        save_pending()

    # Keep local originals available for retry if a Drive operation fails.
    for path in sorted(cloud_to_delete):
        path.unlink(missing_ok=True)
    for path in sorted(local_to_delete):
        path.unlink(missing_ok=True)
