#@title ② 校验＋下载模型
#@markdown 生图引擎**默认 moss**（MOSS-Image 高画质双段）。影片主链路和改写器永远都下。
IMAGE_ENGINE = globals().get("IMAGE_ENGINE", "moss")
#@markdown `moss` = MOSS-Image 高画质双段（19.35 GiB，**要 `CIVITAI_TOKEN`**，无审查、可 NSFW、bf16 满血底模）<br>`qwen` = Qwen-Image 2.1 Noct-Q V4（16.10 GiB，免 token，无审查 NSFW，原生多图编辑/图生图）<br>`ph3` = P-H3（在 H3 模型上加 LoRA，9.52 GiB，免 token）<br>`none` = 不生图，省 9.52–19.35 GiB
KEEP_BOTH = globals().get("KEEP_BOTH", True)
#@markdown 勾上 `KEEP_BOTH`：把 `moss`（文生图）和 `qwen`（Noct-Q V4 多图编辑/图生图）两套权重同时留在盘上，网页设置里随时切换，不用重跑 ②。代价是同时占 35.45 GiB。
VERIFY_HASH = globals().get("VERIFY_HASH", False)
import os
os.environ['HF_HUB_DISABLE_XET'] = '0'
os.environ['HF_HUB_DOWNLOAD_TIMEOUT'] = '60'
os.environ.setdefault('HF_XET_CHUNK_CACHE_SIZE_BYTES', '0')
os.environ.setdefault('HF_XET_RECONSTRUCTION_USE_VECTORED_WRITE', '1')

from huggingface_hub import hf_hub_download
from pathlib import Path
import hashlib, re, shutil, time, struct, threading, urllib.parse, urllib.request
_BAR = None
_bar_lock = threading.Lock()
try:
    from tqdm.auto import tqdm      # 跟 huggingface_hub 用同一个，进度条长相一致
except ImportError:                 # huggingface_hub 一定会带上 tqdm；这里只是兜底
    def tqdm(*a, **k):
        class _Noop:
            def __enter__(self): return self
            def __exit__(self, *x): return False
            def update(self, n=1): pass
        return _Noop()

COMFY = Path('/content/ComfyUI')
APP = Path('/content/minimax-h3-colab/toybox')

if not (COMFY / 'main.py').is_file():
    raise RuntimeError('还没装 ComfyUI：先跑 ① 格，再回来跑这格。')

try:
    from google.colab import userdata as _ud
    _t = (_ud.get('HF_TOKEN') or '').strip()
    if _t:
        os.environ['HF_TOKEN'] = _t
        os.environ['HUGGING_FACE_HUB_TOKEN'] = _t
except Exception:
    pass
if not os.environ.get('HF_TOKEN'):
    print('ℹ️ 这一格是匿名下载（配额较低，可能限速）：权重都是 public 仓库，不需要授权同意\n')

def _read_civitai_token():
    token = os.environ.get('CIVITAI_TOKEN', '').strip()
    if token:
        return token, '环境变量'
    try:
        from google.colab import userdata as _ud2
        return ((_ud2.get('CIVITAI_TOKEN') or '').strip(), 'Colab 密钥')
    except Exception:
        return '', ''

CIVITAI_TOKEN, _token_src = _read_civitai_token()
if CIVITAI_TOKEN:
    os.environ['CIVITAI_TOKEN'] = CIVITAI_TOKEN
    print('🔑 Civitai token 已读到（来源：%s，%d 位）' % (_token_src, len(CIVITAI_TOKEN)))

REV_H3    = '54472221d27c695fca9621b0c3ed52f06c4af707'  # Comfy-Org/MiniMax-H3
REV_SING  = 'f35f94f7a09d48f637649f665a4d468784187468'  # OliviaRossi/Singularity
REV_ASSET = '1feae43759ca3af96d528a4312c48665d9f88811'  # dseditor/minimax-h3-studio-assets
REV_PH3   = '177745a635feb2f0adf3db3693297cb63fea3dda'  # PrunaAI/P-H3-Image-Comfy
REV_MOSS  = '2929efe3cdd506c0a430bcfca70a9a8d805cc095'  # Comfy-Org/MOSS-Image
REV_QWEN  = 'aacf31a6d48e707f352ecba714cee0680da3479b'  # Comfy-Org/Qwen-Image-2.1
REV_NOCTQ = 'a81b9af51120a78e285e57906f2250a2a02080e9'  # Noctaluna/Noct-Q-Uncensored-Qwen-Image-2.1

def W(opt, repo, rev, file, folder, size, sha, gated=False):
    return dict(opt=opt, repo=repo, rev=rev, file=file, folder=folder,
                size=size, sha=sha, gated=gated, source='hf')

def C(opt, label, version_id, file_id, file, folder, size, sha):
    """Civitai 来源：只能按 modelVersionId / fileId 取，sha256 由 API 给出。"""
    return dict(opt=opt, repo=label, rev='', file=file, folder=folder,
                size=size, sha=sha, gated=False, source='civitai',
                version_id=version_id, file_id=file_id)

MANIFEST = [
    W('core', 'OliviaRossi/Minimax-h3_Singularity-x-10Eros-Max_PRISM', REV_SING,
      'Minimax-h3_Singularity_x_10Eros-Max_PRISM_int8.safetensors', 'diffusion_models',
      20285875632, 'a8023aa1770ffaf689e5141f655a0a4cd6688296f67fa01819d6f4b06a77f188'),
    W('core', 'Comfy-Org/MiniMax-H3', REV_H3,
      'text_encoders/qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors', 'text_encoders',
      15687142551, '35a88d51044231fe332301d7a62aa81e3f2cba62febeb446e2c1e3e0ef76f2c6'),
    W('core', 'Comfy-Org/MiniMax-H3', REV_H3,
      'vae/minimax_h3_video_vae_int8_convrot.safetensors', 'vae',
      2811065184, '52a2c8c73583c86e4f41cdcce3a6ad0ea562987bc0bf3d60a0cef5f5c8e60c0e'),
    W('core', 'Comfy-Org/MiniMax-H3', REV_H3,
      'vae/minimax_h3_audio_vae_fp32.safetensors', 'vae',
      605254808, '8e505d95dd1561d47abd43d4238fd40d9bb1ae9e147ed0a4cba778d76ae4db48'),
    W('core', 'Comfy-Org/MiniMax-H3', REV_H3,
      'loras/pika_h3_flex_ref2va_v1_bf16.safetensors', 'loras',
      1956172304, 'de3745ceb7d8d5c20898dd8fa3a05f1c0b00089ccd55297b2a8bab95c766b0e0'),
    W('core', 'dseditor/minimax-h3-studio-assets', REV_ASSET,
      'loras/minimax_h3_turbo_v4_step600_ema_DasiwaREF2VAHybridV1_0_curveproj1025_compat_v001.safetensors', 'loras',
      794888704, 'a117f18682a22a0381cadd9832da0afe9912f604305b3e44310b5aa1919734cc'),
    W('core', 'dseditor/minimax-h3-studio-assets', REV_ASSET,
      'loras/MysticXXX_MMH3-V2.safetensors', 'loras',
      172057936, '2fc32615f20465e0831a5c8069df4006422fc9638a0b7faa216e04a6ddfee8de'),
    W('core', 'dseditor/minimax-h3-studio-assets', REV_ASSET,
      'loras/H3_Combat_V2.safetensors', 'loras',
      155110280, '5b3edb09e9d6029439badf9bc9db4ba4a355ae679b0810888de82c4793c0bac8'),
    W('core', 'dseditor/minimax-h3-studio-assets', REV_ASSET,
      'loras/Motion_Repair.safetensors', 'loras',
      155110272, 'a678a3657cefd5c2c2712f892da2f71be53c3f252e335557e918854f8abc579f'),
    W('core', 'dseditor/minimax-h3-studio-assets', REV_ASSET,
      'loras/H3_SPEED_slider.safetensors', 'loras',
      149166648, '381a6b14c126e891d6bcc823852881c8376b85df12651cac1f0beca0780b7f8e'),
    W('core', 'dseditor/minimax-h3-studio-assets', REV_ASSET,
      'semantic_bridge/t8_compat/BUNNY_H3_ActionLogic_Bridge_V1_T8_Compat.safetensors', 'semantic_bridge/t8_compat',
      22046928, '601769d0c6311628840a7bc5896b2a1035661f0ae2bddc602b782b955c95d3d8'),
    W('core', 'dseditor/minimax-h3-studio-assets', REV_ASSET,
      'latent_upscale_models/minimax_h3_latent_upscaler_3d_fp16_V2.safetensors', 'latent_upscale_models',
      690592672, '043e5a48e161610ef6c3ea974645220354d06fa618abca15f76d084812eb55c2'),
    W('rewriter', 'dseditor/minimax-h3-studio-assets', REV_ASSET,
      'text_encoders/minimax_h3_rewriter_ref2va_qwen3.5_9b_w4a8.safetensors', 'text_encoders',
      7343591088, '7c893fc3261e3362bf3975abf8ce60df54cbc57b03369eaa3eb1044adf0f5a1b'),
    W('image', 'PrunaAI/P-H3-Image-Comfy', REV_PH3,
      'loras/p-h3-image_lora_bf16.safetensors', 'loras',
      1956195360, '0e9013ae2873e3415dcb7bb59a9677293bdbdb6f663e138a79bb9bdced00ea61'),
    W('image', 'PrunaAI/P-H3-Image-Comfy', REV_PH3,
      'text_encoders/p-h3-image_text_encoder_int8_convrot.safetensors', 'text_encoders',
      5120870504, 'fb42c9ff0597e61729475d0b48b69b591ffe06d0b442a79dde22b14fadbe1c0f'),
    W('image', 'PrunaAI/P-H3-Image-Comfy', REV_PH3,
      'vae/p-h3-image_vae_int8_convrot.safetensors', 'vae',
      3146996440, '583e0d43354b6b833aa9a0815a5fac831b1ef6d0c19a7eaa0616980a3dab5ffe'),
    C('moss', 'Sateluco/Dirty-Moss bf16', 3423156, 3317613,
      'Dirty-Moss.safetensors', 'diffusion_models',
      10164673760, 'c67a8edc95c709d00b1da68b98d376372923dd6a1c1bfbab1dee1baaf78de99e'),
    W('moss', 'Comfy-Org/MOSS-Image', REV_MOSS,
      'text_encoders/qwen3.5_9b_int8_convrot.safetensors', 'text_encoders',
      9748121532, '32e3787472f708fa5f83624acbf5ef23f7fc89c439e05065ea57cfe5a97f025a'),
    W('moss', 'Comfy-Org/MOSS-Image', REV_MOSS,
      'vae/flux2-vae.safetensors', 'vae',
      336211292, '868fe7b343cc8f3a19dbcfcafbc3d5f888802be3f89bd81b65b3621a066ce8f3'),
    C('moss', 'salttaro/Better Nudes v3423785', 3423785, 3318330,
      'moss_pussy_lora_v2.0.safetensors', 'loras',
      528525216, '04230f8d7a45c0bf1917df10854c89bdb3c60d412609458adf44d52af4429640'),
    W('qwen', 'Noctaluna/Noct-Q-Uncensored-Qwen-Image-2.1', REV_NOCTQ,
      'NoctQ_V4_int8_convrot.safetensors', 'diffusion_models',
      7256784368, '4d92d5538253ab36e6a73b056cce5f697950f4303cb9e6686a3198d0ea13f9e8'),
    W('qwen', 'Comfy-Org/Qwen-Image-2.1', REV_QWEN,
      'text_encoders/qwen3vl_8b_int8_convrot.safetensors', 'text_encoders',
      9350798360, '8bfd0f6e12abf2d2d697ecc888e5e90b0d6741d6708f05799f53afa560452e8f'),
    W('qwen', 'Comfy-Org/Qwen-Image-2.1', REV_QWEN,
      'vae/qwen_image_2.1_vae_bf16.safetensors', 'vae',
      675509688, 'bb21f7473051e1ac368515dd3f2e15cd44d7a11748ee8823e1ddca3e4876b7c9'),
]

_engine_file = Path('/content/.h3_image_engine')
try:
    _from_disk = _engine_file.read_text(encoding='utf-8').strip()
except OSError:
    _from_disk = ''
if _from_disk in ('moss', 'qwen', 'ph3', 'none') and _from_disk != IMAGE_ENGINE:
    print('ℹ️ ① 格选的是 %s，以它为准（要改就两个下拉一起改）' % _from_disk)
    IMAGE_ENGINE = _from_disk
try:
    _engine_file.write_text(IMAGE_ENGINE, encoding='utf-8')
except OSError:
    pass

if IMAGE_ENGINE == 'ph3':
    items = [i for i in MANIFEST if i['opt'] in ('core', 'rewriter', 'image')]
elif IMAGE_ENGINE == 'moss':
    items = [i for i in MANIFEST if i['opt'] in ('core', 'rewriter', 'moss')]
elif IMAGE_ENGINE == 'qwen':
    items = [i for i in MANIFEST if i['opt'] in ('core', 'rewriter', 'qwen')]
elif IMAGE_ENGINE == 'none':
    items = [i for i in MANIFEST if i['opt'] in ('core', 'rewriter')]
else:
    raise ValueError('IMAGE_ENGINE 只能是 moss / qwen / ph3 / none，现在是：%r' % (IMAGE_ENGINE,))

if KEEP_BOTH and IMAGE_ENGINE in ('moss', 'qwen', 'ph3'):
    _other = 'qwen' if IMAGE_ENGINE == 'moss' else 'moss'
    _have = {(i['folder'], i['file']) for i in items}
    _extra = [i for i in MANIFEST
              if i['opt'] == _other and (i['folder'], i['file']) not in _have]
    items = items + _extra
    print('🔀 KEEP_BOTH：另加 %d 个文件（%s，%.2f GiB），两套生图引擎都可切换' % (
        len(_extra), _other, sum(i['size'] for i in _extra) / 2**30))

print('🎨 生图引擎：%s → 本次下载 %d 个文件，%.2f GiB\n' % (
    IMAGE_ENGINE, len(items), sum(i['size'] for i in items) / 2**30))
if any(i.get('source') == 'civitai' for i in items) and not CIVITAI_TOKEN:
    print('⚠️ 选了 moss 但没有 Civitai token：Dirty-Moss 底模和 LoRA（9.96 GiB）下不来。')
    print('   解决：Colab 左侧「🔑 密钥」加一项，名称必须是 CIVITAI_TOKEN，')
    print('         值贴 32 位的 key，并勾选本笔记本的访问权（跟 HF_TOKEN 一样的做法）')
    print('   备选：colab exec -s h3 --env CIVITAI_TOKEN=xxx（只对那次有效）\n')

try:
    from huggingface_hub import constants as _hf_const
    from huggingface_hub import file_download as _hf_fd
    _xet = _hf_fd.is_xet_available()
    print('📦 传输：%s' % ('xet 分块并行（快，失败会自动降级）' if _xet
                          else '标准 HTTP（xet 不可用）'))
    if not _xet:
        print('   hf_xet 没装上，只能用标准 HTTP（约 115 MB/s）—— 能下，只是慢。')
        print('   想要 400 MB/s：pip install hf_xet，然后重跑本格。')
except Exception:
    pass

def _xet_switch(on):
    """运行时开关 xet。constants.HF_HUB_DISABLE_XET 是模块变量，file_download 每次都重读，
    所以这里改完立刻生效（不要改 os.environ，那个只在 import 时读一次）。"""
    try:
        from huggingface_hub import constants as _c
        _c.HF_HUB_DISABLE_XET = not on
        return not on
    except Exception:
        return None

XET_BAD_LOW, XET_BAD_HIGH = 8 << 30, 12 << 30

def hf_fetch(item, tries=3):
    """hf_hub_download 包一层重试；xet 出错就关掉它、换标准 HTTP 再试。"""
    target = COMFY / 'models' / item['folder'] / item['file'].rsplit('/', 1)[-1]
    size = int(item.get('size') or 0)
    if XET_BAD_LOW <= size <= XET_BAD_HIGH:
        url = 'https://huggingface.co/%s/resolve/%s/%s' % (
            item['repo'], item['rev'], item['file'])
        total, ranged = _range_probe(url, item)
        total = total or size
        partial = target.with_name('.' + target.name + '.part')
        have = partial.stat().st_size if partial.is_file() else 0
        if total and have > total:
            partial.unlink(missing_ok=True)
            have = 0
        print('   ℹ️ %.1f GiB 走 CDN 分段（绕开 xet 的 74~77%% 卡死）'
              % (size / 2**30), flush=True)
        return _fetch(url, target, total, target.name[:44], have=have, ranged=ranged)
    xet_on = True
    last = None
    for attempt in range(1, tries + 1):
        try:
            got = Path(hf_hub_download(repo_id=item['repo'], filename=item['file'],
                                       revision=item['rev'],
                                       local_dir=str(COMFY / 'models')))
            if got.resolve() != target.resolve() and got.is_file():
                shutil.move(str(got), str(target))
            return target
        except Exception as error:
            last = error
            text = (type(error).__name__ + str(error)).lower()
            retryable = any(k in text for k in ('cas ', 'reconstruction', 'xet',
                                                'connection', 'timeout', 'reset',
                                                'incomplete', 'eof', 'incompleteread'))
            if not retryable or attempt == tries:
                raise
            if xet_on:
                _xet_switch(False)
                xet_on = False
                print('   ⚠️ xet 传输出错（%s），改用标准 HTTP 重试' % type(error).__name__,
                      flush=True)
                time.sleep(3)
            else:
                print('   ⚠️ 第 %d/%d 次失败（%s），%d 秒后重试'
                      % (attempt, tries, type(error).__name__, 5 * attempt), flush=True)
                time.sleep(5 * attempt)
    raise last

def sha256_of(path, chunk=8 << 20):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for block in iter(lambda: f.read(chunk), b''):
            h.update(block)
    return h.hexdigest()

def stamp(path):
    """戳记跟权重同一个目录；权重被清掉，戳记也跟著没，不会残留成假凭证。"""
    return path.with_name(path.name + '.sha256')

def read_stamp(path):
    try:
        sha, size = stamp(path).read_text().split()
        return sha, int(size)
    except Exception:
        return '', -1

def write_stamp(path, item):
    try:
        stamp(path).write_text('%s %d\n' % (item['sha'], item['size']))
    except Exception:
        pass

def drop(path):
    path.unlink(missing_ok=True)
    stamp(path).unlink(missing_ok=True)

# __CIVITAI_BEGIN__
class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None

def _civitai_direct(item, token):
    """走 civitai 的第一跳 307，拿到 R2 预签名直链。token 只发给 civitai.com 这一跳。"""
    url = 'https://civitai.com/api/download/models/%d?fileId=%d' % (
        item['version_id'], item['file_id'])
    if token:
        url += '&token=' + urllib.parse.quote(token)
    request = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
    if token:
        request.add_header('Authorization', 'Bearer ' + token)
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        opener.open(request, timeout=60).close()
    except urllib.error.HTTPError as error:
        location = error.headers.get('Location') or ''
        error.close()
        if '/login' in location:
            raise RuntimeError(
                'Civitai 要求登录才能下这个文件。请在 Colab 左侧「🔑 密钥」加一项 '
                'CIVITAI_TOKEN（值贴 32 位的 key，并勾选本笔记本访问权），再重跑本格。')
        if not location:
            raise RuntimeError('Civitai 没给下载地址（配额用完或文件被移除）')
        return location
    raise RuntimeError('Civitai 直接返回了内容而不是跳转，无法确认来源')

def _range_probe(url, item):
    """回 (总字节, 是否支持 Range)。

    HEAD 被挡就拿预期大小顶上；Range 支持与否用一次 1 字节的 GET 实测 ——
    预签名直链上 HEAD 有时不返回 Accept-Ranges，只看它会把能并行的当成不支持。
    """
    total, accepts = int(item.get('size') or 0), False
    try:
        head = urllib.request.Request(url, method='HEAD', headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(head, timeout=30) as r:
            total = int(r.headers.get('Content-Length') or total)
            accepts = 'bytes' in (r.headers.get('Accept-Ranges') or '').lower()
    except Exception:
        pass
    if accepts:
        return total, True
    try:
        probe = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0',
                                                    'Range': 'bytes=0-0'})
        with urllib.request.urlopen(probe, timeout=30) as r:
            if getattr(r, 'status', None) == 206:
                span = r.headers.get('Content-Range') or ''
                m = re.search(r'/(\d+)', span)
                if m:
                    total = int(m.group(1))
                return total, True
    except Exception:
        pass
    return total, False

def _segment(url, start, end, dest, tries=5):
    """把 [start, end] 这段写进 dest 的对应偏移。断了自己重来，重试有上限。"""
    want = end - start + 1
    last = ''
    for attempt in range(1, tries + 1):
        got = 0
        try:
            req = urllib.request.Request(url, headers={
                'User-Agent': 'Mozilla/5.0', 'Range': 'bytes=%d-%d' % (start, end)})
            with urllib.request.urlopen(req, timeout=120) as r:
                if getattr(r, 'status', None) not in (206, 200):
                    raise RuntimeError('status %s' % getattr(r, 'status', '?'))
                with open(dest, 'r+b') as out:
                    out.seek(start)
                    while got < want:
                        block = r.read(min(4 << 20, want - got))
                        if not block:
                            break
                        out.write(block)
                        got += len(block)
                        _bump(len(block))
            if got == want:
                return got
            last = '只收到 %d/%d' % (got, want)
        except Exception as error:
            last = type(error).__name__
        time.sleep(2 * attempt)
    raise RuntimeError('分段 %d-%d 重试 %d 次仍失败（%s）' % (start, end, tries, last))

def _fetch(url, target, total, name, have=0, workers=8, ranged=True):
    """一条下载路径：支持 Range 就分段并发，不支持就单连接。

    have>0 表示续传（接着 .part 已有的字节往下写，不 truncate）。
    分段直接写进同一个文件的偏移，不落分段临时文件 —— 9 GiB 级拼接在 Colab
    叠层盘上比省下的时间还贵。
    """
    global _BAR
    workers = max(1, min(workers, 16))
    ranged = ranged and total > (32 << 20)
    partial = target.with_name('.' + target.name + '.part')
    if have and not ranged:
        partial.unlink(missing_ok=True)       # 单连接没法从中间接，只能重来
        have = 0
    _BAR = tqdm(total=total or None, unit='B', unit_scale=True, unit_divisor=1024,
                desc=name + ((' [%d 连接]' % workers) if ranged else ' [单连接]'),
                leave=True)
    try:
        with _BAR:
            if have:
                _BAR.update(have)
            if not partial.is_file():
                with open(partial, 'wb') as out:
                    out.truncate(total or 0)   # 预分配，各段才有地方 seek
            jobs = _jobs(have, total, workers) if ranged else []
            if not jobs:                       # 单连接：整份顺序写
                partial.unlink(missing_ok=True)
                _fetch_stream(url, partial)
            else:
                errors = []

                def _run(start, end):
                    try:
                        _segment(url, start, end, str(partial))
                    except Exception as error:
                        errors.append(error)

                threads = [threading.Thread(target=_run, args=(s, e), daemon=True)
                           for s, e in jobs]
                for t in threads:
                    t.start()
                for t in threads:
                    t.join()
                if errors:
                    raise RuntimeError('分段下载失败：%s' % errors[0])
        written = partial.stat().st_size
        if total and written != total:
            raise RuntimeError('下完 %d 字节，应为 %d（分段被截断）' % (written, total))
        with open(partial, 'rb') as probe:     # 真 safetensors = 8 字节头长 + '{'
            head, blob = probe.read(8), probe.read(64)
        if len(head) < 8 or not (0 < struct.unpack('<Q', head)[0] < 100_000_000) \
                or blob.lstrip()[:1] != b'{':
            raise RuntimeError('下载到的不是 safetensors（可能拿到登录页或错误页）')
        partial.replace(target)
    except BaseException:
        raise                                  # 失败保留 .part，下次续传
    return written

def _jobs(have, total, workers):
    """把 [have, total) 均分成 workers 段。"""
    span = (total - have + workers - 1) // workers
    out = []
    for i in range(workers):
        start = have + i * span
        end = min(total, start + span) - 1
        if start <= end:
            out.append((start, end))
    return out

def _fetch_stream(url, target, chunk=8 << 20):
    """Range 不支持时的退路：单连接顺序流式。"""
    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
    with urllib.request.urlopen(req, timeout=120) as stream, open(target, 'wb') as out:
        while True:
            block = stream.read(chunk)
            if not block:
                break
            out.write(block)
            _bump(len(block))

def _bump(n):
    with _bar_lock:
        _BAR.update(n)

def _civitai_fetch(item, target, token, workers=8):
    """Civitai：拿 307 直链 → 探 Range → 交给同一个 _fetch()。"""
    direct = _civitai_direct(item, token)
    total, ranged = _range_probe(direct, item)
    total = total or int(item.get('size') or 0)
    partial = target.with_name('.' + target.name + '.part')
    have = partial.stat().st_size if partial.is_file() else 0
    if total and have > total:
        partial.unlink(missing_ok=True)        # 比目标还大 = 脏档，重来
        have = 0
    return _fetch(direct, target, total, item['file'].rsplit('/', 1)[-1][:44],
                  have=have, workers=workers, ranged=ranged)

# __CIVITAI_END__

t0 = time.time()
issues = []
bytes_moved = []    # 本次真正下载过的文件（相对路径, 字节），用来算传输均速
items = sorted(items, key=lambda it: -it['size'])
for item in items:
    target = COMFY / 'models' / item['folder'] / item['file'].rsplit('/', 1)[-1]
    target.parent.mkdir(parents=True, exist_ok=True)
    name = target.name
    note = ''

    if target.is_file():
        got = target.stat().st_size
        if got != item['size']:
            note = '大小不符（%d ≠ %d）→ 重下' % (got, item['size'])
            drop(target)
        else:
            sha, size = read_stamp(target)
            if sha == item['sha'] and size == item['size'] and not VERIFY_HASH:
                print('已验 {:<52} {:6.3f} GiB'.format(name[:52], got / 2**30))
                continue
            print('校验 {} …'.format(name[:56]), flush=True)
            if sha256_of(target) == item['sha']:
                write_stamp(target, item)
                print('校验通过 {:<50} {:6.3f} GiB'.format(name[:50], got / 2**30))
                continue
            note = '哈希不符 → 重下'
            drop(target)

    print(('%s → ' % note if note else '下载 ') + name, flush=True)
    try:
        if item.get('source') == 'civitai':
            _got = _civitai_fetch(item, target, CIVITAI_TOKEN)
        else:
            hf_fetch(item)          # 直接落到 models/ 对应目录
    except Exception as e:
        msg = '%s: %s' % (type(e).__name__, str(e)[:90])
        low = (str(e) + type(e).__name__).lower()
        denied = any(k in low for k in ('401', '403', 'gated', 'unauthorized',
                                        'forbidden', 'invalid username'))
        if denied:
            issues.append((name, '下载失败（401/403）：%s' % msg))
            print('   ❌ %s' % msg, flush=True)
            print('      可能原因，按顺序排：', flush=True)
            print('      1. repo id 拼错 —— HF 对不存在的仓库也报 "Invalid username or password."（401），'
                  '不代表是 gated', flush=True)
            print('         先在浏览器打开 https://huggingface.co/%s 看是不是 404' % item['repo'], flush=True)
            print('      2. 真是 gated —— 用当前 HF 账号点「Agree and access repository」，'
                  '且 Colab 密钥里的 HF_TOKEN 属于同一个账号', flush=True)
            print('      3. token 失效 —— ① 格应该印过「✅ HF_TOKEN 已读到并通过验证」；'
                  '这一格拿到的 token：%s' % ('有' if os.environ.get('HF_TOKEN') else '没有'), flush=True)
        else:
            issues.append((name, '下载失败：%s' % msg))
            print('   ❌ 下载失败：%s' % msg, flush=True)
        continue

    size = target.stat().st_size
    if size != item['size']:
        issues.append((name, '下完大小不符 %d ≠ %d' % (size, item['size'])))
        drop(target)
        continue
    want_hash = VERIFY_HASH or size <= (2 << 30) or (
        size <= (8 << 30) and item.get('source') == 'civitai')
    if want_hash and sha256_of(target) != item['sha']:
        issues.append((name, '下完哈希不符，已删除'))
        drop(target)
        continue
    write_stamp(target, item)
    bytes_moved.append((item['file'], size))
    print('   ✅ {:<24} {:<48} {:6.3f} GiB'.format(item['folder'], name[:48], size / 2**30), flush=True)

GUIDES = APP / 'guides'
GUIDES.mkdir(parents=True, exist_ok=True)
for g in ('VIDEO_PROMPT_WRITING_GUIDE_ref_en.md', 'VIDEO_PROMPT_WRITING_GUIDE_base_en.md'):
    if (GUIDES / g).is_file() and (GUIDES / g).stat().st_size:
        continue
    try:
        got = Path(hf_hub_download(repo_id='MiniMaxAI/MiniMax-H3', filename='docs/' + g,
                                   local_dir=str(GUIDES)))
        if got.resolve() != (GUIDES / g).resolve() and got.is_file():
            shutil.move(str(got), str(GUIDES / g))
    except Exception as error:
        print('⚠️ 改写指南暂不可用：', g, error)
        print('   权重校验照常完成，重跑本格可补指南。')

print('\n' + '=' * 76)
total = 0.0
for item in items:
    target = COMFY / 'models' / item['folder'] / item['file'].rsplit('/', 1)[-1]
    got = target.stat().st_size if target.is_file() else 0
    total += got / 2**30
    state = 'OK' if got == item['size'] else ('缺失' if got == 0 else '不完整')
    print('{:6.3f}/{:6.3f} GiB  {:<7} {}'.format(
        got / 2**30, item['size'] / 2**30, state, item['file'].rsplit('/', 1)[-1][:46]))
print('=' * 76)
_elapsed = time.time() - t0
print('合计 {:.2f} GiB（{} 个文件）／耗时 {:.0f} 秒／哈希模式：{}'.format(
    total, len(items), _elapsed, '全验' if VERIFY_HASH else '已盖戳按大小和戳记检查，其余验哈希'))
_dl = sum(sz for rel, sz in bytes_moved)
print('传输 {:.2f} GiB／本次实下 {} 个文件／平均 {:.1f} MB/s'.format(
    _dl / 2**30, len(bytes_moved), _dl / 1e6 / max(_elapsed, 0.1)))
if bytes_moved and _dl / 1e6 / max(_elapsed, 0.1) < 20:
    print('   偏慢：大头是 9 GiB 级文件落到 Colab 叠层盘（约 15~20 MB/s）。'
          '前面的高速是内存缓冲，不是真实写盘速度。')

if issues:
    print('\n⚠️ {} 个文件有问题，重跑本格会自动补下：'.format(len(issues)))
    for n, why in issues:
        print('   ·', n, '—', why)
    if any('401' in why or '403' in why for _n, why in issues):
        print('\n🔑 401 先别急着找授权，按顺序排：')
        print('   ① repo id 拼错：HF 对不存在的仓库也回 "Invalid username or password."（401）。')
        print('      在浏览器打开 https://huggingface.co/OliviaRossi/Minimax-h3_Singularity-x-10Eros-Max_PRISM')
        print('      注意是 Singularity**-**x**-**10Eros（两个连字符），404 就是名字错了')
        print('   ② token 问题：重跑 ① 格，看有没有「✅ HF_TOKEN 已读到并通过验证」')
        print('   ③ 整个清单现在都是 public 仓库，理论上不需要任何授权同意')
    raise RuntimeError('权重未就绪，重跑 ② 格补齐后再启动。')
else:
    print('✅ 全部就绪：revision 已钉死，字节全部对上')