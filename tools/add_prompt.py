#!/usr/bin/env python3
"""Create and index self-contained image-prompt records."""

from __future__ import annotations

import argparse
import base64
import hashlib
import html
import json
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import date, datetime, timedelta, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, HTTPServer
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
METADATA_PATH = ROOT / "metadata.json"
MAX_IMAGE_EDGE = 1600
WEBP_QUALITY = 85
MAX_UPLOAD_BYTES = 20 * 1024 * 1024
ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]*$")
RATIO_PATTERN = re.compile(r"^[1-9]\d*:[1-9]\d*$")
CUSTOM_RATIO = "自定义"
ADAPTIVE_RATIO = "自适应"
DATA_URL_PATTERN = re.compile(r"^data:image/([a-zA-Z0-9.+-]+);base64,(.*)$", re.DOTALL)


def shanghai_today() -> str:
    # China Standard Time is UTC+8 and has no daylight-saving-time transition.
    return datetime.now(timezone(timedelta(hours=8), "Asia/Shanghai")).date().isoformat()


def load_categories() -> list[str]:
    try:
        data = json.loads(METADATA_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    except json.JSONDecodeError as error:
        raise ValueError("metadata.json 不是有效 JSON。") from error
    categories = data.get("categories") if isinstance(data, dict) else None
    if not isinstance(categories, list) or any(not isinstance(item, str) or not item.strip() for item in categories):
        raise ValueError("metadata.json 的 categories 必须是非空字符串数组。")
    return list(dict.fromkeys(item.strip() for item in categories))


def save_categories(categories: list[str]) -> None:
    METADATA_PATH.write_text(
        json.dumps({"categories": sorted(categories, key=str.casefold)}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def ensure_category(category: str) -> list[str]:
    categories = load_categories()
    if category not in categories:
        categories.append(category)
        save_categories(categories)
    return categories


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
    return parse_tags(raw)


def parse_tags(raw: str) -> list[str]:
    tags: list[str] = []
    for tag in raw.replace("，", ",").split(","):
        tag = tag.strip()
        if tag and tag not in tags:
            tags.append(tag)
    if not tags:
        raise ValueError("至少需要一个标签。")
    return tags


def required_value(value: str, label: str) -> str:
    value = value.strip()
    if not value:
        raise ValueError(f"{label} 不能为空。")
    return value


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
    if value not in {CUSTOM_RATIO, ADAPTIVE_RATIO} and not RATIO_PATTERN.fullmatch(value):
        raise ValueError("aspect_ratio 必须是正整数宽高比（例如 16:9）、“自定义”或“自适应”。")
    return value


def validate_created_at(value: str) -> str:
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ValueError("created_at 必须为 YYYY-MM-DD。")
    try:
        date.fromisoformat(value)
    except ValueError as error:
        raise ValueError("created_at 不是有效日期。") from error
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


def extract_pasted_metadata(prompt: str) -> tuple[dict[str, Any], str]:
    lines = prompt.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        return {}, prompt
    closing = next((index for index, line in enumerate(lines[1:], 1) if line.strip() == "---"), None)
    if closing is None:
        return {}, prompt
    metadata: dict[str, Any] = {"tags": []}
    current_key = ""
    for raw_line in lines[1:closing]:
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("- ") and current_key == "tags":
            tag = parse_scalar(line[2:])
            if tag not in metadata["tags"]:
                metadata["tags"].append(tag)
            continue
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        key = key.strip().replace("\\_", "_")
        current_key = key
        if key == "tags":
            continue
        if key in {"id", "title", "category", "model", "ratio", "aspect_ratio", "created_at"}:
            metadata["aspect_ratio" if key == "ratio" else key] = parse_scalar(value)
    if not metadata["tags"]:
        metadata.pop("tags")
    return metadata, "".join(lines[closing + 1:]).lstrip("\r\n")


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


def insert_preview_after_first_heading(document: str) -> str:
    lines = document.splitlines(keepends=True)
    fence: tuple[str, int] | None = None
    for index, line in enumerate(lines):
        fence_match = re.match(r"^ {0,3}(`{3,}|~{3,})", line)
        if fence_match:
            marker = fence_match.group(1)
            if fence is None:
                fence = (marker[0], len(marker))
            elif marker[0] == fence[0] and len(marker) >= fence[1]:
                fence = None
            continue
        if fence is not None:
            continue
        if re.match(r"^ {0,3}#(?!#)[ \t]+\S", line):
            if not line.endswith(("\n", "\r")):
                lines[index] += "\n"
            lines.insert(index + 1, "![预览图](preview.webp)\n")
            return "".join(lines)
    raise ValueError("Prompt 必须包含一级标题（# 标题）。")


def write_uploaded_record(path: Path, metadata: dict[str, Any], document: str) -> None:
    lines = ["---"]
    for key in ("id", "title", "category"):
        lines.append(f"{key}: {quote(metadata[key])}")
    lines.append("tags:")
    lines.extend(f"  - {quote(tag)}" for tag in metadata["tags"])
    for key in ("model", "aspect_ratio"):
        lines.append(f"{key}: {quote(metadata[key])}")
    lines.extend((f"created_at: {metadata['created_at']}", "---", ""))
    path.write_text("\n".join(lines) + insert_preview_after_first_heading(document), encoding="utf-8")


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
    entries = sorted(records(), key=lambda item: (item[1]["title"].casefold(), item[1]["id"]))
    entries.sort(key=lambda item: item[1]["created_at"], reverse=True)
    lines = [
        "# Image Prompts", "",
        f"共 {len(entries)} 条 Prompt · 点击图片或标题查看完整 Prompt。", "",
        "> 此文件由 `python tools/add_prompt.py rebuild` 自动生成；命令行或网页新增 Prompt 后会自动更新。", "",
    ]
    if not entries:
        lines.append("暂无素材。")
    else:
        # One cell per column lets images of different heights stack independently.
        # GitHub strips CSS, so keep the gallery in plain HTML with no blank lines.
        lines.extend(("<table>", "<tr>"))
        for column in range(2):
            lines.append('<td width="50%" valign="top">')
            for path, metadata in entries[column::2]:
                record_dir = html.escape(path.parent.relative_to(ROOT).as_posix(), quote=True)
                title = html.escape(metadata["title"], quote=True)
                details = html.escape(f'{metadata["category"]} · {metadata["model"]} · {metadata["aspect_ratio"]}')
                tags = html.escape(" / ".join(metadata["tags"]))
                lines.extend((
                    f'<p><a href="{record_dir}/index.md"><img src="{record_dir}/preview.webp" alt="{title}" width="100%"></a></p>',
                    f'<p><a href="{record_dir}/index.md"><strong>{title}</strong></a><br>',
                    f'<sub>{details}</sub><br><sub>{tags}</sub></p>',
                    "<hr>",
                ))
            lines.append("</td>")
        lines.extend(("</tr>", "</table>"))
    README_PATH.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    print(f"已重建 README，共 {len(entries)} 条记录。")


def create_record(image: Path, metadata: dict[str, Any], prompt: str, uploaded_document: str | None = None) -> Path:
    record_id = validate_id(metadata["id"])
    destination_dir = PROMPTS_DIR / record_id
    if destination_dir.exists():
        raise ValueError(f"记录已存在：{destination_dir.relative_to(ROOT)}")
    metadata_before = METADATA_PATH.read_bytes() if METADATA_PATH.is_file() else None
    readme_before = README_PATH.read_bytes() if README_PATH.is_file() else None
    destination_dir.mkdir(parents=True)
    try:
        detected_ratio = convert_image(image, destination_dir / "preview.webp")
        metadata["aspect_ratio"] = metadata["aspect_ratio"] or validate_ratio(detected_ratio)
        if uploaded_document is None:
            write_record(destination_dir / "index.md", metadata, prompt)
        else:
            write_uploaded_record(destination_dir / "index.md", metadata, uploaded_document)
        ensure_category(metadata["category"])
        rebuild()
    except Exception:
        shutil.rmtree(destination_dir)
        for path, previous in ((METADATA_PATH, metadata_before), (README_PATH, readme_before)):
            if previous is None:
                path.unlink(missing_ok=True)
            else:
                path.write_bytes(previous)
        raise
    return destination_dir


def add(args: argparse.Namespace) -> None:
    title = require_text(args.title, "标题")
    metadata: dict[str, Any] = {
        "id": validate_id(args.record_id or generated_id(title)),
        "title": title,
        "category": require_text(args.category, "分类"),
        "tags": normalize_tags(args.tags),
        "model": optional_text(args.model, "模型"),
        "aspect_ratio": validate_ratio(args.ratio) if args.ratio else "",
        "created_at": shanghai_today(),
    }
    destination_dir = create_record(args.image, metadata, read_prompt(args))
    print(f"已添加：{destination_dir.relative_to(ROOT)}")


def list_records() -> None:
    for path, metadata in records():
        print(f"{metadata['id']}\t{metadata['title']}\t{metadata['category']}\t{path.parent.relative_to(ROOT)}")


def web_page(categories: list[str]) -> bytes:
    options = "".join(f'<option value="{html.escape(category, quote=True)}"></option>' for category in categories)
    page = '''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Prompt 素材库</title><style>
body{max-width:760px;margin:40px auto;padding:0 20px;background:#f7f4ee;color:#252525;font:16px system-ui,sans-serif}h1{margin-bottom:6px}form{display:grid;gap:16px;background:#fff;padding:24px;border-radius:14px;box-shadow:0 8px 24px #0001}label{display:grid;gap:6px;font-weight:600}input,textarea,button{box-sizing:border-box;font:inherit;padding:10px;border:1px solid #c9c5bb;border-radius:8px}textarea{min-height:260px;resize:vertical}button{background:#1c5b75;color:#fff;border:0;cursor:pointer;font-weight:700}.drop{padding:28px;border:2px dashed #9ea69a;text-align:center;border-radius:10px;color:#555}.drop.drag{background:#eef6f4}.hint,#result{color:#666;font-size:14px}#result.ok{color:#17653b}#result.error{color:#a32020}</style>
<h1>新增 Prompt</h1><p class="hint">粘贴图片、拖入图片或选择文件；图片只在本机处理。Prompt 顶部的 YAML 元数据会自动解析并优先使用，正文会保留原样，只在首个一级标题后插入预览图。</p><form id="form"><div id="drop" class="drop" tabindex="0">点击选择图片，或直接粘贴 / 拖入图片<br><span id="image-name" class="hint">尚未选择</span><input id="image" type="file" accept="image/*" hidden></div><label>标题<input name="title"></label><label>分类<input name="category" list="categories" placeholder="选择或输入新分类"><datalist id="categories">__CATEGORY_OPTIONS__</datalist><span class="hint">可选择已有分类，也可直接输入新分类。</span></label><label>标签（逗号分隔）<input name="tags"></label><label>模型<input name="model" value="未注明"></label><label>比例（留空自动识别）<input name="ratio" placeholder="16:9、自定义或自适应"></label><label>Prompt<textarea name="prompt" required></textarea></label><button>创建记录</button><div id="result" aria-live="polite"></div></form><script>
const form=document.querySelector('#form'),drop=document.querySelector('#drop'),fileInput=document.querySelector('#image'),name=document.querySelector('#image-name'),result=document.querySelector('#result');let imageFile;
function setImage(file){if(!file||!file.type.startsWith('image/'))return;imageFile=file;name.textContent=`已选择：${file.name||'剪贴板图片'}`}
drop.onclick=()=>fileInput.click();fileInput.onchange=()=>setImage(fileInput.files[0]);drop.ondragover=e=>{e.preventDefault();drop.classList.add('drag')};drop.ondragleave=()=>drop.classList.remove('drag');drop.ondrop=e=>{e.preventDefault();drop.classList.remove('drag');setImage(e.dataTransfer.files[0])};document.addEventListener('paste',e=>{for(const item of e.clipboardData.items)if(item.type.startsWith('image/')){setImage(item.getAsFile());break}});
form.onsubmit=async e=>{e.preventDefault();if(!imageFile){result.className='error';result.textContent='请选择或粘贴一张图片。';return}const reader=new FileReader();reader.onload=async()=>{const data=Object.fromEntries(new FormData(form));data.image=reader.result;result.className='';result.textContent='正在创建…';try{const response=await fetch('/api/prompts',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data)});const body=await response.json();if(!response.ok)throw new Error(body.error);if(![...document.querySelector('#categories').options].some(option=>option.value===data.category))document.querySelector('#categories').append(new Option(data.category));result.className='ok';result.textContent=`已创建：${body.path}`;form.reset();imageFile=undefined;name.textContent='尚未选择'}catch(error){result.className='error';result.textContent=error.message}};reader.readAsDataURL(imageFile)};
</script>'''
    return page.replace("__CATEGORY_OPTIONS__", options).encode("utf-8")


class PromptRequestHandler(BaseHTTPRequestHandler):
    def send_json(self, status: HTTPStatus, body: dict[str, str]) -> None:
        encoded = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def do_GET(self) -> None:
        if self.path != "/":
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        page = web_page(self.server.categories)
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(page)))
        self.end_headers()
        self.wfile.write(page)

    def do_POST(self) -> None:
        if self.path != "/api/prompts":
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= MAX_UPLOAD_BYTES * 2:
                raise ValueError("请求体为空或过大。")
            payload = json.loads(self.rfile.read(length))
            if not isinstance(payload, dict):
                raise ValueError("请求必须是 JSON 对象。")
            image_match = DATA_URL_PATTERN.fullmatch(str(payload.get("image", "")))
            if not image_match:
                raise ValueError("请提供 PNG、JPEG、WebP 等图片。")
            image_bytes = base64.b64decode(image_match.group(2), validate=True)
            if not image_bytes or len(image_bytes) > MAX_UPLOAD_BYTES:
                raise ValueError("图片为空或超过 20 MiB。")
            pasted, prompt = extract_pasted_metadata(str(payload.get("prompt", "")))
            title = required_value(str(pasted.get("title", payload.get("title", ""))), "标题")
            tags = pasted.get("tags") or parse_tags(required_value(str(payload.get("tags", "")), "标签"))
            metadata = {
                "id": validate_id(str(pasted.get("id", payload.get("id", ""))).strip() or generated_id(title)),
                "title": title,
                "category": required_value(str(pasted.get("category", payload.get("category", ""))), "分类"),
                "tags": tags,
                "model": optional_text(str(pasted.get("model", payload.get("model", ""))), "模型"),
                "aspect_ratio": validate_ratio(str(pasted.get("aspect_ratio", payload.get("ratio", ""))).strip()) if str(pasted.get("aspect_ratio", payload.get("ratio", ""))).strip() else "",
                "created_at": validate_created_at(str(pasted.get("created_at", shanghai_today()))),
            }
            if not prompt.strip():
                raise ValueError("Prompt 不能为空。")
            with tempfile.TemporaryDirectory() as temporary:
                source = Path(temporary) / f"upload.{image_match.group(1).split('+')[0]}"
                source.write_bytes(image_bytes)
                destination = create_record(source, metadata, prompt, uploaded_document=prompt)
        except (ValueError, json.JSONDecodeError) as error:
            self.send_json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
            return
        self.server.categories = load_categories()
        self.send_json(HTTPStatus.CREATED, {"path": destination.relative_to(ROOT).as_posix()})

    def log_message(self, format: str, *args: Any) -> None:
        print(f"{self.address_string()} - {format % args}")


def serve(args: argparse.Namespace) -> None:
    server = HTTPServer(("127.0.0.1", args.port), PromptRequestHandler)
    server.categories = load_categories()
    print(f"本地素材库已启动：http://127.0.0.1:{server.server_port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n服务已停止。")
    finally:
        server.server_close()


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
    add_parser.add_argument("--ratio", help="宽高比，例如 16:9、“自定义”或“自适应”；默认自动识别")
    prompt_source = add_parser.add_mutually_exclusive_group()
    prompt_source.add_argument("--prompt-file", type=Path, help="包含 Prompt 的 UTF-8 文本文件")
    prompt_source.add_argument("--prompt-stdin", action="store_true", help="从标准输入读取 Prompt")
    commands.add_parser("rebuild", help="从全部元数据重建 README")
    commands.add_parser("list", help="列出全部记录")
    serve_parser = commands.add_parser("serve", help="启动本地录入页面")
    serve_parser.add_argument("--port", default=9965, type=int, help="监听端口，默认 9965")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        if args.command == "add":
            add(args)
        elif args.command == "rebuild":
            rebuild()
        elif args.command == "serve":
            serve(args)
        else:
            list_records()
    except ValueError as error:
        print(f"错误：{error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
