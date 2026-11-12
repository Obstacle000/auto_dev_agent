"""SEARCH/REPLACE 代码补丁解析、多级容错匹配与多语言 AST 语法校验器.

移植自 aider/coders/editblock_coder.py，包含:
1. 精确行块匹配 (`perfect_replace`)
2. 统一前导缩进偏差自动补偿 (`replace_part_with_missing_leading_whitespace`)
3. 块首多余空行剥离重试
4. `...` 省略号分段匹配 (`try_dotdotdots`)
5. 编辑距离相似度兜底匹配 (`replace_closest_edit_distance`, threshold=0.8)
6. 多语言 Tree-sitter 语法树错误检查 (`tree.root_node.has_error`) + Python `ast.parse`
"""

import ast
from dataclasses import dataclass
from difflib import SequenceMatcher
import math
from pathlib import Path
import re

from grep_ast import filename_to_lang
from grep_ast.tsl import get_parser

_HEAD_RE = re.compile(r"^<{5,9}\s*SEARCH\b.*$")
_DIVIDER_RE = re.compile(r"^={5,9}\s*$")
_UPDATED_RE = re.compile(r"^>{5,9}\s*REPLACE\b.*$")
_FENCE_RE = re.compile(r"^\s*```.*$")


@dataclass
class EditBlock:
    file_path: str
    search_text: str
    replace_text: str


def _extract_preceding_filename(preceding_lines: list[str]) -> str:
    for line in reversed(preceding_lines[-3:]):
        stripped = line.strip()
        if not stripped or _FENCE_RE.match(stripped):
            continue
        cleaned = stripped.strip("`").lstrip("#").strip()
        if cleaned.startswith("File:"):
            cleaned = cleaned[5:].strip()
        if cleaned and not cleaned.startswith(("<", "=", ">")):
            return cleaned
    return ""


def parse_search_replace_blocks(raw_text: str) -> list[EditBlock]:
    """逐行解析 LLM 输出的 SEARCH/REPLACE 编辑块，兼容 Markdown 代码围栏与多文件流."""
    lines = (raw_text or "").splitlines(keepends=True)
    blocks: list[EditBlock] = []
    current_filename = ""
    i = 0

    while i < len(lines):
        stripped = lines[i].strip()
        if _HEAD_RE.match(stripped):
            fname = _extract_preceding_filename(lines[max(0, i - 4) : i]) or current_filename
            if fname:
                current_filename = fname
            i += 1
            search_lines: list[str] = []
            while i < len(lines) and not _DIVIDER_RE.match(lines[i].strip()):
                search_lines.append(lines[i])
                i += 1
            if i >= len(lines):
                raise ValueError("SEARCH 块缺少 `=======` 分隔符")
            i += 1
            replace_lines: list[str] = []
            while i < len(lines) and not _UPDATED_RE.match(lines[i].strip()):
                replace_lines.append(lines[i])
                i += 1
            if i >= len(lines):
                raise ValueError("REPLACE 块缺少 `>>>>>>> REPLACE` 结束标记")
            if not fname:
                raise ValueError("未能在 SEARCH 块前识别到目标文件路径")
            blocks.append(
                EditBlock(
                    file_path=fname,
                    search_text="".join(search_lines),
                    replace_text="".join(replace_lines),
                )
            )
        i += 1

    return blocks


def _prep(content: str) -> tuple[str, list[str]]:
    if content and not content.endswith("\n"):
        content += "\n"
    return content, content.splitlines(keepends=True)


def _perfect_replace(
    whole_lines: list[str], part_lines: list[str], replace_lines: list[str]
) -> str | None:
    part_tup = tuple(part_lines)
    part_len = len(part_lines)
    for i in range(len(whole_lines) - part_len + 1):
        if tuple(whole_lines[i : i + part_len]) == part_tup:
            return "".join(whole_lines[:i] + replace_lines + whole_lines[i + part_len :])
    return None


def _match_but_for_leading_whitespace(
    whole_slice: list[str], part_lines: list[str]
) -> str | None:
    if not all(
        whole_slice[i].lstrip() == part_lines[i].lstrip()
        for i in range(len(whole_slice))
    ):
        return None
    non_blank_indices = [i for i in range(len(whole_slice)) if whole_slice[i].strip()]
    if not non_blank_indices:
        return ""
    if not all(whole_slice[i].endswith(part_lines[i]) for i in non_blank_indices):
        return None
    prefixes = {
        whole_slice[i][: len(whole_slice[i]) - len(part_lines[i])]
        for i in non_blank_indices
    }
    if len(prefixes) != 1:
        return None
    return prefixes.pop()


def _replace_missing_leading_whitespace(
    whole_lines: list[str], part_lines: list[str], replace_lines: list[str]
) -> str | None:
    leading = [len(p) - len(p.lstrip()) for p in part_lines if p.strip()] + [
        len(p) - len(p.lstrip()) for p in replace_lines if p.strip()
    ]
    if leading and min(leading):
        num_leading = min(leading)
        part_lines = [p[num_leading:] if p.strip() else p for p in part_lines]
        replace_lines = [p[num_leading:] if p.strip() else p for p in replace_lines]

    num_part = len(part_lines)
    for i in range(len(whole_lines) - num_part + 1):
        add_leading = _match_but_for_leading_whitespace(
            whole_lines[i : i + num_part], part_lines
        )
        if add_leading is None:
            continue
        adjusted = [add_leading + r if r.strip() else r for r in replace_lines]
        return "".join(whole_lines[:i] + adjusted + whole_lines[i + num_part :])
    return None


def _perfect_or_whitespace(
    whole_lines: list[str], part_lines: list[str], replace_lines: list[str]
) -> str | None:
    res = _perfect_replace(whole_lines, part_lines, replace_lines)
    if res is not None:
        return res
    return _replace_missing_leading_whitespace(whole_lines, part_lines, replace_lines)


def _try_dotdotdots(whole: str, part: str, replace: str) -> str | None:
    dots_re = re.compile(r"(^\s*\.\.\.\r?\n)", re.MULTILINE)
    part_pieces = re.split(dots_re, part)
    replace_pieces = re.split(dots_re, replace)
    if len(part_pieces) != len(replace_pieces) or len(part_pieces) == 1:
        return None
    if not all(part_pieces[i] == replace_pieces[i] for i in range(1, len(part_pieces), 2)):
        raise ValueError("SEARCH 与 REPLACE 块中的 `...` 分段不匹配")

    parts = [part_pieces[i] for i in range(0, len(part_pieces), 2)]
    replaces = [replace_pieces[i] for i in range(0, len(replace_pieces), 2)]
    for p, r in zip(parts, replaces):
        if not p and not r:
            continue
        if not p and r:
            if not whole.endswith("\n"):
                whole += "\n"
            whole += r
            continue
        if whole.count(p) != 1:
            raise ValueError("`...` 分段在原文件中不存在或匹配不唯一")
        whole = whole.replace(p, r, 1)
    return whole


def _replace_closest_edit_distance(
    whole_lines: list[str], part: str, part_lines: list[str], replace_lines: list[str]
) -> str | None:
    threshold = 0.8
    max_sim = 0.0
    best_start = -1
    best_end = -1
    min_len = max(1, math.floor(len(part_lines) * 0.9))
    max_len = max(min_len + 1, math.ceil(len(part_lines) * 1.1) + 1)

    for length in range(min_len, min(max_len, len(whole_lines) + 1)):
        for i in range(len(whole_lines) - length + 1):
            chunk = "".join(whole_lines[i : i + length])
            sim = SequenceMatcher(None, chunk, part).ratio()
            if sim > max_sim:
                max_sim = sim
                best_start = i
                best_end = i + length

    if max_sim < threshold or best_start < 0:
        return None
    return "".join(whole_lines[:best_start] + replace_lines + whole_lines[best_end:])


def replace_most_similar_chunk(whole: str, part: str, replace: str) -> str | None:
    """多级容错匹配替换 (精确 -> 缩进补偿 -> 忽略首空行 -> `...` 分段 -> 编辑距离相似度)."""
    whole, whole_lines = _prep(whole)
    part, part_lines = _prep(part)
    replace, replace_lines = _prep(replace)

    res = _perfect_or_whitespace(whole_lines, part_lines, replace_lines)
    if res is not None:
        return res

    if len(part_lines) > 2 and not part_lines[0].strip():
        res = _perfect_or_whitespace(whole_lines, part_lines[1:], replace_lines)
        if res is not None:
            return res

    try:
        res = _try_dotdotdots(whole, part, replace)
        if res is not None:
            return res
    except ValueError:
        pass

    return _replace_closest_edit_distance(whole_lines, part, part_lines, replace_lines)


def _validate_syntax(target: Path, content: str) -> None:
    """结合 Python AST 与多语言 Tree-sitter 解析器检查语法正确性."""
    if target.suffix == ".py":
        ast.parse(content)
        return
    lang = filename_to_lang(str(target))
    if not lang:
        return
    try:
        parser = get_parser(lang)
    except Exception:
        return
    tree = parser.parse(bytes(content, "utf-8"))
    if tree.root_node.has_error:
        raise SyntaxError(f"{target.name} ({lang}) 存在 Tree-sitter 语法错误")


def _resolve_safe_path(workspace_dir: Path, rel_path: str) -> Path:
    clean_rel = rel_path.replace("\\", "/").lstrip("/")
    target = (workspace_dir / clean_rel).resolve()
    root_resolved = workspace_dir.resolve()
    if target != root_resolved and root_resolved not in target.parents:
        raise ValueError(f"非法越界路径禁止修改: {rel_path}")
    return target


def apply_edit_blocks(workspace_dir: str | Path, blocks: list[EditBlock]) -> list[str]:
    """将 EditBlock 应用到沙箱工作区并做多语言 AST 语法检查."""
    root = Path(workspace_dir).resolve()
    edited_files: list[str] = []

    for blk in blocks:
        target = _resolve_safe_path(root, blk.file_path)
        if not target.exists():
            if blk.search_text.strip() == "":
                target.parent.mkdir(parents=True, exist_ok=True)
                new_content = blk.replace_text
            else:
                raise FileNotFoundError(f"待修改文件不存在: {blk.file_path}")
        else:
            old_content = target.read_text(encoding="utf-8")
            if blk.search_text == "":
                new_content = blk.replace_text
            else:
                replaced = replace_most_similar_chunk(
                    old_content, blk.search_text, blk.replace_text
                )
                if replaced is None:
                    raise ValueError(f"SEARCH 块未能在 {blk.file_path} 中匹配成功")
                new_content = replaced

        _validate_syntax(target, new_content)
        target.write_text(new_content, encoding="utf-8")
        norm_rel = target.relative_to(root).as_posix()
        if norm_rel not in edited_files:
            edited_files.append(norm_rel)

    return edited_files
