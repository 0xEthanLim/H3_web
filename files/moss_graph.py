"""MOSS-Image 高画质双段出图的 ComfyUI API 图构造器。

来源：C:\\Users\\29120\\Downloads\\工作流\\release\\HIRES\\QIM_DirtyMoss_Hires.json
（四份候选里选它：唯一带高清重绘二段、零第三方节点。见 README 第 1 节）

只做纯函数：给尺寸/提示词/种子，回一个可以直接 POST 到 /prompt 的 dict。
不碰文件、不碰网络、不读写 config —— 这样它能被离线自测完整覆盖。

16 个节点的连线与 release 工作流逐条一致：
    UNETLoader → LoraLoaderModelOnly → ModelSamplingAuraFlow → KSampler(第一采)
    CLIPLoader → CLIPTextEncode×2 ─────────────────────────────┘
    EmptyFlux2LatentImage ──────────────────────────────────────┘
    KSampler → VAEDecode → ImageScaleBy(1.5) → VAEEncode → KSampler(第二采) → VAEDecode → SaveImage
"""

from __future__ import annotations

import math

# 第一采的默认参数，取自 release 工作流 node 10
FIRST_STEPS, FIRST_CFG, FIRST_SAMPLER, FIRST_SCHED = 25, 2.0, 'er_sde', 'simple'
# 第二采（高清重绘），取自 release 工作流 node 14
SECOND_STEPS, SECOND_CFG, SECOND_DENOISE = 8, 2.0, 0.15
UPSCALE_METHOD, UPSCALE_BY = 'lanczos', 1.5
SHIFT = 5.0

DEFAULT_NEGATIVE = ('worst quality, bad anatomy, watermark, jpeg artifact, '
                    'extra fingers, bad hands, fused limbs')


def _snap(value, multiple):
    """最近的一档，正好一半时向上。"""
    return max(multiple, (int(value) + multiple // 2) // multiple * multiple)


def round32(value):
    """latent 尺寸必须是 32 的倍数，否则 flux2 的分块会报错。"""
    return _snap(value, 32)


def round64(value):
    """第一采尺寸取 64 的倍数。

    这不是多余的严格：1.5× 放大后要再对齐 32，只有 64 的倍数做得到。
    w*3/2 ≡ 0 (mod 32)  ⟺  w ≡ 0 (mod 64)。
    release 工作流选 896×1152 正是这个道理 —— 放大后 1344×1728 仍然对齐 32。
    """
    return _snap(value, 64)


def upscaled(width, height, factor=UPSCALE_BY):
    """第二采的实际像素尺寸：ImageScaleBy 的输出，再对齐 32 保证 latent 合法。"""
    return (round32(round(int(width) * factor)),
            round32(round(int(height) * factor)))


def sizes(width, height, factor=UPSCALE_BY):
    """给前端/日志用：两段各自的真实像素尺寸。"""
    w, h = round64(width), round64(height)
    w2, h2 = upscaled(w, h, factor)
    return {'first': (w, h), 'second': (w2, h2)}


def graph(*, unet, clip, vae, prompt, negative=DEFAULT_NEGATIVE,
          width=896, height=1152, seed=0, seed2=None,
          steps=FIRST_STEPS, cfg=FIRST_CFG, hires_steps=SECOND_STEPS,
          hires_denoise=SECOND_DENOISE, factor=UPSCALE_BY,
          lora=None, lora_strength=1.0, shift=SHIFT,
          slg_layers='', prefix='MOSSToybox/img'):
    """构造 HIRES 双段图。lora=None 时不插入 LoRA 节点，其余连线不变。"""
    if not str(prompt or '').strip():
        raise ValueError('提示词不能为空。')
    w, h = round64(width), round64(height)
    seed = int(seed) if seed not in (None, '', -1) else 0
    seed2 = seed if seed2 in (None, '', -1) else int(seed2)

    g = {
        'u': {'class_type': 'UNETLoader',
              'inputs': {'unet_name': unet, 'weight_dtype': 'default'}},
        'c': {'class_type': 'CLIPLoader',
              'inputs': {'clip_name': clip, 'type': 'moss_image', 'device': 'default'}},
        'v': {'class_type': 'VAELoader', 'inputs': {'vae_name': vae}},
        'p': {'class_type': 'CLIPTextEncode', 'inputs': {'text': prompt, 'clip': ['c', 0]}},
        'n': {'class_type': 'CLIPTextEncode', 'inputs': {'text': negative, 'clip': ['c', 0]}},
        'e': {'class_type': 'EmptyFlux2LatentImage',
              'inputs': {'width': w, 'height': h, 'batch_size': 1}},
    }
    upstream = ['u', 0]
    if lora:
        g['l'] = {'class_type': 'LoraLoaderModelOnly',
                  'inputs': {'model': ['u', 0], 'lora_name': lora,
                             'strength_model': float(lora_strength)}}
        upstream = ['l', 0]
    g['s'] = {'class_type': 'ModelSamplingAuraFlow',
              'inputs': {'model': upstream, 'shift': float(shift), 'sampling': 'flow'}}
    model = ['s', 0]
    if slg_layers:
        # release 工作流里这一格是空串，等于 no-op；只有真的要跳层时才插。
        g['g'] = {'class_type': 'SkipLayerGuidanceDiTSimple',
                  'inputs': {'model': ['s', 0], 'double_layers': '',
                             'single_layers': str(slg_layers),
                             'start_percent': 0.0, 'end_percent': 1.0}}
        model = ['g', 0]

    g['k1'] = {'class_type': 'KSampler',
               'inputs': {'model': model, 'positive': ['p', 0], 'negative': ['n', 0],
                          'latent_image': ['e', 0], 'seed': seed,
                          'steps': int(steps), 'cfg': float(cfg),
                          'sampler_name': FIRST_SAMPLER, 'scheduler': FIRST_SCHED,
                          'denoise': 1.0}}
    g['d1'] = {'class_type': 'VAEDecode', 'inputs': {'samples': ['k1', 0], 'vae': ['v', 0]}}
    g['up'] = {'class_type': 'ImageScaleBy',
               'inputs': {'image': ['d1', 0], 'upscale_method': UPSCALE_METHOD,
                          'scale_by': float(factor)}}
    g['en'] = {'class_type': 'VAEEncode', 'inputs': {'pixels': ['up', 0], 'vae': ['v', 0]}}
    g['k2'] = {'class_type': 'KSampler',
               'inputs': {'model': model, 'positive': ['p', 0], 'negative': ['n', 0],
                          'latent_image': ['en', 0], 'seed': seed2,
                          'steps': int(hires_steps), 'cfg': float(cfg),
                          'sampler_name': FIRST_SAMPLER, 'scheduler': FIRST_SCHED,
                          'denoise': float(hires_denoise)}}
    g['d2'] = {'class_type': 'VAEDecode', 'inputs': {'samples': ['k2', 0], 'vae': ['v', 0]}}
    # 节点 id 必须叫 isave：Toybox 的 server.py 里写死了
    # N_IMG_SAVE = "isave"，它只会去 entry['outputs']['isave'] 找成品。
    # 用别的名字 → ComfyUI 明明跑完了，网页却一直显示 'isave' 这个原始键名。
    g['isave'] = {'class_type': 'SaveImage',
                  'inputs': {'images': ['d2', 0], 'filename_prefix': prefix}}
    return g


def sizes(width, height, factor=UPSCALE_BY):
    """给前端/日志用：第一采和第二采的实际像素尺寸。"""
    w, h = round32(width), round32(height)
    w2, h2 = upscaled(w, h, factor)
    return {'first': (w, h), 'second': (w2, h2)}