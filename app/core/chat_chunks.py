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
        # Prompt Cache（提示词缓存）是大模型 API 服务端提供的一项计算复用机制：当多个请求拥有完全相同的开头前缀时，服务端直接复用之前算好的中间推理状态（KV Cache），跳过重复计算，从而同时降低费用和首字延迟。
        # Transformer 模型在生成回复前，必须先把你传进去的全部输入 Token 做一次前向计算，算出每一层的注意力 Key/Value 矩阵（称为 KV Cache），这个过程叫 Prefill（预填充）。
        # 在多轮对话或 Agent 循环中，每次请求都要把前面几万 Token（System Prompt + Tools + RepoMap + 历史消息）重新发给服务端。
        # 开启 Prompt Cache 后：
        #   第 1 次请求：服务端完整计算 前缀 A + 新消息 1 的 KV Cache，并将 前缀 A 的 KV Cache 暂存在服务端缓存中（通常存活几分钟）。
        #   第 2 次请求：发送 前缀 A + 新消息 1 + 新消息 2。服务端发现开头的 前缀 A（或 前缀 A + 新消息 1）一字不差地命中了缓存，直接读取已有 KV Cache，只对末尾新增的 新消息 2 执行 Prefill 计算。
        # 项目里只要保证高命中率即可,缓存是大模型底层云服务器做的事情
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
