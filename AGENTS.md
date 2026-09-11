# Prompt 素材库规范

## 目录与所有权

- 一条 Prompt 是一个独立记录，唯一位置为 `prompts/<id>/`。
- 每个记录目录必须包含 `index.md` 与 `preview.webp`；图片和 Prompt 不得分散存放。
- `README.md` 是生成的索引。只运行 `python tools/add_prompt.py rebuild` 更新，禁止手工编辑。
- `static/` 不再用于新增素材。现有的旧图片仅作为历史迁移来源，不得被新条目或 README 引用；如需保留原图，请放在仓库外的个人归档。

## `index.md` 格式

文件以 YAML Front Matter 开头，字段均为必填：

```yaml
---
id: whiteboard-infographic
title: "白板手绘知识图解"
category: "信息图"
tags:
  - "手绘"
  - "白板"
model: "ChatGPT Image"
aspect_ratio: "16:9"
created_at: 2026-09-11
---
```

- `id` 必须和目录名一致，只能由字母、数字和连字符组成；优先使用简短、语义明确的 ASCII kebab-case。中文标题无法自然转写时，使用工具生成的 ID，或显式传入 `--id`。
- `title`、`category`、`model` 使用展示名称；`category` 使用一个稳定的主分类，`tags` 为去重后的细粒度标签。
- `aspect_ratio` 用 `16:9`、`1:1`、`9:16` 等 `宽:高` 写法；新增时由工具根据图片自动识别，也可以通过 `--ratio` 覆盖。
- `created_at` 是首次收录日期，格式固定为 `YYYY-MM-DD`，以 `Asia/Shanghai` 为准，迁移和编辑时不得随意刷新。
- 正文必须保留 `# <title>`、`![预览图](preview.webp)`、`## Prompt` 下的 `text` 代码块，以及可选的 `## 说明`。Prompt 原样保存，不把它压进 Markdown 表格或转义为 HTML。

## 新增流程

推荐流程：复制 Prompt 到剪贴板，保存图片，再执行：

```powershell
python tools/add_prompt.py add C:\\path\\to\\image.png
```

工具会交互收集元数据、读取剪贴板、压缩图片为最大边 1600px、质量 85 的 WebP、写入 `index.md`，并重建 README。可使用 `--prompt-file` 或 `--prompt-stdin` 替代剪贴板；完整参数见 `--help`。

提交前运行：

```powershell
python tools/add_prompt.py rebuild
python tools/add_prompt.py list
```

也可以启动仅本机可访问的录入页面：

```powershell
python tools/add_prompt.py serve
```

在浏览器打开命令输出的地址，粘贴或上传图片、粘贴 Prompt 并填写元数据即可创建记录。

根目录 `metadata.json` 是分类的权威来源。服务启动时读取它；页面可选择已有分类，或输入新分类。首次成功使用的新分类会自动写回该文件。

网页的 Prompt 可以以 YAML Front Matter 开头。若含有 `id`、`title`、`category`、`tags`、`model`、`ratio`（或 `aspect_ratio`）和 `created_at`，服务会优先解析这些字段并从保存的 Prompt 正文中移除该元数据块；页面字段只用于补全缺失字段。
