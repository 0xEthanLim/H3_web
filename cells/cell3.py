#@title ③ 启动 ComfyUI ＋ H3 Toybox，拿一个手机也能开的网址
TUNNEL = globals().get("TUNNEL", True)
#@markdown 作品存云盘（第一次会跳授权，按同意）
SAVE_TO_DRIVE = globals().get("SAVE_TO_DRIVE", True)
#@markdown API 密钥也存云盘
SAVE_API_KEYS = globals().get("SAVE_API_KEYS", False)
import subprocess, time, urllib.request, json, sys, os, threading, hashlib, secrets
from pathlib import Path

# ③ 和 🔄 共用，构建时写入两个单元。
import json, re, subprocess, time, urllib.request
from pathlib import Path
from urllib.parse import urlencode

TUNNEL_STATE = Path('/content/.h3_tunnel_state.json')
TUNNEL_LOG = Path('/content/cloudflared.log')
TUNNEL_BINARY = Path('/content/cloudflared')
GATE_HEALTH = 'http://127.0.0.1:8191/__health'


def health_ok(url):
    try:
        with urllib.request.urlopen(url, timeout=3) as response:
            return response.status == 200 and response.read(16) == b'ok'
    except (OSError, ValueError):
        return False


def stop_process(proc):
    if proc is not None and proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)


def tunnel_link(url, token):
    return url + ('/?' + urlencode({'k': token}) if token else '/')


def keep_recent_tunnel(state, proc, force=False):
    if force or not isinstance(state, dict) or proc is None or proc.poll() is not None:
        return False
    url, started = state.get('url'), state.get('at')
    return (isinstance(url, str) and re.fullmatch(r'https://[a-z0-9-]+\.trycloudflare\.com', url)
            and isinstance(started, (float, int)) and 0 <= time.time() - started < 180
            and health_ok(url + '/__health'))


def start_tunnel(previous):
    if not health_ok(GATE_HEALTH):
        raise RuntimeError('口令门禁未就绪，请先运行 ③ 格。')
    binary = TUNNEL_BINARY
    if not binary.is_file() or not binary.stat().st_size:
        temporary = binary.with_suffix('.download')
        try:
            subprocess.run(['wget', '-q', '-O', str(temporary),
                            'https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64'],
                           check=True, timeout=90)
            temporary.chmod(0o755)
            temporary.replace(binary)
        finally:
            temporary.unlink(missing_ok=True)
    stop_process(previous)
    TUNNEL_STATE.unlink(missing_ok=True)
    with TUNNEL_LOG.open('w') as log:
        proc = subprocess.Popen([str(binary), 'tunnel', '--no-autoupdate',
                                 '--url', 'http://127.0.0.1:8191'],
                                stdout=log, stderr=subprocess.STDOUT)
    try:
        url = None
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                break
            if not url:
                match = re.search(r'https://[a-z0-9-]+\.trycloudflare\.com',
                                  TUNNEL_LOG.read_text(encoding='utf-8', errors='replace'))
                if match:
                    url = match.group(0)
            if url and health_ok(url + '/__health') and proc.poll() is None:
                TUNNEL_STATE.write_text(json.dumps({'url': url, 'at': time.time()}), encoding='utf-8')
                return proc, url
            time.sleep(1)
        raise RuntimeError('隧道还未通过健康检查：\n' +
                           TUNNEL_LOG.read_text(encoding='utf-8', errors='replace')[-1200:])
    except BaseException:
        stop_process(proc)
        raise

COMFY = Path('/content/ComfyUI')
APP = Path('/content/minimax-h3-colab/toybox')
STATE = Path('/content/.h3_runtime_state.json')

# ── 生图引擎：② 格选的，还是上次留在磁盘上的 ──
# 必须在这里解析，而不是在写 config.json 那里 —— 因为 MOSS 的装配块在
# 启动 Toybox 之前就要用到它。kernel 重启后 ② 格的变量会丢，③ 格单独点也拿不到，
# 所以退一步读 ② 格写下的选择文件；都没有就按 ph3（改动前的默认）。
try:
    IMAGE_ENGINE
except NameError:
    _engine_file = Path('/content/.h3_image_engine')
    try:
        IMAGE_ENGINE = _engine_file.read_text(encoding='utf-8').strip() or 'moss'
    except OSError:
        IMAGE_ENGINE = 'moss'
    print('ℹ️ 没读到 ① ② 格的选择，用的是：%s（先在 ① 格选引擎可改变）' % IMAGE_ENGINE)
if IMAGE_ENGINE not in ('ph3', 'moss', 'qwen', 'none'):
    print('⚠️ 未知引擎 %r，按 moss 处理' % IMAGE_ENGINE)
    IMAGE_ENGINE = 'moss'

# 决定「能不能沿用已经载入模型的 ComfyUI」的依据。
# 这三个节点倉的 commit 是 ① 格 clone 下来的 HEAD 对应版本；权重靠 ② 格盖的 .sha256 戳记认。
NODE_DIRS = ('minimax-h3-audio-t8', 'comfyui-SelfLift', 'ComfyUI-H3-Motion-Context')

# 默认影片鏈路真的会执行到的节点。少一个就代表环境坏了，不能沿用。
REQUIRED_NODES = (
    'UNETLoader', 'CLIPLoader', 'VAELoader', 'LoraLoaderModelOnly',
    'MiniMaxH3ReferenceToVideo', 'MiniMaxH3SigmaShift',
    'SelfLiftH3Sampler', 'MiniMaxH3SemanticBridgeApplyT8', 'MiniMaxH3SemanticBridgeConfigT8',
    'ModelAttentionBackend', 'BlockSparseAttention',
    'KSamplerSelect', 'BasicScheduler', 'BasicGuider', 'RandomNoise', 'SamplerCustomAdvanced',
    'ConditioningZeroOut', 'PrimitiveFloat', 'ComfyMathExpression',
    'VAEDecode', 'VAEDecodeAudio', 'CreateVideo', 'SaveVideo',
)


def wait_http(url, name, log, proc, tries=180):
    for _ in range(tries):
        if proc.poll() is not None:
            break
        try:
            with urllib.request.urlopen(url, timeout=2):
                pass
            if proc.poll() is not None:
                break
            print(name, '已启动')
            return
        except Exception:
            time.sleep(2)
    try:
        print(open(log, encoding='utf-8', errors='ignore').read()[-3000:])
    except Exception:
        pass
    raise RuntimeError(name + ' 没有启动成功，看上面的纪录')


def _try(fn, default=None):
    try:
        return fn()
    except Exception:
        return default


def git_head(path):
    return _try(lambda: subprocess.run(['git', '-C', str(path), 'rev-parse', 'HEAD'],
                                       capture_output=True, text=True, timeout=20).stdout.strip(), '')


def fingerprint(output_root):
    """ComfyUI commit ＋ 三个节点 commit ＋ 权重戳记 ＋ 输出目录。
    全部一致才代表「现在跑著的 ComfyUI 跟这一輪该跑的是同一个东西」。"""
    models = COMFY / 'models'
    parts = {
        'comfy': git_head(COMFY),
        'nodes': {d: git_head(COMFY / 'custom_nodes' / d) for d in NODE_DIRS},
        'weights': sorted((str(p.relative_to(models)), _try(lambda p=p: p.read_text().strip(), ''))
                          for p in models.rglob('*.sha256')),
        'output': str(output_root),
    }
    return hashlib.sha256(json.dumps(parts, sort_keys=True).encode()).hexdigest()[:16]


def comfy_probe(port=8188):
    """回传 (可用, 说明)。只发两个很轻的请求，不载入任何东西。"""
    try:
        stats = json.loads(urllib.request.urlopen(
            'http://127.0.0.1:%d/system_stats' % port, timeout=4).read())
    except Exception:
        return False, '连不上'
    if not isinstance(stats, dict) or not stats:
        return False, 'system_stats 内容不对'
    try:
        info = json.loads(urllib.request.urlopen(
            'http://127.0.0.1:%d/object_info' % port, timeout=30).read())
    except Exception as e:
        return False, 'object_info 读不到（%s）' % type(e).__name__
    missing = [n for n in REQUIRED_NODES if n not in info]
    if missing:
        return False, '缺节点：%s' % ', '.join(missing[:6])
    return True, '%d 个节点、权重已载入' % len(info)


# 权重清单由 ① 格定义
missing_weights = []
# WEIGHT_SIZES 是 (引擎分组, 相对路径, 字节) 三元组，来自 ① 格。
# 分组名和引擎名不一致：P-H3 那组在清单里叫 'image'，引擎名是 'ph3' ——
# 这里显式映射，否则 ph3 的权重缺失会被静默放过（查 'ph3' 永远匹配不到 'image'）。
if 'WEIGHT_SIZES' not in globals():
    raise RuntimeError('③ 格要用 ① 格的权重清单来判断缺哪个文件：请先运行 ① 格。')
_OPT_FOR_ENGINE = {'ph3': 'image', 'moss': 'moss', 'qwen': 'qwen', 'none': None}
_mine = _OPT_FOR_ENGINE.get(IMAGE_ENGINE, 'image')
for _opt, name, size in WEIGHT_SIZES:
    if _opt not in ('core', 'rewriter', _mine):
        continue
    path = COMFY / 'models' / name
    if not path.is_file() or path.stat().st_size != size:
        missing_weights.append(name)
if missing_weights:
    raise RuntimeError('请先跑完 ② 格（引擎选 %s），以下权重缺失或大小不符：%s'
                       % (IMAGE_ENGINE, ', '.join(missing_weights[:4])))

# 归档线程停下来后再重启服务，保持单一归档写入者。
if globals().get('archive_stop') is not None:
    archive_stop.set()
    archive_worker.join(timeout=15)
    if archive_worker.is_alive():
        raise RuntimeError('云盘归档仍在完成上一次复制，请稍后重跑 ③ 格。')
for _n in ('tun', 'proxy', 'toybox'):
    stop_process(globals().get(_n))
TUNNEL_STATE.unlink(missing_ok=True)

# ── 数据放哪：一律转成 Path，后面才不会出现 str.rglob 的错 ──
# 两个根目录，各管各的：
#   DATA      云盘：素材库、jobs.json、config.json —— 换执行阶段也不能丢
#   RUN       本机：ComfyUI 输出、Toybox 的 outputs 臨时档 —— 本来就是一次性
# 原本 output 和 outputs 都挂在云盘上，等於同一支片在云盘存两份；
# 搬到本机后云盘只剩 H3_Productions 一处成品。
DATA = Path('/content/h3toybox_data')
if SAVE_TO_DRIVE:
    # 三种情况都要能走：
    #   1. 网页端：drive.mount() 存在，正常授权
    #   2. CLI 端（colab exec）：google.colab 不存在，但 `colab drivemount` 可能已经把
    #      /content/drive 挂好了 —— 这时直接用它，不要因为 import 失败就放弃云盘
    #   3. 都没挂：退回本机，明确告诉用户数据不会留存
    _mounted = Path('/content/drive/MyDrive')
    if _mounted.is_dir():
        DATA = _mounted / 'H3Toybox'
        print('☁️ 云端硬盘已挂载，数据放：', DATA)
    else:
        try:
            from google.colab import drive
            drive.mount('/content/drive')
            if not _mounted.is_dir():
                raise OSError('挂载返回后找不到 MyDrive')
            DATA = _mounted / 'H3Toybox'
        except Exception as e:
            print('⚠️ 没挂上云端硬盘（', e, '）：这次作品只存在暂存空间')
            print('   CLI 下请先跑 `colab drivemount -s <session>` 再执行本格')
DATA = Path(str(DATA))
DATA.mkdir(parents=True, exist_ok=True)

# 只有 ComfyUI 的输出目录搬去本机。素材库、jobs.json、config.json 仍留在 DATA（云盘），
# 不能搬 —— 换执行阶段它们必须还在。
RUN = Path('/content/h3toybox_run')      # 本机，永不进云盘
RUN.mkdir(parents=True, exist_ok=True)
output_root = Path(str(RUN / 'output'))  # ComfyUI --output-directory
# Toybox 自己的拼接成品（_part/_src/full_video/_lib_*）也必须落在本机。
# 它的 OUTPUTS 默认是 DATA/outputs，也就是云盘 —— 不挪走的话云盘会同时存在
# MyDrive/H3Toybox/outputs/ 和 MyDrive/H3_Productions/videos/ 两份同样的影片。
# 这里用 H3TOYBOX_OUTPUTS 覆盖（server.py 由 ③ 格的小补丁改成可覆盖）。
run_outputs = Path(str(RUN / 'outputs'))
run_outputs.mkdir(parents=True, exist_ok=True)
videos = Path(str(output_root / 'H3Toybox'))
videos.mkdir(parents=True, exist_ok=True)

# ── ComfyUI：只当引擎，只听 127.0.0.1 ──
# 重跑这格时，如果现在这台 ComfyUI 已经载好模型、环境又没变，就别关它 ——
# 一关就得把 30 GiB 权重从 VRAM 赶出去再从磁盘重读，那才是每次重跑最贵的 2–4 分钟。
fp_want = fingerprint(output_root)
fp_now = _try(lambda: json.loads(STATE.read_text()) if STATE.is_file() else None, None)
keep = False

if not isinstance(fp_now, dict):
    print('没有沿用记录：这次重新启动 ComfyUI')
elif fp_now.get('fingerprint') != fp_want:
    print('环境变了（权重戳记／节点 commit／输出目录）：重启 ComfyUI')
else:
    # 指纹一致：不管那只 ComfyUI 是不是这个 kernel 启的，探针说合格就直接用。
    # 指纹已经锁住 comfy commit＋三个节点 commit＋每个权重的 sha，能对上就是同一套环境。
    ok, why = comfy_probe()
    if ok:
        keep = True
        print('♻️ 沿用已在跑的 ComfyUI（%s），模型不用重新载入' % why)
    else:
        print('已在跑的 ComfyUI 不合格（%s）：重启' % why)

if not keep:
    _p = globals().get('server')
    if _p is not None and hasattr(_p, 'poll') and _p.poll() is None:
        _try(lambda: (_p.terminate(), _p.wait(timeout=15)))
    else:
        # kernel 重连过、process 不在我们手上：用精确的 cmdline 认出那只 ComfyUI
        _try(lambda: subprocess.run(
            ['pkill', '-f', 'main.py --listen 127.0.0.1 --port 8188'],
            capture_output=True, timeout=15))
    for _ in range(40):  # 等埠真的放开，不然新 process 会 bind 失敗
        try:
            urllib.request.urlopen('http://127.0.0.1:8188/system_stats', timeout=1)
            time.sleep(0.5)
        except Exception:
            break
    comfy_started = time.time()
    server = subprocess.Popen(
        [sys.executable, 'main.py', '--listen', '127.0.0.1', '--port', '8188',
         '--output-directory', str(output_root)],
        cwd=str(COMFY),
        stdout=open('/content/comfyui.log', 'w'), stderr=subprocess.STDOUT)
    wait_http('http://127.0.0.1:8188/system_stats', 'ComfyUI', '/content/comfyui.log', server)
    ok, why = comfy_probe()
    if not ok:
        raise RuntimeError('ComfyUI 起来了但节点不全（%s）。① 格可能要重跑：节点没装好。' % why)

# pid 只在我们自己启的时候更新；沿用的情況保留原本那个，純记录用
pids = _try(lambda: server.pid, None) if not keep else (fp_now or {}).get('pid')
if keep:
    comfy_started = (fp_now or {}).get('comfy_started', 0)
STATE.write_text(json.dumps({'fingerprint': fp_want, 'pid': pids,
                             'comfy_started': comfy_started, 'saved': time.time()}))
print('🎬 影片写到：', videos)

# ── Toybox 设置：只覆写这台机器的路径 ──
(COMFY / 'input').mkdir(exist_ok=True)

# ═════ 界面补丁包：运行时从仓库拉，不再塞进笔记本 ═════
# 要改这里的东西，改仓库里的文件即可，笔记本不用重发。
PATCH_URL = 'https://codeload.github.com/0xEthanLim/H3_web/zip/refs/heads/main'
PATCH_DIR = Path('/content/h3patch')
PATCH_FILES = PATCH_DIR / 'files'
# 补丁包每次 ③ 都重拉：判断「文件在不在」会让改过的 app.js / index.html
# 永远停在第一次下载的版本，前端改了什么用户都看不到。
# 拉了失败就退回盘上已有的那份，不至于把已经能跑的会话搞死。
PATCH_STAMP = PATCH_DIR / '.sha'
_NEEED = ('authproxy.py', 'style.css', 'index.html', 'app.js', 'zh_cn_backend.json',
          'archive_media.py', 'media_edits.json', 'rewrite_guard.py',
          'moss_engine_patch.py', 'moss_graph.py')


def _pull_patch():
    """拉一次补丁包，成功返回 True。失败时盘上的旧文件仍然可用。"""
    import shutil as _shutil
    import urllib.request as _urlreq
    import zipfile as _zipfile
    _zip_path = Path('/content/h3patch.zip')
    print('⬇️ 下载界面补丁包…', flush=True)
    try:
        _urlreq.urlretrieve(PATCH_URL, _zip_path)
        PATCH_DIR.mkdir(parents=True, exist_ok=True)
        with _zipfile.ZipFile(_zip_path) as _zip:
            _zip.extractall(PATCH_DIR)
    finally:
        _zip_path.unlink(missing_ok=True)
    _tops = [p for p in PATCH_DIR.iterdir() if p.is_dir() and p.name not in ('files', 'cells')]
    if not _tops or not (_tops[0] / 'files').is_dir():
        raise RuntimeError('补丁包结构不对：压缩包里应只有一层目录，且含 files/。')
    PATCH_FILES.mkdir(parents=True, exist_ok=True)
    for _item in (_tops[0] / 'files').iterdir():
        _shutil.move(str(_item), str(PATCH_FILES / _item.name))
    _shutil.rmtree(_tops[0], ignore_errors=True)
    _want = set(_NEEED)
    _got = {p.name for p in PATCH_FILES.iterdir() if p.is_file()}
    if _want - _got:
        raise RuntimeError('补丁包缺文件：%s' % '、'.join(sorted(_want - _got)))
    print('✅ 界面补丁包就绪：%d 个文件' % len(_got))
    return True


try:
    _pull_patch()
except Exception as _err:
    if not (PATCH_FILES / 'authproxy.py').is_file():
        raise
    print('⚠️ 补丁包没拉到（%s），沿用盘上 %d 个旧文件' % (_err, len(list(PATCH_FILES.iterdir()))))
if not (PATCH_FILES / 'authproxy.py').is_file():
    raise RuntimeError('界面补丁包不完整，请检查 PATCH_URL 是否正确。')


# ═════ 手机适配 CSS ═════
(APP / 'static' / 'style.css').write_text(
    (PATCH_FILES / 'style.css').read_text(encoding='utf-8'), encoding='utf-8')
print('🎨 已套用手机适配 CSS')
# ═════ 改写器防拒补丁 ═════
_h3g_ns = {'__name__': 'h3_rewrite_guard'}
exec(compile((PATCH_FILES / 'rewrite_guard.py').read_text(encoding='utf-8'),
             'h3_rewrite_guard.py', 'exec'), _h3g_ns)
_server_file = APP / 'server.py'
if not _h3g_ns['main'](str(_server_file)) or 'H3TOYBOX_OUTPUTS' not in _server_file.read_text(encoding='utf-8'):
    raise RuntimeError('Toybox 后端与存储补丁不匹配，无法确认作品的输出目录。')
# ═════ 简体中文界面 ═════
_zh_cn = {
    'static': {_n: (PATCH_FILES / _n).read_text(encoding='utf-8')
               for _n in ('index.html', 'app.js')},
    'backend_messages': json.loads(
        (PATCH_FILES / 'zh_cn_backend.json').read_text(encoding='utf-8')),
}
for _name, _text in _zh_cn['static'].items():
    (APP / 'static' / _name).write_text(_text, encoding='utf-8')
_message_file = APP / 'server.py'
_original = _message_file.read_text(encoding='utf-8')
_messages = _zh_cn['backend_messages']
_localized = '\n'.join(_messages.get(line, line) for line in _original.split('\n'))
if _localized != _original:
    compile(_localized, str(_message_file), 'exec')
    _message_file.write_text(_localized, encoding='utf-8')
print('🌐 已加载简体中文界面')
# ═════ 成品管理 ═════
_media_payload = json.loads(
    (PATCH_FILES / 'media_edits.json').read_text(encoding='utf-8'))
_media_payload['module'] = (PATCH_FILES / 'archive_media.py').read_text(encoding='utf-8')
_media_module = {'__name__': 'h3_media_notebook'}
exec(compile(_media_payload['module'], 'h3_media.py', 'exec'), _media_module)
MediaArchive = _media_module['MediaArchive']

_server = APP / 'server.py'
_server_text = _server.read_text(encoding='utf-8')
_needs_media_patch = '# h3-media-delete-v3' not in _server_text
_needs_faststart = '# h3-faststart-v1' not in _server_text
if _needs_media_patch:
    _edits = _media_payload['preview_edits']
    if '# h3-media-delete-v2' not in _server_text:
        _edits = _media_payload['upgrade_edits'] + _edits
        if '# h3-media-delete-v1' not in _server_text:
            _edits = _media_payload['edits'] + _edits
    for _before, _after in _edits:
        if _server_text.count(_before) != 1:
            raise RuntimeError('Toybox 后端版本与成品管理补丁不匹配。')
        _server_text = _server_text.replace(_before, _after, 1)
if _needs_faststart:
    for _before, _after in _media_payload.get('faststart_edits', []):
        if _server_text.count(_before) != 1:
            raise RuntimeError('Toybox 后端版本与 faststart 补丁不匹配。')
        _server_text = _server_text.replace(_before, _after, 1)
compile(_server_text, str(_server), 'exec')

(APP / 'h3_media.py').write_text(_media_payload['module'], encoding='utf-8')
if _needs_media_patch or _needs_faststart:
    _media_temp = _server.with_suffix('.media.tmp')
    _media_temp.write_text(_server_text, encoding='utf-8')
    _media_temp.replace(_server)
# ═════ OpenAI 兼容端点适配（纯文本发字符串 + 标准 UA，避免中转站 502）═════
_oa_srv = APP / 'server.py'
_oa_txt = _oa_srv.read_text(encoding='utf-8')
if '# h3-oa-compat-v1' not in _oa_txt:
    _oa_old_c = (
        '    content = [{"type": "text", "text": p} if kind == "text" else\n'
        '               {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + jpeg_b64(p)}} for kind, p in parts]'
    )
    _oa_new_c = (
        '    # h3-oa-compat-v1\n'
        '    content = ("\\n".join(p for _, p in parts) if all(kind == "text" for kind, _ in parts) else\n'
        '               [{"type": "text", "text": p} if kind == "text" else\n'
        '                {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + jpeg_b64(p)}} for kind, p in parts])'
    )
    _oa_old_h = '    headers = {"Authorization": "Bearer " + oa["api_key"]} if oa.get("api_key") else {}'
    _oa_new_h = '    headers = {"User-Agent": "Mozilla/5.0", **({"Authorization": "Bearer " + oa["api_key"]} if oa.get("api_key") else {})}'
    if _oa_old_c in _oa_txt and _oa_old_h in _oa_txt:
        _oa_txt2 = _oa_txt.replace(_oa_old_c, _oa_new_c, 1).replace(_oa_old_h, _oa_new_h, 1)
        compile(_oa_txt2, str(_oa_srv), 'exec')
        _oa_tmp = _oa_srv.with_suffix('.oa.tmp')
        _oa_tmp.write_text(_oa_txt2, encoding='utf-8')
        _oa_tmp.replace(_oa_srv)
        print('🔌 OpenAI 兼容端点补丁已应用（纯文本直发 + 标准 UA）')

# ═════ 改写输出语言：图片纯中文、视频英文小标题 + 中文正文（幂等）═════
_zh_rw_txt = _oa_srv.read_text(encoding='utf-8')
if '# h3-zh-rewrite-v1' not in _zh_rw_txt:
    _zh_img_old = '    system = spec + "\\n\\nReturn only the final rewritten prompt, nothing else."'
    _zh_img_new = (
        '    # h3-zh-rewrite-v1\n'
        '    system = spec + ("\\n\\nLanguage requirement: Write the final rewritten prompt in natural, vivid Simplified Chinese "'
        '"(keep reference tags like <image1> or <Picture 1> unchanged if present). Return only the final rewritten prompt, nothing else.")'
    )
    _zh_vid_old = '    return system.strip(), parts'
    _zh_vid_new = (
        '    system = system.strip() + ("\\n\\n### Output Language\\n"'
        '"Keep all English section headers (subject_definitions, summary, retention_analysis, detailed_description, "'
        '"overall_soundscape, non_diegetic_music, integrated_multimodal_description) and tags (<Subject N>, <Picture N>, [Shot N]) in English, "'
        '"and write all body descriptions in natural, precise Simplified Chinese. "'
        '"Keep overall_soundscape clean and natural; unless the user explicitly asks for music or crowd noise, set non_diegetic_music to N/A.")\n'
        '    return system, parts'
    )
    if _zh_img_old in _zh_rw_txt and _zh_vid_old in _zh_rw_txt:
        _zh_rw_txt2 = _zh_rw_txt.replace(_zh_img_old, _zh_img_new, 1).replace(_zh_vid_old, _zh_vid_new, 1)
        compile(_zh_rw_txt2, str(_oa_srv), 'exec')
        _zh_tmp = _oa_srv.with_suffix('.zhrw.tmp')
        _zh_tmp.write_text(_zh_rw_txt2, encoding='utf-8')
        _zh_tmp.replace(_oa_srv)
        print('🈶 改写语言补丁已应用（图片纯中文 / 视频英文小标题+中文正文）')

# ═════ 短片 Agent 复用设置里已配好的 OpenAI 中转 API（幂等）═════
_ag_txt = _oa_srv.read_text(encoding='utf-8')
if '# h3-agent-oa-v1' not in _ag_txt:
    _ag_old = (
        '    return rewrite_openai({"base_url": ag["base_url"], "api_key": "", "model": ag.get("model")}, knobs, system, parts,\n'
        '                          max_tokens=4096)'
    )
    _ag_new = (
        '    # h3-agent-oa-v1: 优先复用设置里配置好的 OpenAI 兼容中转端点\n'
        '    _rw_oa = (cfg.get("rewriter") or {}).get("openai") or {}\n'
        '    _oa_conf = (_rw_oa if _rw_oa.get("base_url") else {"base_url": ag["base_url"], "api_key": "", "model": ag.get("model")})\n'
        '    return rewrite_openai(_oa_conf, knobs, system, parts, max_tokens=4096)'
    )
    if _ag_old in _ag_txt:
        _ag_txt2 = _ag_txt.replace(_ag_old, _ag_new, 1)
        compile(_ag_txt2, str(_oa_srv), 'exec')
        _ag_tmp = _oa_srv.with_suffix('.ag.tmp')
        _ag_tmp.write_text(_ag_txt2, encoding='utf-8')
        _ag_tmp.replace(_oa_srv)
        print('🤖 短片 Agent 已对接设置里的 OpenAI 中转 API')

# ═════ H3 视频工作流音画质调优（幂等）═════
_wf_path = APP / 'WF.json'
if _wf_path.is_file():
    try:
        _wf = json.loads(_wf_path.read_text(encoding='utf-8'))
        _wf_changed = False
        if isinstance(_wf.get('404', {}).get('inputs'), dict) and _wf['404']['inputs'].get('strength_model') != 0.65:
            _wf['404']['inputs']['strength_model'] = 0.65
            _wf_changed = True
        if isinstance(_wf.get('397', {}).get('inputs'), dict) and _wf['397']['inputs'].get('lowres_scale') != 0.75:
            _wf['397']['inputs']['lowres_scale'] = 0.75
            _wf_changed = True
        if _wf_changed:
            _wf_tmp = _wf_path.with_suffix('.json.tmp')
            _wf_tmp.write_text(json.dumps(_wf, ensure_ascii=False, indent=2), encoding='utf-8')
            _wf_tmp.replace(_wf_path)
            print('🎞 H3 工作流已调优：turbo=0.65 / lowres_scale=0.75')
        else:
            print('🎞 H3 工作流参数已是调优值（turbo=0.65 / lowres_scale=0.75）')
    except Exception as _wf_err:
        print('⚠️ WF.json 调优跳过：', _wf_err)

# ═════ MOSS 生图引擎 ═════
# 只要没关生图且补丁包带了 moss_graph.py 就装配：这样即便默认选 qwen/ph3，网页下拉仍能热切回 moss。
if globals().get('IMAGE_ENGINE') != 'none' and (PATCH_FILES / 'moss_graph.py').is_file():
    _server_file = APP / 'server.py'
    _graph_file = APP / 'moss_graph.py'
    _graph_text = (PATCH_FILES / 'moss_graph.py').read_text(encoding='utf-8')
    if _graph_file.is_file() and _graph_file.read_text(encoding='utf-8') == _graph_text:
        print('🖼 moss_graph.py 已是最新')
    else:
        _graph_file.write_text(_graph_text, encoding='utf-8')
        print('🖼 moss_graph.py 已更新（%d 字节）' % len(_graph_text))
    if 'moss-engine:v1/helpers' in _server_file.read_text(encoding='utf-8'):
        print('🖼 MOSS 引擎补丁已在')
    else:
        _h3m_ns = {'__name__': 'moss_engine_patch'}
        exec(compile((PATCH_FILES / 'moss_engine_patch.py').read_text(encoding='utf-8'),
                     'moss_engine_patch.py', 'exec'), _h3m_ns)
        if not _h3m_ns['main'](str(_server_file), str(APP)):
            raise RuntimeError('MOSS 引擎补丁没打上：Toybox 后端与所选生图引擎不匹配。')
        print('🖼 已接入 MOSS-Image 引擎（HIRES 双段）')
    _missing = [n for n in ('isave',) if "g['%s']" % n not in _graph_text]
    if _missing:
        raise RuntimeError('MOSS 图缺少 server 需要的成品节点：%s' % ', '.join(_missing))
else:
    print('⏭️ 生图引擎是 %s，不装配 MOSS' % globals().get('IMAGE_ENGINE'))
cfg_path = Path(str(DATA / 'config.json'))
cfg = json.loads(cfg_path.read_text(encoding='utf-8')) if cfg_path.exists() else {}
if not isinstance(cfg, dict):
    raise ValueError('config.json 需要是 JSON 对象，请先检查设置文件。')
cfg.update({
    'comfy_url': 'http://127.0.0.1:8188',
    'comfy_input_dir': str(COMFY / 'input'),
    'skills_dir': '/content/t8-enhancer/directional_skills',
    # 改写完全关掉卸载：ComfyUI 的 /free 走 unload_all_models()，会把 30 GiB
    # H3 权重连 VAE 一起踢出 VRAM。下一次生成要重新从磁盘读回来 —— 这就是
    # 「③ 格跑出结果的时间变长」的根因：每支片开头白等一次冷加载（实测 65~73 秒）。
    # 高内存规格的显存本来够 9B 改写器和 H3 主链路并存，不需要为省显存付这个代价。
    'free_after_rewrite': False,
})
cfg.pop('guides', None)
# 生图引擎：② 格选了哪一组，就把哪一组当默认；另一组的权重在不在盘上单独探测。
# 网页设置里能切引擎，所以这里不能只报「选中的那个」—— 两套都要算，
# 切到没有权重的那一档时必须说清「没下」，而不是让 ComfyUI 报节点缺失。
PH3 = ('loras/p-h3-image_lora_bf16.safetensors',
       'text_encoders/p-h3-image_text_encoder_int8_convrot.safetensors',
       'vae/p-h3-image_vae_int8_convrot.safetensors')
MOSS_FILES = ('diffusion_models/Dirty-Moss.safetensors',
              'text_encoders/qwen3.5_9b_int8_convrot.safetensors',
              'vae/flux2-vae.safetensors')
QWEN_UNET = ('NoctQ_V4_int8_convrot.safetensors'
             if (COMFY / 'models' / 'diffusion_models' / 'NoctQ_V4_int8_convrot.safetensors').is_file()
             else 'qwen_image_2.1_int8_convrot.safetensors')
QWEN_FILES = ('diffusion_models/' + QWEN_UNET,
              'text_encoders/qwen3vl_8b_int8_convrot.safetensors',
              'vae/qwen_image_2.1_vae_bf16.safetensors')
_ready = {name: all((COMFY / 'models' / f).is_file() for f in group)
          for name, group in (('moss', MOSS_FILES), ('qwen', QWEN_FILES), ('ph3', PH3))}
if IMAGE_ENGINE not in ('moss', 'qwen', 'ph3', 'none'):
    IMAGE_ENGINE = 'moss'
cfg['agent'] = dict(cfg.get('agent') or {}, provider=(cfg.get('agent') or {}).get('provider', 'gemini'))
cfg['image'] = dict(cfg.get('image') or {}, engine=IMAGE_ENGINE, unet=QWEN_UNET,
                    installed=_ready.get(IMAGE_ENGINE, False), ready=_ready)
if _ready.get('moss'):
    cfg['image']['moss'] = dict(cfg['image'].get('moss') or {}, lora=(
        'moss_pussy_lora_v2.0.safetensors'
        if (COMFY / 'models' / 'loras' / 'moss_pussy_lora_v2.0.safetensors').is_file() else ''))
print('🖼 生图引擎可切换：' + '　'.join(
    '%s %s' % (name, '✅' if ok else '❌') for name, ok in _ready.items()))
print('   默认：%s %s' % (IMAGE_ENGINE, '（② 格 selection）' if IMAGE_ENGINE != 'none' else ''))
if IMAGE_ENGINE != 'none' and not _ready.get(IMAGE_ENGINE):
    print('   ⚠️ 默认这一档的权重不全，请在 ② 格补下，或换到标 ✅ 的引擎。')
_cfg_tmp = cfg_path.with_suffix('.json.tmp')
_cfg_tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=1), encoding='utf-8')
_cfg_tmp.replace(cfg_path)

_my_drive = Path('/content/drive/MyDrive')
ARCHIVE = _my_drive / 'H3_Productions' if SAVE_TO_DRIVE and DATA.parent == _my_drive else None
MEDIA_LOCK = RUN / '.h3_media.lock'
secrets = Path(str(DATA / 'secrets.json')) if SAVE_API_KEYS else Path('/content/h3toybox_secrets.json')

toybox = subprocess.Popen(
    [sys.executable, 'server.py'], cwd=str(APP),
    env=dict(os.environ, H3TOYBOX_PORT='8190', H3TOYBOX_DATA=str(DATA),
             H3TOYBOX_OUTPUTS=str(run_outputs),
             H3TOYBOX_COMFY_OUTPUT=str(output_root),
             H3TOYBOX_COMFY_STARTED=str(comfy_started),
             H3TOYBOX_LEGACY_OUTPUT=str(COMFY / 'output' / 'H3Toybox'),
             H3TOYBOX_MEDIA_LOCK=str(MEDIA_LOCK),
             H3TOYBOX_DRIVE_MOUNT=str(_my_drive) if SAVE_TO_DRIVE else '',
             H3TOYBOX_ARCHIVE=str(ARCHIVE) if ARCHIVE is not None else '',
             H3TOYBOX_SECRETS=str(secrets)),
    stdout=open('/content/toybox.log', 'w'), stderr=subprocess.STDOUT)
wait_http('http://127.0.0.1:8190/api/config', 'H3 Toybox', '/content/toybox.log', toybox, tries=60)
print('📁 素材库／设置：', DATA)
print('📁 拼接成品：', run_outputs)
# ═════ 口令门禁代理 ═════
# trycloudflare 网址本身没有访问控制：谁拿到谁就能用你的 GPU、翻你的素材库。
PROXY = Path('/content/authproxy.py')
PROXY.write_text((PATCH_FILES / 'authproxy.py').read_text(encoding='utf-8'),
                 encoding='utf-8')

import secrets as _token_secrets
_alpha = 'abcdefghijkmnpqrstuvwxyz23456789'   # 去掉易混的 l/o/0/1
try:
    from google.colab import userdata as _ud
    TOYBOX_TOKEN = (_ud.get('TOYBOX_TOKEN') or '').strip()
except Exception:
    TOYBOX_TOKEN = ''
TOYBOX_TOKEN = TOYBOX_TOKEN or os.environ.get('TOYBOX_TOKEN', '').strip()
if not TOYBOX_TOKEN:
    TOYBOX_TOKEN = ''.join(_token_secrets.choice(_alpha) for _ in range(16))

with open('/content/authproxy.log', 'w') as _log:
    proxy = subprocess.Popen(
        [sys.executable, str(PROXY), '--listen', '127.0.0.1:8191',
         '--upstream', '127.0.0.1:8190'],
        env=dict(os.environ, H3_GATE_TOKEN=TOYBOX_TOKEN),
        stdout=_log, stderr=subprocess.STDOUT)
wait_http('http://127.0.0.1:8191/__health', '口令门禁', '/content/authproxy.log', proxy, tries=20)
print('🔐 口令门禁已挂上（127.0.0.1:8191 → 8190）')
print('   口令：%s' % TOYBOX_TOKEN)

# ══════════ 成品归档 ══════════
if ARCHIVE is not None:
    try:
        archiver = MediaArchive(ARCHIVE, lock_path=MEDIA_LOCK)

        def sync_archive(service, roots, legacy_roots, stop):
            # 两组目录独立缓存，只有旧版扫描失败时才重复扫描云盘目录。
            legacy = None
            legacy_pending = True
            while not stop.is_set():
                try:
                    v, i = service.scan(roots, stop=stop)
                    if v or i:
                        print('📦 新归档：视频 %d / 图片 %d' % (v, i), flush=True)
                except OSError as error:
                    print('⚠️ 扫描归档失败，下轮重试：', error, flush=True)
                if legacy_pending and not stop.is_set():
                    try:
                        if legacy is None:
                            legacy = MediaArchive(service.root, lock_path=service.lock_path)
                        v, i = legacy.scan(legacy_roots, require_stable=False, stop=stop)
                        legacy_pending = legacy.last_scan_failed
                        if v or i:
                            print('📦 旧版成品归档：视频 %d / 图片 %d，旧目录保留' % (v, i), flush=True)
                    except OSError as error:
                        print('⚠️ 旧版归档未完成，下轮重试：', error, flush=True)
                stop.wait(30)

        archive_stop = threading.Event()
        archive_worker = threading.Thread(
            target=sync_archive,
            args=(archiver, (output_root, run_outputs, COMFY / 'output' / 'H3Toybox'),
                  (DATA / 'outputs', DATA / 'output', DATA / 'videos'), archive_stop),
            daemon=True)
        archive_worker.start()
        print('📦 归档目录：', ARCHIVE, '（每 30 秒检查，连续两次状态相同才保存）')
    except OSError as error:
        print('⚠️ 云盘归档初始化失败，成品仍在本机：', error)
else:
    print('⚠️ 未挂载云盘，本次成品不会归档')

# ══════════ 对外网址 ══════════
if TUNNEL:
    tun, url = start_tunnel(None)
    print('📱 手机／电脑都能开的网址：', tunnel_link(url, TOYBOX_TOKEN))
else:
    from google.colab import output as _co
    _co.serve_kernel_port_as_window(8191, anchor_text='🧸 打开 H3 Toybox')
    print('⚠️ 此连结仅供同一 Google 账号使用，打开后输入上面的口令。')