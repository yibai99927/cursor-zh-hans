#!/usr/bin/env python3
"""patch-glass-ui 核心替换逻辑的单元测试（无需安装 Cursor）。"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MOD_PATH = ROOT / "scripts" / "patch-glass-ui.py"


def load_patch_module():
    spec = importlib.util.spec_from_file_location("patch_glass_ui", MOD_PATH)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["patch_glass_ui"] = mod
    # paths.py 同目录依赖
    sys.path.insert(0, str(ROOT / "scripts"))
    spec.loader.exec_module(mod)
    return mod


mod = load_patch_module()
replace_exact_string = mod.replace_exact_string
apply_structured_replacements = mod.apply_structured_replacements


def test_short_exact_quote_only() -> None:
    src = 'foo:"Accept",bar:"Accepted",baz:Accept'
    out, n = replace_exact_string(src, "Accept", "接受")
    assert n == 1, n
    assert 'foo:"接受"' in out
    assert 'bar:"Accepted"' in out  # 不得误伤
    assert "baz:Accept" in out  # 短词禁止裸替换


def test_contextual_label_applies_without_extra_quotes() -> None:
    src = 'kc("appearance","theme",{label:"Theme",surface:"glass"})'
    out, n = replace_exact_string(src, 'label:"Theme"', 'label:"主题"')
    assert n == 1, n
    assert 'label:"主题"' in out
    assert 'label:"Theme"' not in out


def test_long_exact_unquoted_fallback() -> None:
    phrase = "Prevent Agent from automatically running Browser tools"
    zh = "阻止 Agent 自动运行浏览器工具"
    src = f"html('<p>{phrase}</p>')"
    out, n = replace_exact_string(src, phrase, zh)
    assert n == 1
    assert zh in out


def test_structured_translates_and_skips_already_done() -> None:
    content = (
        'nav={general:"General",chat:"Agents",'
        'protectionDescription:"Prevent Agent from automatically running Browser tools",'
        'showLocalhostLinks:"Show Localhost Links in Browser",'
        'openWebLinks:"Open Web Links in Browser",'
        'section:"Browser",automation:"Browser Automation",protection:"Browser Protection"}'
    )
    structured = {
        "structuredReplacements": [
            {
                "id": "settings-nav-labels",
                "type": "object-literal-values",
                "requiredKeys": ["general", "chat"],
                "values": {
                    "general": {"source": "General", "target": "通用"},
                    "chat": {"source": "Agents", "target": "Agent"},
                },
            },
            {
                "id": "browser-settings-labels",
                "type": "object-literal-values",
                "requiredKeys": [
                    "protectionDescription",
                    "showLocalhostLinks",
                    "openWebLinks",
                ],
                "values": {
                    "section": {"source": "Browser", "target": "浏览器"},
                    "automation": {
                        "source": "Browser Automation",
                        "target": "浏览器自动化",
                    },
                },
            },
        ]
    }
    patched, stats = apply_structured_replacements(content, structured)
    assert 'general:"通用"' in patched
    assert 'section:"浏览器"' in patched
    assert stats["structured"] >= 3
    assert stats["missed_structured"] == []

    # 再次应用应视为已翻译，不得报未命中
    patched2, stats2 = apply_structured_replacements(patched, structured)
    assert patched2 == patched
    assert stats2["missed_structured"] == []


def test_optional_structured_missing_is_silent() -> None:
    content = 'other={foo:"bar"}'
    structured = {
        "structuredReplacements": [
            {
                "id": "browser-settings-labels",
                "type": "object-literal-values",
                "optional": True,
                "requiredKeys": [
                    "protectionDescription",
                    "showLocalhostLinks",
                    "openWebLinks",
                ],
                "values": {
                    "section": {"source": "Browser", "target": "浏览器"},
                    "automation": {
                        "source": "Browser Automation",
                        "target": "浏览器自动化",
                    },
                },
            }
        ]
    }
    patched, stats = apply_structured_replacements(content, structured)
    assert patched == content
    assert stats["missed_structured"] == []
    assert "browser-settings-labels" in stats["skipped_optional"]


def test_exact_coverage_counts_as_structured_ok() -> None:
    # 无对象 key，但 exact 已写入译文
    content = '"浏览器自动化","浏览器保护","阻止 Agent 自动运行浏览器工具","在浏览器中显示 Localhost 链接","在浏览器中打开网页链接","浏览器"'
    structured = {
        "structuredReplacements": [
            {
                "id": "browser-settings-labels",
                "type": "object-literal-values",
                "optional": True,
                "requiredKeys": [
                    "protectionDescription",
                    "showLocalhostLinks",
                    "openWebLinks",
                ],
                "values": {
                    "section": {"source": "Browser", "target": "浏览器"},
                    "automation": {
                        "source": "Browser Automation",
                        "target": "浏览器自动化",
                    },
                    "protection": {
                        "source": "Browser Protection",
                        "target": "浏览器保护",
                    },
                    "protectionDescription": {
                        "source": "Prevent Agent from automatically running Browser tools",
                        "target": "阻止 Agent 自动运行浏览器工具",
                    },
                    "showLocalhostLinks": {
                        "source": "Show Localhost Links in Browser",
                        "target": "在浏览器中显示 Localhost 链接",
                    },
                    "openWebLinks": {
                        "source": "Open Web Links in Browser",
                        "target": "在浏览器中打开网页链接",
                    },
                },
            }
        ]
    }
    _, stats = apply_structured_replacements(content, structured)
    assert stats["missed_structured"] == []
    assert stats["skipped_optional"] == []


def main() -> int:
    test_short_exact_quote_only()
    test_contextual_label_applies_without_extra_quotes()
    test_long_exact_unquoted_fallback()
    test_structured_translates_and_skips_already_done()
    test_optional_structured_missing_is_silent()
    test_exact_coverage_counts_as_structured_ok()
    print("ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
