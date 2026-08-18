#!/usr/bin/env python3
"""为 Cursor Glass UI / Settings 界面打中文补丁。

会修改 Cursor 安装目录内的 JS，并同步更新 product.json 中的 SHA256 校验和，
避免触发 “Installation has been modified on disk”。
支持 macOS / Windows / Linux。
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path

from paths import find_cursor_app_root, is_windows, platform_display_name, quit_hint

ROOT = Path(__file__).resolve().parent.parent
DATA_FILE = ROOT / "data/glass-ui-replacements.json"
STRUCTURED_FILE = ROOT / "data/structured-patches.json"
BACKUP_DIR = ROOT / "backups"


def log(msg: str = "") -> None:
    print(msg, flush=True)


def load_config() -> dict:
    with DATA_FILE.open(encoding="utf-8") as f:
        return json.load(f)


def load_structured() -> dict | None:
    if not STRUCTURED_FILE.exists():
        return None
    with STRUCTURED_FILE.open(encoding="utf-8") as f:
        return json.load(f)


def sha256_checksum(data: bytes) -> str:
    return base64.b64encode(hashlib.sha256(data).digest()).decode("ascii").rstrip("=")


def backup_file(source: Path) -> Path:
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_path = BACKUP_DIR / f"{source.name}.{stamp}.bak"
    shutil.copy2(source, backup_path)
    return backup_path


def find_latest_backup(filename: str) -> Path | None:
    candidates = sorted(BACKUP_DIR.glob(f"{filename}.*.bak"), reverse=True)
    return candidates[0] if candidates else None


def ensure_writable(app_root: Path) -> None:
    """提前检查是否有写权限，避免读完大文件后才失败。"""
    probe = app_root / f".cursor-i18n-write-test-{os.getpid()}"
    try:
        probe.write_text("ok", encoding="utf-8")
        probe.unlink(missing_ok=True)
    except OSError as exc:
        hint = ""
        if is_windows() and "Program Files" in str(app_root):
            hint = (
                "\n提示: Cursor 安装在 Program Files，请以管理员身份重新打开 PowerShell，"
                "再运行 install-win.ps1。"
            )
        raise PermissionError(
            f"无法写入 Cursor 安装目录: {app_root}\n原因: {exc}{hint}"
        ) from exc


# 短词裸替换极易误伤邻近英文（如 Accept → Accepted 变成 接受ed）
MIN_UNQUOTED_EXACT_LEN = 16

# label:"Theme" 这类条目本身已带代码上下文，不能再外包一层引号
_CONTEXTUAL_SRC = re.compile(
    r"^(?:label|title|description|name|placeholder|original|actionTitle|"
    r"fallback|collectionLabel|children|aria-label)\s*:"
)


def replace_exact_string(content: str, src: str, dst: str) -> tuple[str, int]:
    """优先替换引号包裹的 UI 文案；短词禁止裸替换以防污染标识符/其它词。"""
    if _CONTEXTUAL_SRC.search(src) and ("'" in src or '"' in src):
        n = content.count(src)
        if n:
            return content.replace(src, dst), n
        return content, 0

    hits = 0
    for quote in ('"', "'"):
        needle = f"{quote}{src}{quote}"
        n = content.count(needle)
        if not n:
            continue
        content = content.replace(needle, f"{quote}{dst}{quote}")
        hits += n
    if hits:
        return content, hits
    # 长句才允许裸替换（覆盖 HTML 片段内嵌文案等）
    if len(src) >= MIN_UNQUOTED_EXACT_LEN and src in content:
        content = content.replace(src, dst)
        return content, 1
    return content, 0


def _quoted_target_coverage(content: str, values: dict) -> int:
    """统计已以引号形式存在的目标译文（含 exact 先替换的情况）。"""
    ok = 0
    for mapping in values.values():
        target = mapping.get("target")
        if not target:
            continue
        if f'"{target}"' in content or f"'{target}'" in content:
            ok += 1
    return ok

def apply_replacements(content: str, config: dict, progress_label: str = "") -> tuple[str, dict]:
    stats = {
        "blocks": 0,
        "exact": 0,
        "structured": 0,
        "missed_blocks": [],
        "missed_exact": [],
        "missed_structured": [],
    }

    for block in config.get("blocks", []):
        src = block["from"]
        dst = block["to"]
        optional = block.get("optional", False)
        if src in content:
            content = content.replace(src, dst)
            stats["blocks"] += 1
        elif not optional:
            stats["missed_blocks"].append(block.get("id", src[:40]))

    exact_items = sorted(config.get("exact", []), key=lambda x: len(x["from"]), reverse=True)
    total = len(exact_items)
    report_every = max(50, total // 10) if total else 1
    started = time.time()

    for i, item in enumerate(exact_items, start=1):
        src = item["from"]
        dst = item["to"]
        content, n = replace_exact_string(content, src, dst)
        if n:
            stats["exact"] += 1
        else:
            stats["missed_exact"].append(src)

        if progress_label and (i % report_every == 0 or i == total):
            elapsed = time.time() - started
            log(f"    {progress_label}: 精确替换进度 {i}/{total} ({elapsed:.1f}s)")

    return content, stats


def _key_present(content: str, key: str) -> bool:
    return re.search(rf'(?:{re.escape(key)}|"{re.escape(key)}")\s*:', content) is not None


def _replace_object_value(content: str, key: str, source: str, target: str) -> tuple[str, int]:
    patterns = [
        rf'({re.escape(key)}\s*:\s*)"{re.escape(source)}"',
        rf'("{re.escape(key)}"\s*:\s*)"{re.escape(source)}"',
    ]
    for pat in patterns:
        new_content, n = re.subn(pat, rf'\1"{target}"', content, count=1)
        if n:
            return new_content, n
    return content, 0


def _value_already_translated(content: str, key: str, target: str) -> bool:
    patterns = [
        rf'{re.escape(key)}\s*:\s*"{re.escape(target)}"',
        rf'"{re.escape(key)}"\s*:\s*"{re.escape(target)}"',
    ]
    return any(re.search(pat, content) for pat in patterns)


def apply_structured_replacements(content: str, structured: dict) -> tuple[str, dict]:
    """按对象字面量 key 锚定替换字符串值，降低压缩变量改名带来的失效。"""
    stats = {"structured": 0, "missed_structured": [], "skipped_optional": []}
    for item in structured.get("structuredReplacements", []):
        if item.get("type") != "object-literal-values":
            continue
        values = item.get("values") or {}
        required = item.get("requiredKeys") or []
        optional = item.get("optional", True)
        item_id = item.get("id", "structured")
        min_hits = max(1, (len(required) + 1) // 2) if required else 0

        def mark_miss() -> None:
            # 可选补丁在当前包体中不存在时静默跳过，避免重复安装误报
            if optional:
                stats["skipped_optional"].append(item_id)
            else:
                stats["missed_structured"].append(item_id)

        hit_required = sum(1 for key in required if _key_present(content, key))
        if required and hit_required < min_hits:
            # exact 可能已写入译文，或本文件根本不含该模块
            if _quoted_target_coverage(content, values) >= max(1, len(values) // 2):
                continue
            mark_miss()
            continue

        changed_here = 0
        already_ok = 0
        for key, mapping in values.items():
            source = mapping.get("source")
            target = mapping.get("target")
            if not source or not target or source == target:
                continue
            content, n = _replace_object_value(content, key, source, target)
            if n:
                changed_here += n
            elif _value_already_translated(content, key, target):
                already_ok += 1
        if changed_here:
            stats["structured"] += changed_here
        elif already_ok > 0:
            continue
        elif _quoted_target_coverage(content, values) >= max(1, len(values) // 2):
            continue
        else:
            mark_miss()

    return content, stats

def update_product_checksums(app_root: Path, changed_rel_paths: list[str], dry_run: bool = False) -> int:
    product_path = app_root / "product.json"
    product = json.loads(product_path.read_text(encoding="utf-8"))
    checksums = product.get("checksums", {})
    updated = 0

    for rel in changed_rel_paths:
        # product.json uses paths relative to out/
        key = rel[4:] if rel.startswith("out/") else rel
        if key not in checksums:
            continue
        data = (app_root / "out" / key).read_bytes()
        new_sum = sha256_checksum(data)
        if checksums[key] != new_sum:
            log(f"  更新校验和: {key}")
            log(f"    {checksums[key]} -> {new_sum}")
            checksums[key] = new_sum
            updated += 1

    if updated and not dry_run:
        backup_file(product_path)
        product["checksums"] = checksums
        product_path.write_text(json.dumps(product, ensure_ascii=False, indent="\t") + "\n", encoding="utf-8")

    return updated


def patch_file(
    app_root: Path,
    rel_path: str,
    config: dict,
    structured: dict | None = None,
    dry_run: bool = False,
) -> dict | None:
    target = app_root / rel_path
    if not target.exists():
        return None

    size_mb = target.stat().st_size / (1024 * 1024)
    log(f"  处理: {rel_path} ({size_mb:.1f} MB) ...")
    t0 = time.time()
    original = target.read_text(encoding="utf-8")
    log(f"    已读取 ({time.time() - t0:.1f}s)，开始替换（约 {len(config.get('exact', []))} 条）...")

    patched, stats = apply_replacements(original, config, progress_label=Path(rel_path).name)
    if structured:
        patched, struct_stats = apply_structured_replacements(patched, structured)
        stats["structured"] = struct_stats.get("structured", 0)
        stats["missed_structured"] = struct_stats.get("missed_structured", [])
        stats["skipped_optional"] = struct_stats.get("skipped_optional", [])
    else:
        stats.setdefault("structured", 0)
        stats.setdefault("missed_structured", [])
        stats.setdefault("skipped_optional", [])

    if patched == original:
        log(f"    无变化（已是最新或无需补丁）({time.time() - t0:.1f}s)")
        return {"file": rel_path, "changed": False, **stats}
    if not dry_run:
        log("    正在备份并写入...")
        backup_file(target)
        target.write_text(patched, encoding="utf-8")

    log(f"    完成 ({time.time() - t0:.1f}s)")
    return {"file": rel_path, "changed": True, **stats}


def restore_file(filename: str, app_root: Path, rel_path: str) -> bool:
    backup = find_latest_backup(filename)
    if not backup:
        return False
    target = app_root / rel_path
    if not target.parent.exists():
        return False
    shutil.copy2(backup, target)
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description="Patch Cursor Glass UI strings for zh-cn")
    parser.add_argument(
        "--app-root",
        default=None,
        help="Cursor resources/app path (auto-detect if omitted)",
    )
    parser.add_argument("--dry-run", action="store_true", help="Preview without writing")
    parser.add_argument("--restore", action="store_true", help="Restore latest backups")
    args = parser.parse_args()

    try:
        app_root = find_cursor_app_root(args.app_root)
    except FileNotFoundError as exc:
        log(f"错误: {exc}")
        return 1

    log(f"平台: {platform_display_name()}")
    log(f"Cursor app root: {app_root}")

    config = load_config()
    structured = load_structured()
    if structured:
        log(f"已加载结构化补丁: {STRUCTURED_FILE.name}")

    if args.restore:
        try:
            ensure_writable(app_root)
        except PermissionError as exc:
            log(f"错误: {exc}")
            return 1
        restored = 0
        targets = config.get("targets") or []
        if structured and structured.get("targets"):
            # 合并目标，去重
            seen = {t["file"] for t in targets}
            for t in structured["targets"]:
                if t["file"] not in seen:
                    targets.append(t)
                    seen.add(t["file"])
        for target in targets:
            rel = target["file"]
            filename = Path(rel).name
            if restore_file(filename, app_root, rel):
                log(f"已恢复 {rel}")
                restored += 1
        if restore_file("product.json", app_root, "product.json"):
            log("已恢复 product.json")
            restored += 1
        if restored:
            keys = [t["file"] for t in targets]
            update_product_checksums(app_root, keys, dry_run=False)
        if restored == 0:
            log("未找到可恢复的备份文件")
            return 1
        log(f"共恢复 {restored} 个文件")
        log(quit_hint())
        return 0

    if not args.dry_run:
        try:
            ensure_writable(app_root)
        except PermissionError as exc:
            log(f"错误: {exc}")
            return 1

    log("==> 应用 Glass UI 中文补丁（并同步 product.json 校验和）")
    log("    提示: workbench JS 体积较大，首次可能需要 1–3 分钟，请耐心等待。")
    total_changed = 0
    changed_files: list[str] = []
    for target in config["targets"]:
        rel = target["file"]
        required = target.get("required", True)
        try:
            result = patch_file(app_root, rel, config, structured=structured, dry_run=args.dry_run)
        except PermissionError as exc:
            log(f"错误: 写入失败 {rel}: {exc}")
            if is_windows() and "Program Files" in str(app_root):
                log("请以管理员身份重新打开 PowerShell，再运行安装脚本。")
            return 1
        except OSError as exc:
            log(f"错误: 处理失败 {rel}: {exc}")
            return 1

        if result is None:
            msg = f"跳过（文件不存在）: {rel}"
            if required:
                log(f"警告: {msg}")
            else:
                log(msg)
            continue

        status = "已更新" if result["changed"] else "无变化"
        log(f"  {rel}: {status}")
        if result["changed"]:
            total_changed += 1
            changed_files.append(rel)
            log(f"    - 块替换: {result['blocks']} 处")
            log(f"    - 精确替换: {result['exact']} 处")
            log(f"    - 结构化替换: {result.get('structured', 0)} 处")
        if result["missed_blocks"]:
            log(f"    - 未命中必选块: {', '.join(result['missed_blocks'][:5])}")
        if result.get("missed_structured"):
            log(f"    - 未命中必选结构化项: {', '.join(result['missed_structured'][:5])}")
        if result["missed_exact"] and result["changed"]:
            missed = result["missed_exact"]
            log(f"    - 未命中条目: {len(missed)} 条（多为历史失效词条，可忽略）")

    if changed_files:
        updated = update_product_checksums(app_root, changed_files, dry_run=args.dry_run)
        log(f"  product.json 校验和更新: {updated} 项")

    if args.dry_run:
        log("预览模式，未写入任何文件")
    elif total_changed == 0:
        log("没有文件被修改（已打过补丁，可直接重启 Cursor）")
    else:
        log(f"完成，共修改 {total_changed} 个文件")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
