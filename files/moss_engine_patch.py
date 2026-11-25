"""MOSS-Image 引擎补丁：把 HIRES 双段出图接进 Toybox 的「🖼 图片」这条路。

不新增服务、不新增端口、不新增笔记本。只做三件事：

  A 装图构造器    把 moss_graph.py 落到 APP 目录，供 server.py import
  B 加一个分支     image_graph() 在 ph3 / qwen 之外多认一个 engine="moss"
  C 前端可选       /api/config 多回一档 image_engines，设置面板多一个下拉

幂等：文件里已有 MARK 就跳过；锚点找不到只警告不抛异常，Toybox 照常启动。

    python moss_engine_patch.py <path/to/server.py> [--graph-dir <dir>]
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

MARK = 'moss-engine:v1'

# 补丁里每个注入块都用独立标记，幂等判定才不会被「引用标记」误触发。
MARK_HELPERS = MARK + '/helpers'
MARK_IMPORT = MARK + '/import'

# ── 插在 ph3_mode() 之前：模式判断 + 图构造 ──
HELPERS = r'''

# ── __MARK_HELPERS__：MOSS-Image HIRES 双段出图（构建时自动接，别手改）──
MOSS_SIZES = {"0.9K": 896, "1K": 1024, "1.2K": 1152, "1.3K": 1344}

MOSS_DEFAULTS = {
    "unet": "Dirty-Moss.safetensors",
    "clip": "qwen3.5_9b_int8_convrot.safetensors",
    "vae": "flux2-vae.safetensors",
    "lora": "",
    "lora_strength": 1.0,
    "steps": 25,
    "cfg": 2.0,
    "hires_steps": 8,
    "hires_denoise": 0.15,
    "factor": 1.5,
    "shift": 5.0,
}


def moss_mode(cfg):
    return cfg["image"].get("engine", "qwen") == "moss"


def moss_settings(cfg):
    """合并默认值与用户设置。缺键不崩，坏值退回默认。"""
    out = dict(MOSS_DEFAULTS)
    saved = (cfg.get("image") or {}).get("moss") or {}
    if isinstance(saved, dict):
        for key in MOSS_DEFAULTS:
            if key in saved and saved[key] not in (None, ""):
                out[key] = saved[key]
    return out


def moss_dims(aspect, size_key, refs):
    """长边来自尺寸按钮；比例为 auto 时跟随第一张参考图（没有就 3:4）。"""
    long_side = MOSS_SIZES.get(size_key, 1152)
    if aspect == "auto":
        a, b = (refs[0]["width"], refs[0]["height"]) if refs and refs[0].get("width") else (3, 4)
    else:
        a, b = ASPECTS.get(aspect, (3, 4))
    w, h = (long_side, long_side * b / a) if a >= b else (long_side * a / b, long_side)
    # 第一采取 64 的倍数：1.5× 放大后第二采才天然对齐 32（release 选 896×1152 就是这个道理）
    return tuple(moss_graph.round64(v) for v in (w, h))


def moss_graph_build(cfg, prompt, w, h, seed):
    s = moss_settings(cfg)
    token = uuid.uuid4().hex
    graph = moss_graph.graph(
        unet=s["unet"], clip=s["clip"], vae=s["vae"], prompt=prompt,
        width=w, height=h, seed=seed,
        steps=int(s["steps"]), cfg=float(s["cfg"]),
        hires_steps=int(s["hires_steps"]), hires_denoise=float(s["hires_denoise"]),
        factor=float(s["factor"]), lora=s["lora"] or None,
        lora_strength=float(s["lora_strength"]), shift=float(s["shift"]),
        prefix="H3Toybox/img_" + token)
    return graph, token

'''

# ── image_graph 里加分支 ──
DISPATCH = (
    'def image_graph(cfg, prompt, w, h, refs, seed):\n'
    '    if ph3_mode(cfg):\n'
    '        return ph3_graph(cfg, prompt, w, h, refs, seed)',
    'def image_graph(cfg, prompt, w, h, refs, seed):\n'
    '    if moss_mode(cfg):\n'
    '        # MOSS 这一路只做文生图：带参考图时明确报错，不静默退化成纯文字\n'
    '        if refs:\n'
    '            raise ValueError("MOSS 引擎只支持文生图，请先清空参考图，或在设置里换回别的生图引擎。")\n'
    '        graph, _token = moss_graph_build(cfg, prompt, w, h, seed)\n'
    '        return graph\n'
    '    if ph3_mode(cfg):\n'
    '        return ph3_graph(cfg, prompt, w, h, refs, seed)',
)

# ── 尺寸计算：MOSS 走自己的表 ──
DIMS = (
    '    w, h = image_dims(req.get("aspect", "auto"), req.get("image_size", "1.3K"), refs)',
    '    if moss_mode(cfg):\n'
    '        w, h = moss_dims(req.get("aspect", "auto"), req.get("image_size", "1.2K"), refs)\n'
    '    else:\n'
    '        w, h = image_dims(req.get("aspect", "auto"), req.get("image_size", "1.3K"), refs)',
)

# ── 任务记录的步数 ──
STEPS = (
    '           "steps": 8 if ph3_mode(cfg) else int(cfg["image"]["steps"])}',
    '           "steps": (moss_settings(cfg)["steps"] if moss_mode(cfg)\n'
    '                     else 8 if ph3_mode(cfg) else int(cfg["image"]["steps"]))}',
)

# ── /api/config 把引擎清单和 MOSS 尺寸表带给前端 ──
CONFIG = (
    '"image_sizes": list(IMAGE_SIZES),',
    '"image_sizes": list(IMAGE_SIZES), "image_engines": list(IMAGE_ENGINES),'
    ' "moss_sizes": list(MOSS_SIZES),',
)

ENGINES = (
    'IMAGE_SIZES = {',
    'IMAGE_ENGINES = {"moss": "MOSS-Image 高画质双段", "ph3": "P-H3 (H3 模型)", "qwen": "Qwen-Image 2.1"}\n'
    'IMAGE_SIZES = {',
)

EDITS = [DISPATCH, DIMS, STEPS, CONFIG, ENGINES]


def apply_edits(source, edits):
    applied, problems = [], []
    for old, new in edits:
        if new.split('\n')[0].strip() in source and old not in source:
            applied.append(old.split('\n')[0].strip()[:40] + '（已应用）')
            continue
        if source.count(old) != 1:
            problems.append('锚点出现 %d 次：%s' % (source.count(old), old.split('\n')[0][:70]))
            continue
        source = source.replace(old, new, 1)
        applied.append(new.split('\n')[0].strip()[:40])
    return source, applied, problems


def main(path, graph_dir=None):
    server = Path(path)
    source = server.read_text(encoding='utf-8')
    if MARK_HELPERS in source and MARK_IMPORT in source:
        print('⏭️ MOSS 引擎补丁已在（%s）' % MARK)
        return True

    # A 把图构造器放到能被 import 的位置
    graph_dir = Path(graph_dir or server.parent)
    target = graph_dir / 'moss_graph.py'
    if not target.is_file():
        print('⚠️ 找不到 %s，MOSS 引擎无法装配' % target)
        return False

    source, applied, problems = apply_edits(source, EDITS)
    if problems:
        for p in problems:
            print('⚠️ ' + p)
        if len(applied) == 0:
            print('⚠️ 一处都没打上，不写盘')
            return False

    # helpers 插在 def ph3_mode 之前，并补上 import
    anchor = 'def ph3_mode(cfg):'
    if anchor not in source:
        print('⚠️ 找不到 ph3_mode 锚点，不写盘')
        return False
    helpers = HELPERS.strip('\n').replace('__MARK_HELPERS__', MARK_HELPERS)
    source = source.replace(anchor, helpers + '\n\n\n' + anchor, 1)
    if MARK_IMPORT not in source:
        old_import = 'import uuid\n'
        if old_import not in source:
            print('⚠️ 找不到 import uuid 锚点，不写盘')
            return False
        source = source.replace(
            old_import, old_import + '\nimport moss_graph  # %s\n' % MARK_IMPORT, 1)
        if MARK_IMPORT not in source:
            print('⚠️ import 没插进去，不写盘')
            return False

    try:
        ast.parse(source)
    except SyntaxError as error:
        print('⚠️ 打补丁后语法错误，不写盘：%s' % error)
        return False

    server.write_text(source, encoding='utf-8')
    print('🖼 MOSS 引擎已接入（%s）：%d 处改动' % (MARK, len(applied) + 2))
    for a in applied:
        print('   · ' + a)
    return True


if __name__ == '__main__':
    args = sys.argv[1:]
    graph_dir = None
    if '--graph-dir' in args:
        index = args.index('--graph-dir')
        graph_dir = args[index + 1]
        args = args[:index] + args[index + 2:]
    if not args:
        print('用法：python moss_engine_patch.py <path/to/server.py> [--graph-dir DIR]')
        raise SystemExit(2)
    raise SystemExit(0 if main(args[0], graph_dir) else 1)