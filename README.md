# h3-toybox-patch

H3 Toybox 的界面/功能补丁包。原本这些文件以 gzip+base64 塞在 Colab 笔记本 ③ 格里
（占 86,045 字符 / 78%），改成放这里，笔记本运行时下载。

## 使用方法

笔记本 ③ 格里有这一段，改 `PATCH_URL` 指向本仓库即可：

```python
PATCH_URL = 'https://github.com/YOURNAME/h3-toybox-patch/archive/refs/heads/main.zip'
```

首次运行会下载并解压到 `/content/h3patch/files/`；同一 runtime 内再次运行直接复用。

## files/ 内容

| 文件 | 字节 | 作用 |
|---|---|---|
| `authproxy.py` | 12,367 | 口令门禁代理（8191 → 8190），网址外流也进不来 |
| `style.css` | 34,208 | 手机适配样式（上游只有桌面版） |
| `index.html` | 12,094 | 简体中文前端 |
| `app.js` | 54,625 | 简体中文前端逻辑 |
| `zh_cn_backend.json` | 8,989 | 后端消息简繁对照表 |
| `archive_media.py` | 25,843 | 成品归档（云盘去重、断点续跑） |
| `media_edits.json` | 10,951 | 后端补丁锚点（4 组，按版本递进） |
| `rewrite_guard.py` | 10,012 | 改写器防拒补丁 |
| `moss_engine_patch.py` | 8,165 | 把 MOSS 生图接进 Toybox 后端 |
| `moss_graph.py` | 6,846 | MOSS HIRES 双段工作流构造器 |

合计 184,100 字节。

## 注意

- `media_edits.json` 的锚点对应上游 `dseditor/minimax-h3-colab` 的 commit
  `44708d4aa17e30fc8b56be375387ba2106936a9c`。上游改了这个文件要跟着改。
- `moss_engine_patch.py` 是幂等的：已打过 `moss-engine:v1/helpers` 标记就跳过。
- 改这里的文件不需要重新发笔记本。