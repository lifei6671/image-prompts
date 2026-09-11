#!/usr/bin/env python3
"""Create and index self-contained image-prompt records."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from math import gcd
from pathlib import Path
from typing import Any

try:
    from PIL import Image, ImageOps
except ImportError:  # pragma: no cover - exercised by users without dependencies
    Image = ImageOps = None


ROOT = Path(__file__).resolve().parents[1]
PROMPTS_DIR = ROOT / "prompts"
README_PATH = ROOT / "README.md"
MAX_IMAGE_EDGE = 1600
WEBP_QUALITY = 85
ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]*$")
RATIO_PATTERN = re.compile(r"^[1-9]\d*:[1-9]\d*$")


def shanghai_today() -> str:
    # China Standard Time is UTC+8 and has no daylight-saving-time transition.
    return datetime.now(timezone(timedelta(hours=8), "Asia/Shanghai")).date().isoformat()


def require_text(value: str | None, label: str) -> str:
    if value and value.strip():
        return value.strip()
    if not sys.stdin.isatty():
        raise ValueError(f"缺少 {label}；请通过参数提供。")
    entered = input(f"{label}：").strip()
    if not entered:
        raise ValueError(f"{label} 不能为空。")
    return entered


def optional_text(value: str | None, label: str, default: str = "未注明") -> str:
    if value is not None:
        return value.strip() or default
    if not sys.stdin.isatty():
        return default
    return input(f"{label}（留空为“{default}”）：").strip() or default


def normalize_tags(value: str | None) -> list[str]:
    raw = require_text(value, "标签（以逗号分隔）")
    tags: list[str] = []
    for tag in raw.replace("，", ",").split(","):
        tag = tag.strip()
        if tag and tag not in tags:
            tags.append(tag)
    if not tags:
        raise ValueError("至少需要一个标签。")
    return tags


def generated_id(title: str) -> str:
    words = re.findall(r"[a-z0-9]+", title.casefold())
    if words:
        return "-".join(words)
    digest = hashlib.sha1(title.encode("utf-8")).hexdigest()[:8]
    return f"prompt-{shanghai_today().replace('-', '')}-{digest}"


def validate_id(record_id: str) -> str:
    if not ID_PATTERN.fullmatch(record_id):
        raise ValueError("id 只能包含小写字母、数字和连字符，且不能以连字符开头。")
    return record_id


def read_clipboard() -> str:
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command", "Get-Clipboard -Raw"],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
    except (FileNotFoundError, subprocess.CalledProcessError) as error:
        raise ValueError("无法读取剪贴板；请使用 --prompt-file 或 --prompt-stdin。") from error
    return result.stdout


def read_prompt(args: argparse.Namespace) -> str:
    if args.prompt_file:
        prompt = args.prompt_file.read_text(encoding="utf-8")
    elif args.prompt_stdin:
        prompt = sys.stdin.read()
    else:
        prompt = read_clipboard()
    if not prompt.strip():
        raise ValueError("Prompt 不能为空。")
    return prompt


def detect_ratio(width: int, height: int) -> str:
    divisor = gcd(width, height)
    reduced_width, reduced_height = width // divisor, height // divisor
    common_ratios = ((1, 1), (16, 9), (9, 16), (4, 3), (3, 4), (3, 2), (2, 3), (21, 9), (9, 21))
    ratio = width / height
    for candidate_width, candidate_height in common_ratios:
        if abs(ratio - candidate_width / candidate_height) < 0.02:
            return f"{candidate_width}:{candidate_height}"
    return f"{reduced_width}:{reduced_height}"


def validate_ratio(value: str) -> str:
    if not RATIO_PATTERN.fullmatch(value):
        raise ValueError("aspect_ratio 必须是正整数宽高比，例如 16:9。")
    return value


def convert_image(source: Path, destination: Path) -> str:
    if Image is None:
        raise ValueError("缺少 Pillow；请先运行 `python -m pip install -r requirements.txt`。")
    if not source.is_file():
        raise ValueError(f"图片文件不存在：{source}")
    try:
        with Image.open(source) as opened:
            image = ImageOps.exif_transpose(opened)
            ratio = detect_ratio(*image.size)
            image.thumbnail((MAX_IMAGE_EDGE, MAX_IMAGE_EDGE), Image.Resampling.LANCZOS)
            if image.mode not in {"RGB", "RGBA"}:
                image = image.convert("RGBA" if "transparency" in image.info else "RGB")
            image.save(destination, "WEBP", quality=WEBP_QUALITY, method=6)
    except OSError as error:
        raise ValueError(f"无法处理图片：{source}") from error
    return ratio


def quote(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def code_fence(prompt: str) -> str:
    longest_run = max((len(run) for run in re.findall(r"`+", prompt)), default=0)
    return "`" * max(3, longest_run + 1)


def parse_scalar(value: str) -> str:
    value = value.strip()
    if not value:
        raise ValueError("元数据值不能为空。")
    if value.startswith('"'):
        parsed = json.loads(value)
        if not isinstance(parsed, str):
            raise ValueError("元数据字符串必须使用双引号。")
        return parsed
    return value


def write_record(path: Path, metadata: dict[str, Any], prompt: str) -> None:
    fence = code_fence(prompt)
    lines = ["---"]
    for key in ("id", "title", "category"):
        lines.append(f"{key}: {quote(metadata[key])}")
    lines.append("tags:")
    lines.extend(f"  - {quote(tag)}" for tag in metadata["tags"])
    for key in ("model", "aspect_ratio"):
        lines.append(f"{key}: {quote(metadata[key])}")
    lines.extend((f"created_at: {metadata['created_at']}", "---", "", f"# {metadata['title']}", "", "![预览图](preview.webp)", "", "## Prompt", "", f"{fence}text"))
    document = "\n".join(lines) + "\n" + prompt
    if not prompt.endswith("\n"):
        document += "\n"
    path.write_text(document + fence + "\n", encoding="utf-8")


def parse_metadata(path: Path) -> dict[str, Any]:
    lines = path.read_text(encoding="utf-8").splitlines()
    if not lines or lines[0] != "---":
        raise ValueError(f"缺少 YAML Front Matter：{path.relative_to(ROOT)}")
    try:
        closing = lines.index("---", 1)
    except ValueError as error:
        raise ValueError(f"YAML Front Matter 未闭合：{path.relative_to(ROOT)}") from error
    metadata: dict[str, Any] = {"tags": []}
    current_key: str | None = None
    for line in lines[1:closing]:
        if line.startswith("  - ") and current_key == "tags":
            metadata["tags"].append(parse_scalar(line[4:]))
        elif line.endswith(":"):
            current_key = line[:-1]
        elif ": " in line:
            key, value = line.split(": ", 1)
            current_key = key
            metadata[key] = parse_scalar(value)
        else:
            raise ValueError(f"无法读取元数据行：{path.relative_to(ROOT)}: {line}")
    required = {"id", "title", "category", "tags", "model", "aspect_ratio", "created_at"}
    missing = required.difference(metadata)
    if missing or not metadata["tags"]:
        raise ValueError(f"元数据不完整：{path.relative_to(ROOT)}（缺少 {', '.join(sorted(missing)) or 'tags'}）")
    if any(not isinstance(metadata[key], str) or not metadata[key].strip() for key in required - {"tags"}):
        raise ValueError(f"元数据字段不能为空：{path.relative_to(ROOT)}")
    if any(not isinstance(tag, str) or not tag.strip() for tag in metadata["tags"]) or len(set(metadata["tags"])) != len(metadata["tags"]):
        raise ValueError(f"tags 必须是非空且不重复的字符串：{path.relative_to(ROOT)}")
    directory_id = path.parent.name
    if metadata["id"] != directory_id or not ID_PATTERN.fullmatch(metadata["id"]):
        raise ValueError(f"id 必须为合法目录名：{path.relative_to(ROOT)}")
    validate_ratio(metadata["aspect_ratio"])
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", metadata["created_at"]):
        raise ValueError(f"created_at 必须为 YYYY-MM-DD：{path.relative_to(ROOT)}")
    try:
        date.fromisoformat(metadata["created_at"])
    except ValueError as error:
        raise ValueError(f"created_at 不是有效日期：{path.relative_to(ROOT)}") from error
    if not (path.parent / "preview.webp").is_file():
        raise ValueError(f"缺少 preview.webp：{path.relative_to(ROOT)}")
    return metadata


def records() -> list[tuple[Path, dict[str, Any]]]:
    if not PROMPTS_DIR.exists():
        return []
    found: list[tuple[Path, dict[str, Any]]] = []
    for index_path in sorted(PROMPTS_DIR.glob("*/index.md")):
        found.append((index_path, parse_metadata(index_path)))
    return found


def rebuild() -> None:
    grouped: dict[str, list[tuple[Path, dict[str, Any]]]] = defaultdict(list)
    for path, metadata in records():
        grouped[metadata["category"]].append((path, metadata))
    lines = ["# Image Prompts", "", "> 此文件由 `python tools/add_prompt.py rebuild` 自动生成，请勿手工编辑。", ""]
    if not grouped:
        lines.append("暂无素材。")
    for category in sorted(grouped, key=str.casefold):
        lines.extend((f"## {category}", ""))
        for path, metadata in sorted(grouped[category], key=lambda item: item[1]["title"].casefold()):
            record_dir = path.parent.relative_to(ROOT).as_posix()
            tags = " / ".join(metadata["tags"])
            lines.extend((f"### {metadata['title']}", "", f"[查看 Prompt]({record_dir}/index.md) · {tags} · {metadata['model']} · {metadata['aspect_ratio']}", "", f'<img src="{record_dir}/preview.webp" alt="{metadata["title"]}" width="360">', ""))
    README_PATH.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    print(f"已重建 README，共 {sum(len(entries) for entries in grouped.values())} 条记录。")


def add(args: argparse.Namespace) -> None:
    title = require_text(args.title, "标题")
    record_id = validate_id(args.record_id or generated_id(title))
    category = require_text(args.category, "分类")
    metadata: dict[str, Any] = {
        "id": record_id,
        "title": title,
        "category": category,
        "tags": normalize_tags(args.tags),
        "model": optional_text(args.model, "模型"),
        "aspect_ratio": validate_ratio(args.ratio) if args.ratio else "",
        "created_at": shanghai_today(),
    }
    destination_dir = PROMPTS_DIR / record_id
    if destination_dir.exists():
        raise ValueError(f"记录已存在：{destination_dir.relative_to(ROOT)}")
    prompt = read_prompt(args)
    destination_dir.mkdir(parents=True)
    try:
        detected_ratio = convert_image(args.image, destination_dir / "preview.webp")
        metadata["aspect_ratio"] = metadata["aspect_ratio"] or validate_ratio(detected_ratio)
        write_record(destination_dir / "index.md", metadata, prompt)
        rebuild()
    except Exception:
        shutil.rmtree(destination_dir)
        raise
    print(f"已添加：{destination_dir.relative_to(ROOT)}")


def list_records() -> None:
    for path, metadata in records():
        print(f"{metadata['id']}\t{metadata['title']}\t{metadata['category']}\t{path.parent.relative_to(ROOT)}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    add_parser = commands.add_parser("add", help="新增一条 Prompt 记录")
    add_parser.add_argument("image", type=Path, help="源图片路径")
    add_parser.add_argument("--id", dest="record_id", help="目录 ID；默认由标题生成")
    add_parser.add_argument("--title", help="标题")
    add_parser.add_argument("--category", help="主分类")
    add_parser.add_argument("--tags", help="逗号分隔的标签")
    add_parser.add_argument("--model", help="生成模型")
    add_parser.add_argument("--ratio", help="宽高比，例如 16:9；默认自动识别")
    prompt_source = add_parser.add_mutually_exclusive_group()
    prompt_source.add_argument("--prompt-file", type=Path, help="包含 Prompt 的 UTF-8 文本文件")
    prompt_source.add_argument("--prompt-stdin", action="store_true", help="从标准输入读取 Prompt")
    commands.add_parser("rebuild", help="从全部元数据重建 README")
    commands.add_parser("list", help="列出全部记录")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        if args.command == "add":
            add(args)
        elif args.command == "rebuild":
            rebuild()
        else:
            list_records()
    except ValueError as error:
        print(f"错误：{error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
