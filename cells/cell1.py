#@title ① 检查环境＋安装 ComfyUI、节点与 H3 Toybox（GPU 或驱动不符就停下，不会安装）
#@markdown 生图引擎跟 ② 格同一个选择；改了这里也会同步到 ② 格。
IMAGE_ENGINE = globals().get("IMAGE_ENGINE", "moss")
import torch, shutil, subprocess, sys, os
from pathlib import Path
# 传输策略由 ② 格的 hf_fetch() 在运行时决定（先 xet 后降级），这里只给初值。
os.environ['HF_HUB_DISABLE_XET'] = '0'
os.environ['HF_HUB_DOWNLOAD_TIMEOUT'] = '60'
if not torch.cuda.is_available():
    raise RuntimeError('没有 GPU：请到「执行阶段 → 变更执行阶段类型」选 L4 / G4（RTX PRO 6000），再重跑这格')
cap = torch.cuda.get_device_capability(0)
drv = subprocess.run(['nvidia-smi', '--query-gpu=driver_version', '--format=csv,noheader'], capture_output=True, text=True).stdout.strip()
print('GPU:', torch.cuda.get_device_name(0), '| capability', cap, '| VRAM', round(torch.cuda.get_device_properties(0).total_memory / 2**30, 1), 'GiB')
print('驱动:', drv, '| 可用磁盘', round(shutil.disk_usage('/content').free / 2**30, 1), 'GiB')
if cap < (8, 0):
    raise RuntimeError(f'这张卡（capability {cap}）跑不动 int8 模型，没有安装任何东西。'
                       '请换 L4 / G4（或 A100）再重跑。')
if int(drv.split('.')[0]) < 580:
    raise RuntimeError(f'驱动 {drv} 不支援 CUDA 13（需要 580 以上），没有安装任何东西。'
                       '请换一台执行阶段再试。')
# 把引擎选择写到磁盘，②③ 格都靠它保持一致（kernel 重启后变量会丢）
try:
    Path('/content/.h3_image_engine').write_text(IMAGE_ENGINE, encoding='utf-8')
except OSError:
    pass
# 已下载、大小正确的文件不再占用本轮的新增空间。
# 门槛按所选引擎分档 —— 选 moss（bf16 底模）要 89 GiB，标准规格 78 GiB 装不下，
# 高内存规格（96 GiB+）够。
WEIGHT_SIZES = (('core', 'diffusion_models/Minimax-h3_Singularity_x_10Eros-Max_PRISM_int8.safetensors', 20285875632), ('core', 'text_encoders/qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors', 15687142551), ('core', 'vae/minimax_h3_video_vae_int8_convrot.safetensors', 2811065184), ('core', 'vae/minimax_h3_audio_vae_fp32.safetensors', 605254808), ('core', 'loras/pika_h3_flex_ref2va_v1_bf16.safetensors', 1956172304), ('core', 'loras/minimax_h3_turbo_v4_step600_ema_DasiwaREF2VAHybridV1_0_curveproj1025_compat_v001.safetensors', 794888704), ('core', 'loras/MysticXXX_MMH3-V2.safetensors', 172057936), ('core', 'loras/H3_Combat_V2.safetensors', 155110280), ('core', 'loras/Motion_Repair.safetensors', 155110272), ('core', 'loras/H3_SPEED_slider.safetensors', 149166648), ('core', 'semantic_bridge/t8_compat/BUNNY_H3_ActionLogic_Bridge_V1_T8_Compat.safetensors', 22046928), ('core', 'latent_upscale_models/minimax_h3_latent_upscaler_3d_fp16_V2.safetensors', 690592672), ('rewriter', 'text_encoders/minimax_h3_rewriter_ref2va_qwen3.5_9b_w4a8.safetensors', 7343591088), ('image', 'loras/p-h3-image_lora_bf16.safetensors', 1956195360), ('image', 'text_encoders/p-h3-image_text_encoder_int8_convrot.safetensors', 5120870504), ('image', 'vae/p-h3-image_vae_int8_convrot.safetensors', 3146996440), ('moss', 'diffusion_models/Dirty-Moss.safetensors', 10164673760), ('moss', 'text_encoders/qwen3.5_9b_int8_convrot.safetensors', 9748121532), ('moss', 'vae/flux2-vae.safetensors', 336211292), ('moss', 'loras/moss_pussy_lora_v2.0.safetensors', 528525216), ('qwen', 'diffusion_models/NoctQ_V4_int8_convrot.safetensors', 7256784368), ('qwen', 'text_encoders/qwen3vl_8b_int8_convrot.safetensors', 9350798360), ('qwen', 'vae/qwen_image_2.1_vae_bf16.safetensors', 675509688))
needed = 0
# 分组名和引擎名不一致（P-H3 那组在清单里叫 'image'，引擎名是 'ph3'），显式映射。
# 不映射的话选 ph3 时这 9.52 GiB 会被漏算，磁盘门槛就报少了。
_OPT_FOR_ENGINE = {'ph3': 'image', 'moss': 'moss', 'qwen': 'qwen', 'none': None}
_mine = _OPT_FOR_ENGINE.get(IMAGE_ENGINE, 'image')
for opt, path, size in WEIGHT_SIZES:
    if opt not in ('core', 'rewriter', _mine):
        continue
    local = Path('/content/ComfyUI/models') / path
    if not local.is_file() or local.stat().st_size != size:
        needed += size
free = shutil.disk_usage('/content').free
# 大规格（≥96 GiB）留 22 GiB 够了；标准规格（78 GiB）本身就紧，只留 12 GiB，
# 免得「明明装得下」被一刀切的预留误判成不足。
headroom = 22 if free >= 96 * 2**30 else 12
print('生图引擎：%s｜本轮要下 %.1f GiB｜预留 %.0f GiB｜可用 %.1f GiB' % (
    IMAGE_ENGINE, needed / 2**30, headroom, free / 2**30))
if free < needed + headroom * 2**30:
    raise RuntimeError(
        f'磁盘不足：本轮要下 {needed / 2**30:.1f} GiB，另需 {headroom} GiB 余量，'
        f'可用只有 {free / 2**30:.1f} GiB。\n'
        '  · moss（bf16 底模）约需 89 GiB：请把执行阶段规格换成「高内存」\n'
        '  · 换 ph3 约需 79 GiB，none 约需 69 GiB\n'
        '  · 高内存规格下仍报这个，说明开的不是高内存，重开执行阶段再试')
print('✅ 环境检查通过，开始安装')
# HF_TOKEN 只用一种方式带进去：让它变得「不可能静默失败」。
# 以前是 try/except 全吞，token 拼错或没开笔记本访问权时只会印一行「没有设置」，然后
# 跑到 ② 格才因为 gated 401 崩掉，看不出真正原因。
_HF_TOKEN = ''
try:
    from google.colab import userdata
    _HF_TOKEN = (userdata.get('HF_TOKEN') or '').strip()
except Exception as _e:
    # SecretNotFoundError 就是「没配这一项」，属正常情况，交给下面的分支印 ℹ️
    if 'SecretNotFound' not in type(_e).__name__:
        print('⚠️ 读不到 Colab 密钥：%s: %s' % (type(_e).__name__, _e))

if _HF_TOKEN:
    os.environ['HF_TOKEN'] = _HF_TOKEN
    os.environ['HUGGING_FACE_HUB_TOKEN'] = _HF_TOKEN
    # 立刻验一次：能拿到自己的用户名，才代表这把 token 真的被 Hub 认了
    _who = subprocess.run(
        [sys.executable, '-c',
         'import os,urllib.request as u,json;'
         'r=u.Request("https://huggingface.co/api/whoami-v2",'
         'headers={"Authorization":"Bearer "+os.environ["HF_TOKEN"]});'
         'print(json.load(u.urlopen(r,timeout=20))["name"])'],
        capture_output=True, text=True)
    if _who.returncode == 0:
        print('✅ HF_TOKEN 已读到并通过验证，账号：', _who.stdout.strip())
        print('   作用是提高下载限额；②格的权重全是 public 仓库，不需要任何授权同意')
    else:
        print('⚠️ HF_TOKEN 读到了，但 Hub 不认（%s）' % _who.stderr.strip()[-160:])
        print('   常见原因：token 复制不全、选成了 fine-grained 但没勾 Read、或已被撤销')
        print('   不影响下载：②格的权重都是 public 仓库，匿名也能下（只是会被限速）')
elif 'google.colab' in sys.modules:
    print('ℹ️ 没有 HF_TOKEN：②格的权重全是 public，照样能下，只是容易被限速')
    print('   想提速：左侧「🔑 密钥」加一项，名称必须是 HF_TOKEN，值贴 hf_ 开头的 token，并勾选本笔记本的访问权')
else:
    print('ℹ️ 不在 Colab 里，也没有 HF_TOKEN（本地自测可忽略）')

def clone(url, dest, revision):
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    repo = ['git', '-C', str(dest)]
    if not (dest / '.git').is_dir():
        subprocess.run([*repo, 'init', '-q'], check=True)
    head = subprocess.run([*repo, 'rev-parse', '--verify', 'HEAD'], capture_output=True, text=True)
    if head.stdout.strip() != revision:
        subprocess.run([*repo, 'fetch', '--depth', '1', url, revision], check=True)
        subprocess.run([*repo, 'checkout', '--detach', revision], check=True)

COMFY = Path('/content/ComfyUI')
clone('https://github.com/Comfy-Org/ComfyUI.git', COMFY, 'f99de883d5e65317c7f4a4b617b5aa7936622029')
if not (torch.version.cuda or '').startswith('13'):
    print('安装 CUDA 13 版 PyTorch（int8 ConvRot 权重需要）…')
    subprocess.run([sys.executable, '-m', 'pip', 'install', '-q', 'torch', 'torchvision', 'torchaudio',
                    '--index-url', 'https://download.pytorch.org/whl/cu130'], check=True)
# hf_xet 要装上：它是 400 MB/s 那条快路的唯一依赖。没有它只能走标准 HTTP（约 115 MB/s）。
# 它偶发 CAS 重建失败，但 ② 格的 hf_fetch() 会在失败后自动降级到标准 HTTP，所以装上是纯收益。
subprocess.run([sys.executable, '-m', 'pip', 'install', '-q', '-r', str(COMFY / 'requirements.txt'),
                'huggingface_hub', 'hf_xet'], check=True)
# 工作流用到的三个节点组（都不会动到 PyTorch）
NODES = {
    'minimax-h3-audio-t8': ('https://github.com/T8mars/comfyui-minimax-h3-audio-T8', 'be3a3e9219feb63848b3415f1dd1d1571b812203'),
    'comfyui-SelfLift': ('https://github.com/facok/comfyui-SelfLift', '835c3cf7919410f71f414688f049c6ca497789d0'),
    'ComfyUI-H3-Motion-Context': ('https://github.com/NikoDemon80/ComfyUI-H3-Motion-Context', '518c083d11d80fa5920222ad89889924ee4401c9'),
}
for name, (url, revision) in NODES.items():
    clone(url, COMFY / 'custom_nodes' / name, revision)
# 导演 Skill 放在 custom_nodes 外面，只读取文字内容。
clone('https://github.com/T8mars/comfyui-minimax-h3-prompt-enhancer-T8', '/content/t8-enhancer',
      'e63802929913034ad89b7572e5e6651bf4493393')
# H3 Toybox 本体
clone('https://github.com/dseditor/minimax-h3-colab.git', '/content/minimax-h3-colab',
      revision='44708d4aa17e30fc8b56be375387ba2106936a9c')
APP = Path('/content/minimax-h3-colab/toybox')
print('ffmpeg:', shutil.which('ffmpeg') or '没有（延伸影片会失敗）')
# 直接读当前进程里的 torch 就行 —— 子进程再 import 一次要几秒，
# 而且装完新 torch 后当前进程还是旧的，测的是旧版本，本来就不准。
print('ComfyUI 会用的 PyTorch：', torch.__version__, torch.version.cuda,
      '| CUDA 可用：', torch.cuda.is_available())
if not torch.cuda.is_available():
    raise RuntimeError('安装后的 PyTorch 无法使用 GPU，请检查上面的错误。')
print('✅ 安装完成')