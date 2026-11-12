"""提示词前缀稳定组装器 (参考 aider/coders/chat_chunks.py 提升 Prompt Cache 命中率)."""

from dataclasses import dataclass, field
from pathlib import Path
from langchain_core.messages import AnyMessage, HumanMessage, SystemMessage


def load_chat_files_block(
    workspace_dir: str | Path,
    chat_fnames: list[str],
    *,
    max_total_chars: int = 24000,
    max_file_chars: int = 8000,
) -> str:
    """安全读取 chat_fnames 中的已有源码正文，组装为模型完整文件上下文块."""
    if not chat_fnames or not workspace_dir:
        return ""
    root = Path(workspace_dir).resolve()
    parts: list[str] = []
    used = 0
    for rel in chat_fnames:
        norm = rel.replace("\\", "/").lstrip("/")
        if not norm:
            continue
        target = (root / norm).resolve()
        if target != root and root not in target.parents:
            continue
        if not target.is_file():
            continue
        try:
            text = target.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if len(text) > max_file_chars:
            text = text[:max_file_chars] + "\n... (文件内容过长已截断)"
        block = f"### {norm}\n```\n{text}\n```"
        if used + len(block) > max_total_chars and parts:
            break
        parts.append(block)
        used += len(block)
    return "\n\n".join(parts)


@dataclass
class ChatChunks:
    """按变动频率由低到高排列消息段落: 系统指令 -> RepoMap -> DBRepoMap -> ChatFiles -> 证据 -> 历史 -> 当前输入."""

    system_prompt: str
    repo_map: str = ""
    db_repo_map: str = ""
    chat_files_block: str = ""
    evidence_block: str = ""
    history_messages: list[AnyMessage] = field(default_factory=list)
    user_query: str = ""

    def to_messages(self) -> list[AnyMessage]:
        """构建稳定前缀顺序的消息列表."""
        messages: list[AnyMessage] = [SystemMessage(content=self.system_prompt)]
        if self.repo_map.strip():
            messages.append(SystemMessage(content=f"[REPO_MAP]\n{self.repo_map}"))
        if self.db_repo_map.strip():
            messages.append(SystemMessage(content=f"[DB_REPO_MAP]\n{self.db_repo_map}"))
        if self.chat_files_block.strip():
            messages.append(SystemMessage(content=f"[CHAT_FILES]\n{self.chat_files_block}"))
        if self.evidence_block.strip():
            messages.append(SystemMessage(content=f"[EVIDENCE_POOL]\n{self.evidence_block}"))
        messages.extend(self.history_messages)
        if self.user_query.strip():
            messages.append(HumanMessage(content=self.user_query))
        return messages
