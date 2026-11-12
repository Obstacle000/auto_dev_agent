"""子图 1：证据探查子图 (`evidence_probe_subgraph`)."""

from pathlib import Path
import re
from typing import Any, Literal
from langchain_core.messages import AIMessage, BaseMessage, ToolMessage
from langchain_core.tools import tool
from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime
from langgraph.types import Send

from app.clients.llm_client_manager import llm_client_manager
from app.core.chat_chunks import ChatChunks
from app.core.intent_router import classify_task_intent
from app.graphs.context import AutoDevContext
from app.graphs.state import (
    CodeProbeOutputState,
    DBProbeOutputState,
    EvidenceSubState,
    ProbeWorkerState,
)
from app.schemas.domain_schemas import Evidence, TaskIntent
from app.tools.cache_tools import search_error_cache
from app.tools.code_tools import git_log_recent, grep_code, ls_directory, read_file_window
from app.tools.db_tools import (
    execute_readonly_sql,
    generate_db_repo_map,
    inspect_table_schema,
)
from app.tools.log_tools import probe_runtime_logs


def _emit_custom(event: str, data: dict) -> None:
    try:
        from langgraph.config import get_stream_writer

        writer = get_stream_writer()
        if writer:
            writer({"event": event, "data": data})
    except Exception:
        pass


def _extract_search_keyword(question: str) -> str:
    tokens = re.findall(r"[A-Za-z_][A-Za-z0-9_\.]{2,}", question or "")
    return tokens[0] if tokens else "error"


async def plan_probes_node(
    state: EvidenceSubState, runtime: Runtime[AutoDevContext]
) -> dict:
    """若上游未指定 intent/enabled_probes（如独立调用子图），在此完成入口分流与探针规划."""
    if state.get("enabled_probes"):
        return {}
    ctx = runtime.context
    has_db = bool(
        state.get("has_db_datasource")
        and ctx["datasource_registry"].has_db_datasources()
    )
    has_log_signal = bool(
        state.get("has_log_datasource")
        or ctx.get("es_log_index")
        or ctx.get("log_path")
    )
    intent, enabled = await classify_task_intent(
        state.get("user_query", ""),
        repo_map=state.get("repo_map", ""),
        has_db_datasource=has_db,
        has_log_signal=has_log_signal,
    )
    return {"intent": intent, "enabled_probes": enabled}


def _make_probe_payload(
    probe_type: str,
    *,
    intent: TaskIntent,
    query: str,
    repo_map: str,
    db_repo_map: str = "",
    chat_fnames: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "probe_type": probe_type,
        "intent": intent,
        "target_question": query,
        "repo_map": repo_map,
        "db_repo_map": db_repo_map,
        "worker_messages": [],
        "chat_fnames": list(chat_fnames or []),
        "evidences": [],
        "gaps": [],
    }


def route_probes_edge(
    state: EvidenceSubState, runtime: Runtime[AutoDevContext]
) -> list[Send]:
    """按分流结果 `enabled_probes` 与运行期数据源配置动态分发 Send 分支."""
    query = state.get("user_query", "")
    repo_map = state.get("repo_map", "")
    db_repo_map = state.get("db_repo_map", "")
    chat_fnames = list(state.get("chat_fnames") or [])
    intent: TaskIntent = state.get("intent") or "bugfix"
    enabled = set(state.get("enabled_probes") or ["source_code"])

    sends: list[Send] = []
    if "error_cache" in enabled:
        sends.append(
            Send(
                "cache_probe_worker",
                _make_probe_payload("error_cache", intent=intent, query=query, repo_map=repo_map),
            )
        )
    if "source_code" in enabled or not enabled:
        sends.append(
            Send(
                "code_probe_worker",
                _make_probe_payload(
                    "source_code",
                    intent=intent,
                    query=query,
                    repo_map=repo_map,
                    chat_fnames=chat_fnames,
                ),
            )
        )
    if "log_error" in enabled:
        sends.append(
            Send(
                "log_probe_worker",
                _make_probe_payload("log_error", intent=intent, query=query, repo_map=repo_map),
            )
        )
    if (
        "db_query" in enabled
        and state.get("has_db_datasource")
        and runtime.context["datasource_registry"].has_db_datasources()
    ):
        sends.append(
            Send(
                "db_probe_worker",
                _make_probe_payload(
                    "db_query",
                    intent=intent,
                    query=query,
                    repo_map=repo_map,
                    db_repo_map=db_repo_map,
                ),
            )
        )
    return sends


async def cache_probe_worker(
    state: ProbeWorkerState, runtime: Runtime[AutoDevContext]
) -> dict:
    """ES (BM25) + Qdrant (Vector) RRF 报错缓存检索 Worker."""
    ctx = runtime.context
    _emit_custom(
        "tool_start",
        {
            "tool_name": "search_error_cache",
            "args_summary": f"namespace={ctx['namespace']}",
            "node_name": "cache_probe_worker",
            "probe_type": "error_cache",
        },
    )
    evidences, gaps = await search_error_cache(
        namespace=ctx["namespace"],
        query=state.get("target_question", ""),
        es_repo=ctx.get("error_cache_es_repo"),
        qdrant_repo=ctx.get("error_cache_qdrant_repo"),
        embedding_client=ctx.get("embedding_client"),
        top_k=3,
    )
    for ev in evidences:
        _emit_custom(
            "evidence_added",
            {
                "evidence_id": ev.id,
                "evidence_type": ev.evidence_type,
                "summary": ev.summary,
            },
        )
    _emit_custom(
        "tool_end",
        {
            "tool_name": "search_error_cache",
            "status": "ok",
            "duration_ms": 0,
            "summary": f"召回 {len(evidences)} 条缓存证据",
        },
    )
    return {"evidences": evidences, "gaps": gaps}


def _record_code_tool(
    tool_name: str,
    args_summary: str,
    runner,
    collected: list[Evidence],
    node_name: str = "code_tools_node",
) -> Evidence | None:
    """统一发射工具事件并记录工具产出的源码证据."""
    _emit_custom(
        "tool_start",
        {
            "tool_name": tool_name,
            "args_summary": args_summary,
            "node_name": node_name,
            "probe_type": "source_code",
        },
    )
    try:
        ev: Evidence = runner()
    except Exception as exc:
        _emit_custom(
            "tool_end",
            {
                "tool_name": tool_name,
                "status": "error",
                "duration_ms": 0,
                "summary": f"{tool_name} 执行失败: {exc}",
            },
        )
        return None

    collected.append(ev)
    _emit_custom(
        "evidence_added",
        {
            "evidence_id": ev.id,
            "evidence_type": ev.evidence_type,
            "summary": ev.summary,
        },
    )
    _emit_custom(
        "tool_end",
        {
            "tool_name": tool_name,
            "status": "ok",
            "duration_ms": 0,
            "summary": ev.summary,
        },
    )
    return ev


def _build_code_probe_tools(
    repo_root: str,
    collected: list[Evidence],
    read_files: list[str],
):
    """构造绑定当前仓库根目录的只读 LangChain 工具集."""

    @tool
    def list_repo_directory(rel_dir: str = ".") -> str:
        """列出目标代码仓库相对目录下的直接文件与子目录，用于确认模块布局."""
        ev = _record_code_tool(
            "ls_directory",
            f"dir={rel_dir}",
            lambda: ls_directory(repo_root, rel_dir),
            collected,
        )
        if ev is None:
            return f"目录读取失败: {rel_dir}"
        return f"[{ev.id}] {ev.summary}\n{ev.raw_snippet}"

    @tool
    def search_repo_code(pattern: str, max_hits: int = 25) -> str:
        """按关键字或正则表达式全仓搜索源码位置并签发 Evidence 证据."""
        ev = _record_code_tool(
            "grep_code",
            f"pattern={pattern}",
            lambda: grep_code(repo_root, pattern, max_hits=max_hits),
            collected,
        )
        if ev is None:
            return f"正则检索失败（请检查正则语法）: {pattern}"
        return f"[{ev.id}] {ev.summary}\n{ev.raw_snippet}"

    @tool
    def read_repo_file(rel_path: str, start_line: int = 1, end_line: int = 120) -> str:
        """读取指定相对路径文件在 [start_line, end_line] 区间内的代码并纳入 chat_fnames 上下文."""
        ev = _record_code_tool(
            "read_file_window",
            f"file={rel_path}:{start_line}-{end_line}",
            lambda: read_file_window(repo_root, rel_path, start_line=start_line, end_line=end_line),
            collected,
        )
        if ev is None:
            return f"读取文件失败（非法路径或IO错误）: {rel_path}"
        norm_rel = rel_path.replace("\\", "/").lstrip("/")
        if norm_rel and not ev.raw_snippet.startswith("文件不存在") and norm_rel not in read_files:
            read_files.append(norm_rel)
        return f"[{ev.id}] {ev.summary}\n{ev.raw_snippet}"

    @tool
    def git_recent_commits(limit: int = 5) -> str:
        """查看仓库最近 Git 提交记录，辅助确认近期变更上下文."""
        ev = _record_code_tool(
            "git_log_recent",
            f"limit={limit}",
            lambda: git_log_recent(repo_root, limit=limit),
            collected,
        )
        if ev is None:
            return "读取 Git 提交历史失败"
        return f"[{ev.id}] {ev.summary}\n{ev.raw_snippet}"

    return [list_repo_directory, search_repo_code, read_repo_file, git_recent_commits]


MAX_CODE_PROBE_TURNS = 4


def _build_code_probe_messages(
    *,
    repo_map: str,
    question: str,
    intent: TaskIntent,
    worker_messages: list[BaseMessage],
) -> list[BaseMessage]:
    """组装自主源码探查 Agent 的提示词上下文与 ReAct 对话历史."""
    if intent == "feature":
        mission = (
            "当前任务属于功能创建/增量改造：请先阅读 [REPO_MAP] 大纲确定挂载模块与参考文件，"
            "自主调用 `read_repo_file` / `search_repo_code` / `list_repo_directory` 读取关键现有代码范式、"
            "接口定义与目录结构，为后续编码与集成收集充分上下文。"
        )
    else:
        mission = (
            "当前任务属于故障排查/代码分析：请先阅读 [REPO_MAP] 大纲与问题线索，"
            "自主调用 `search_repo_code` / `read_repo_file` / `git_recent_commits` 追踪目标符号、调用链与报错现场代码。"
        )

    chunks = ChatChunks(
        system_prompt=(
            f"你是自主源码探查 ReAct Agent。{mission}\n"
            "执行规范：\n"
            "1. 你位于多轮 ReAct 循环中：每次工具调用结果返回后，若信息仍不充分（例如刚搜到文件路径还需读具体代码行），"
            "请继续调用下一轮工具；不要猜测未读过的代码。\n"
            f"2. 最多连续探索 {MAX_CODE_PROBE_TURNS} 轮；一旦确认关键文件并读取核心代码行，请停止调用工具并用一两句话总结核心发现。"
        ),
        repo_map=repo_map,
        user_query=question,
    )
    return [*chunks.to_messages(), *worker_messages]


def _fallback_code_probe(
    repo_root: str,
    question: str,
    collected: list[Evidence],
    read_files: list[str],
) -> None:
    """无有效 LLM Key 或模型调用异常时的确定性兜底取证."""
    keyword = _extract_search_keyword(question)
    _record_code_tool(
        "ls_directory",
        "dir=.",
        lambda: ls_directory(repo_root, "."),
        collected,
        node_name="code_agent_node",
    )
    # 按关键字在仓库里搜源码位置并生成一条 Evidence（证据对象）。
    grep_ev = _record_code_tool(
        "grep_code",
        f"pattern={keyword}",
        lambda: grep_code(repo_root, keyword),
        collected,
        node_name="code_agent_node",
    )
    if grep_ev is None:
        return
    first_hit_file = None
    for line in grep_ev.raw_snippet.splitlines():
        if ":" in line and not line.startswith("未匹配"):
            candidate = line.split(":", 1)[0].strip()
            if candidate:
                first_hit_file = candidate
                break
    if first_hit_file:
        norm = first_hit_file.replace("\\", "/")
        _record_code_tool(
            "read_file_window",
            f"file={norm}:1-120",
            lambda: read_file_window(repo_root, norm, 1, 120),
            collected,
            node_name="code_agent_node",
        )
        if norm not in read_files:
            read_files.append(norm)


async def code_agent_node(
    state: ProbeWorkerState, runtime: Runtime[AutoDevContext]
) -> dict:
    """ReAct 推理节点：结合 RepoMap 与工具观察结果决定继续调用工具或结束源码探查."""
    ctx = runtime.context
    repo_root = ctx["repo_root"]
    question = state.get("target_question", "")
    repo_map = state.get("repo_map", "")
    intent: TaskIntent = state.get("intent") or "bugfix"
    worker_messages = list(state.get("worker_messages", []))
    existing_evidences = list(state.get("evidences", []))

    if not llm_client_manager.has_valid_key():
        collected: list[Evidence] = []
        read_files: list[str] = []
        _fallback_code_probe(repo_root, question, collected, read_files)
        return {
            "evidences": collected,
            "chat_fnames": read_files,
            "worker_messages": [AIMessage(content="未检测到有效 LLM Key，已执行确定性源码探查兜底。")],
        }

    ai_turns = sum(1 for msg in worker_messages if isinstance(msg, AIMessage))
    if ai_turns >= MAX_CODE_PROBE_TURNS:
        if not existing_evidences:
            collected = []
            read_files = []
            _fallback_code_probe(repo_root, question, collected, read_files)
            return {
                "evidences": collected,
                "chat_fnames": read_files,
                "worker_messages": [AIMessage(content="达到最大探查轮次，已补充确定性兜底证据并结束。")],
            }
        return {
            "worker_messages": [AIMessage(content="已达到最大源码探查轮次，结束本轮自主探查。")]
        }

    tools = _build_code_probe_tools(repo_root, [], [])
    llm_with_tools = llm_client_manager.get_llm(temperature=0.0).bind_tools(tools)
    messages = _build_code_probe_messages(
        repo_map=repo_map,
        question=question,
        intent=intent,
        worker_messages=worker_messages,
    )

    try:
        ai_msg = await llm_with_tools.ainvoke(messages)
    except Exception as exc:
        if not existing_evidences:
            collected = []
            read_files = []
            _fallback_code_probe(repo_root, question, collected, read_files)
            return {
                "evidences": collected,
                "chat_fnames": read_files,
                "worker_messages": [
                    AIMessage(content=f"模型探查调用异常（{exc}），已执行确定性源码探查兜底。")
                ],
            }
        return {
            "worker_messages": [
                AIMessage(content=f"模型探查调用异常（{exc}），保留已采集证据并结束探查。")
            ]
        }

    has_tool_calls = bool(getattr(ai_msg, "tool_calls", None))
    if not has_tool_calls and not existing_evidences:
        collected = []
        read_files = []
        _fallback_code_probe(repo_root, question, collected, read_files)
        return {
            "evidences": collected,
            "chat_fnames": read_files,
            "worker_messages": [ai_msg],
        }

    return {"worker_messages": [ai_msg]}


async def code_tools_node(
    state: ProbeWorkerState, runtime: Runtime[AutoDevContext]
) -> dict:
    """ReAct 工具执行节点：执行上一轮 AIMessage 发起的只读源码工具调用并返回 ToolMessage."""
    ctx = runtime.context
    repo_root = ctx["repo_root"]
    worker_messages = list(state.get("worker_messages", []))
    last_msg = worker_messages[-1] if worker_messages else None
    tool_calls = getattr(last_msg, "tool_calls", None) or []

    collected: list[Evidence] = []
    read_files: list[str] = []
    tools = _build_code_probe_tools(repo_root, collected, read_files)
    tool_map = {t.name: t for t in tools}

    tool_messages: list[ToolMessage] = []
    for tc in tool_calls:
        t_name = tc.get("name", "")
        t_args = tc.get("args") or {}
        t_id = tc.get("id") or t_name
        target_tool = tool_map.get(t_name)
        if target_tool is None:
            obs = f"未知工具: {t_name}"
        else:
            try:
                obs = str(target_tool.invoke(t_args))
            except Exception as exc:
                obs = f"工具参数错误: {exc}"
        tool_messages.append(ToolMessage(content=obs, tool_call_id=t_id, name=t_name))

    return {
        "worker_messages": tool_messages,
        "evidences": collected,
        "chat_fnames": read_files,
    }

# 这个节点同时是db和code的工具节点
def _route_react_tools(
    state: ProbeWorkerState,
    *,
    tools_node: str,
    max_turns: int,
) -> str:
    """通用 ReAct 条件边：存在 tool_calls 且未达轮次上限时路由至工具执行节点."""
    worker_messages = state.get("worker_messages", [])
    if not worker_messages:
        return END
    last_msg = worker_messages[-1]
    ai_turns = sum(1 for msg in worker_messages if isinstance(msg, AIMessage))
    if (
        isinstance(last_msg, AIMessage)
        and getattr(last_msg, "tool_calls", None)
        and ai_turns <= max_turns
    ):
        return tools_node
    return END


def route_code_probe_tools(
    state: ProbeWorkerState,
) -> Literal["code_tools_node", "__end__"]:
    """ReAct 条件边：若模型产生了 tool_calls 且未超出轮次上限，路由至 code_tools_node."""
    return _route_react_tools(  # type: ignore[return-value]
        state,
        tools_node="code_tools_node",
        max_turns=MAX_CODE_PROBE_TURNS,
    )


def build_code_probe_subgraph():
    """构建自主源码探查 ReAct 子图（code_agent_node <-> code_tools_node）."""
    builder = StateGraph(
        state_schema=ProbeWorkerState,
        context_schema=AutoDevContext,
        output_schema=CodeProbeOutputState,
    )
    builder.add_node("code_agent_node", code_agent_node)
    builder.add_node("code_tools_node", code_tools_node)

    builder.add_edge(START, "code_agent_node")
    builder.add_conditional_edges(
        "code_agent_node",
        route_code_probe_tools,
        {
            "code_tools_node": "code_tools_node",
            END: END,
        },
    )
    builder.add_edge("code_tools_node", "code_agent_node")
    return builder.compile()


code_probe_subgraph = build_code_probe_subgraph()
code_probe_worker = code_probe_subgraph


async def log_probe_worker(
    state: ProbeWorkerState, runtime: Runtime[AutoDevContext]
) -> dict:
    """日志报错探查 Worker：配置了 ES 则直读 ES，未配置则降级扫描本地日志文件."""
    ctx = runtime.context
    question = state.get("target_question", "")
    keyword = _extract_search_keyword(question)
    trace_match = re.search(r"\b(tr_[a-zA-Z0-9_\-]+)\b", question)
    trace_id = trace_match.group(1) if trace_match else None

    _emit_custom(
        "tool_start",
        {
            "tool_name": "probe_runtime_logs",
            "args_summary": f"es_index={ctx.get('es_log_index') or 'local_fallback'}",
            "node_name": "log_probe_worker",
            "probe_type": "log_error",
        },
    )
    es_cfg = ctx["datasource_registry"].get_es_datasource()
    evidences, gaps = await probe_runtime_logs(
        repo_root=ctx["repo_root"],
        keyword=keyword,
        runtime_log_es_repo=ctx.get("runtime_log_es_repo"),
        es_log_index=ctx.get("es_log_index") or (es_cfg.es_index_name if es_cfg else None),
        datasource_id=es_cfg.ds_id if es_cfg else None,
        log_path=ctx.get("log_path"),
        trace_id=trace_id,
    )
    for ev in evidences:
        _emit_custom(
            "evidence_added",
            {
                "evidence_id": ev.id,
                "evidence_type": ev.evidence_type,
                "summary": ev.summary,
            },
        )
    _emit_custom(
        "tool_end",
        {
            "tool_name": "probe_runtime_logs",
            "status": "ok",
            "duration_ms": 0,
            "summary": f"日志取证 {len(evidences)} 条，gaps={len(gaps)}",
        },
    )
    return {"evidences": evidences, "gaps": gaps}

MAX_DB_PROBE_TURNS = 4


async def _record_db_tool(
    tool_name: str,
    args_summary: str,
    runner,
    collected: list[Evidence],
    node_name: str = "db_tools_node",
) -> tuple[Evidence | None, str | None]:
    """统一发射数据库探查工具事件并记录标准 db_query 证据，返回 `(Evidence, err_msg)`."""
    _emit_custom(
        "tool_start",
        {
            "tool_name": tool_name,
            "args_summary": args_summary,
            "node_name": node_name,
            "probe_type": "db_query",
        },
    )
    try:
        ev: Evidence | None = await runner()
    except Exception as exc:
        err_text = str(exc)
        _emit_custom(
            "tool_end",
            {
                "tool_name": tool_name,
                "status": "error",
                "duration_ms": 0,
                "summary": f"{tool_name} 执行失败: {err_text}",
            },
        )
        return None, err_text

    if ev is None:
        _emit_custom(
            "tool_end",
            {
                "tool_name": tool_name,
                "status": "empty",
                "duration_ms": 0,
                "summary": f"{tool_name} 未返回有效结果",
            },
        )
        return None, None

    collected.append(ev)
    _emit_custom(
        "evidence_added",
        {
            "evidence_id": ev.id,
            "evidence_type": ev.evidence_type,
            "summary": ev.summary,
        },
    )
    _emit_custom(
        "tool_end",
        {
            "tool_name": tool_name,
            "status": "ok",
            "duration_ms": 0,
            "summary": ev.summary,
        },
    )
    return ev, None


def _build_db_probe_tools(
    registry,
    default_query: str,
    code_repo_map: str,
    collected: list[Evidence],
    generated_db_maps: list[str] | None = None,
    cache_dir: str | Path | None = None,
):
    """构建供 DB ReAct Agent 自主调度的只读数据库探查工具."""

    @tool
    async def get_db_repo_map(
        focus_query: str = "",
        max_tokens: int = 1200,
        ds_id: str | None = None,
    ) -> str:
        """生成基于 PageRank 的数据库表拓扑大纲 [DB_REPO_MAP]（含列类型、主键、物理/推断外键与索引）.

        当库中表很多或需要理清多表 JOIN 关系时，优先调用此工具；可用 `focus_query` 指定关注的表名/业务关键词。
        """
        effective_query = focus_query.strip() or default_query

        async def _run_db_map() -> Evidence | None:
            ev, rendered_map = await generate_db_repo_map(
                registry,
                query=effective_query,
                repo_map=code_repo_map,
                max_tokens=max_tokens,
                ds_id=ds_id,
                cache_dir=cache_dir,
            )
            if rendered_map and generated_db_maps is not None:
                generated_db_maps.append(rendered_map)
            return ev

        ev, err = await _record_db_tool(
            "build_db_repo_map",
            f"focus={effective_query[:60]}, max_tokens={max_tokens}",
            _run_db_map,
            collected,
        )
        if err:
            return f"生成数据库拓扑大纲失败: {err}"
        if ev is None:
            return "未获取到数据库表结构信息（库中无可用表或未配置数据库数据源）"
        rendered = generated_db_maps[-1] if generated_db_maps else ev.raw_snippet
        return f"[{ev.id}] {ev.summary}\n{rendered}"

    @tool
    async def inspect_table_detail(
        table_names: list[str] | None = None,
        ds_id: str | None = None,
    ) -> str:
        """获取指定表（如 ['orders', 'order_items']）的完整列定义、主键、外键与索引 JSON."""
        tbl_arg = ",".join(table_names) if table_names else "all"
        ev, err = await _record_db_tool(
            "inspect_table_schema",
            f"tables={tbl_arg}",
            lambda: inspect_table_schema(registry, ds_id=ds_id, table_names=table_names),
            collected,
        )
        if err:
            return f"查询表详情失败: {err}"
        if ev is None:
            return f"未找到匹配的表结构: {tbl_arg}"
        return f"[{ev.id}] {ev.summary}\n{ev.raw_snippet}"

    @tool
    async def run_readonly_sql(
        sql: str,
        ds_id: str | None = None,
        limit: int = 30,
    ) -> str:
        """执行单条只读 SELECT / SHOW / EXPLAIN 语句，用于核对列/数据异常、复现报错 SQL 或排查索引问题."""
        safe_limit = min(max(1, limit), 50)
        ev, err = await _record_db_tool(
            "execute_readonly_sql",
            f"sql={sql[:80]}",
            lambda: execute_readonly_sql(registry, sql=sql, ds_id=ds_id, limit=safe_limit),
            collected,
        )
        if err:
            return (
                f"SQL 执行失败: {err}\n"
                "请结合 [DB_REPO_MAP] 检查表名、字段名或只读语句约束；"
                "若该报错本身能直接印证代码与库表不一致，可直接汇总为诊断发现。"
            )
        if ev is None:
            return "未配置数据库数据源，无法执行只读 SQL。"
        return f"[{ev.id}] {ev.summary}\n{ev.raw_snippet}"

    return [get_db_repo_map, inspect_table_detail, run_readonly_sql]


def _build_db_probe_messages(
    *,
    code_repo_map: str,
    db_repo_map: str,
    question: str,
    intent: TaskIntent,
    datasource_ids: list[str],
    worker_messages: list[BaseMessage],
) -> list[BaseMessage]:
    """组装自主数据库探查 ReAct Agent 的提示词上下文与对话历史（代码与数据库大纲分离）."""
    ds_desc = ", ".join(datasource_ids) if datasource_ids else "已挂载数据库"
    if intent == "feature":
        mission = (
            "当前任务属于功能创建/增量改造：请先调用 `get_db_repo_map` 获取高相关库表的 [DB_REPO_MAP]（含主外键/关联与索引），"
            "确认需要关联或新建字段的表结构与约束。"
        )
    else:
        mission = (
            "当前任务属于故障排查/只读诊断：请先调用 `get_db_repo_map` 查看表间关联与字段定义，"
            "必要时结合 `inspect_table_detail` 或自主构造安全的只读 `run_readonly_sql`（仅 SELECT / SHOW / EXPLAIN）"
            "去用户库中验证字段是否存在、排查异常数据行或复现问题 SQL 的真实执行结果/报错。"
        )

    chunks = ChatChunks(
        system_prompt=(
            f"你是自主数据库探查 ReAct Agent（当前数据源: {ds_desc}）。{mission}\n"
            "执行规范：\n"
            "1. 你处于多轮 ReAct 循环中：拿到 `get_db_repo_map` 生成的独立 `[DB_REPO_MAP]` 后，可按关联关系继续调用 `run_readonly_sql` 或 `inspect_table_detail` 做深度核验；\n"
            "2. 所有 SQL 必须严格只读（单条 SELECT / SHOW / EXPLAIN 并带合理 LIMIT），严禁猜未确认过的表名与列名；\n"
            f"3. 最多探索 {MAX_DB_PROBE_TURNS} 轮；获取足够库表与验证证据后请停止调工具并简短总结结论。"
        ),
        repo_map=code_repo_map,
        db_repo_map=db_repo_map,
        user_query=question,
    )
    return [*chunks.to_messages(), *worker_messages]


async def _fallback_db_probe(
    registry,
    *,
    question: str,
    code_repo_map: str,
    collected: list[Evidence],
    gaps: list[str],
    cache_dir: str | Path | None = None,
) -> str:
    """无有效 LLM Key 或模型调用异常时的确定性 DBRepoMap 兜底取证，返回生成的 DBRepoMap 文本."""
    rendered_holder: list[str] = []

    async def _run_fallback_map() -> Evidence | None:
        ev_map, rendered = await generate_db_repo_map(
            registry, query=question, repo_map=code_repo_map, cache_dir=cache_dir
        )
        if rendered:
            rendered_holder.append(rendered)
        return ev_map

    ev, err = await _record_db_tool(
        "build_db_repo_map",
        f"fallback_query={question[:60]}",
        _run_fallback_map,
        collected,
        node_name="db_agent_node",
    )
    if err:
        gaps.append(f"数据库探查失败: {err}")
        return ""
    if ev is None:
        ev2, err2 = await _record_db_tool(
            "inspect_table_schema",
            "fallback_all",
            lambda: inspect_table_schema(registry),
            collected,
            node_name="db_agent_node",
        )
        if err2:
            gaps.append(f"数据库探查失败: {err2}")
        elif ev2 is None:
            gaps.append("数据库未反射出可用表结构。")
        return ""
    return rendered_holder[-1] if rendered_holder else ""


async def db_agent_node(
    state: ProbeWorkerState, runtime: Runtime[AutoDevContext]
) -> dict:
    """数据库 ReAct 推理节点：结合独立 DBRepoMap 与 SQL 观察结果决定继续取证或结束探查."""
    ctx = runtime.context
    registry = ctx["datasource_registry"]
    if not registry.has_db_datasources():
        return {"evidences": [], "gaps": []}

    question = state.get("target_question", "")
    code_repo_map = state.get("repo_map", "")
    db_repo_map = state.get("db_repo_map", "")
    cache_dir = Path(ctx["repo_root"]) / ".auto_dev_cache" / "db_repo_map"
    intent: TaskIntent = state.get("intent") or "bugfix"
    worker_messages = list(state.get("worker_messages", []))
    existing_evidences = list(state.get("evidences", []))

    if not llm_client_manager.has_valid_key():
        collected: list[Evidence] = []
        gaps: list[str] = []
        fallback_db_map = await _fallback_db_probe(
            registry,
            question=question,
            code_repo_map=code_repo_map,
            collected=collected,
            gaps=gaps,
            cache_dir=cache_dir,
        )
        out: dict[str, Any] = {
            "evidences": collected,
            "gaps": gaps,
            "worker_messages": [AIMessage(content="未检测到有效 LLM Key，已执行 DBRepoMap 确定性取证兜底。")],
        }
        if fallback_db_map:
            out["db_repo_map"] = fallback_db_map
        return out

    ai_turns = sum(1 for msg in worker_messages if isinstance(msg, AIMessage))
    if ai_turns >= MAX_DB_PROBE_TURNS:
        if not existing_evidences:
            collected = []
            gaps = []
            fallback_db_map = await _fallback_db_probe(
                registry,
                question=question,
                code_repo_map=code_repo_map,
                collected=collected,
                gaps=gaps,
                cache_dir=cache_dir,
            )
            out = {
                "evidences": collected,
                "gaps": gaps,
                "worker_messages": [AIMessage(content="达到数据库最大探查轮次，已补充 DBRepoMap 兜底证据并结束。")],
            }
            if fallback_db_map:
                out["db_repo_map"] = fallback_db_map
            return out
        return {
            "worker_messages": [AIMessage(content="已达到最大数据库探查轮次，结束本轮自主探查。")]
        }

    tools = _build_db_probe_tools(registry, question, code_repo_map, [])
    llm_with_tools = llm_client_manager.get_llm(temperature=0.0).bind_tools(tools)
    messages = _build_db_probe_messages(
        code_repo_map=code_repo_map,
        db_repo_map=db_repo_map,
        question=question,
        intent=intent,
        datasource_ids=list(ctx.get("active_datasource_ids", [])),
        worker_messages=worker_messages,
    )

    try:
        ai_msg = await llm_with_tools.ainvoke(messages)
    except Exception as exc:
        if not existing_evidences:
            collected = []
            gaps = []
            fallback_db_map = await _fallback_db_probe(
                registry,
                question=question,
                code_repo_map=code_repo_map,
                collected=collected,
                gaps=gaps,
                cache_dir=cache_dir,
            )
            out = {
                "evidences": collected,
                "gaps": gaps,
                "worker_messages": [
                    AIMessage(content=f"数据库探查模型调用异常（{exc}），已执行 DBRepoMap 确定性兜底。")
                ],
            }
            if fallback_db_map:
                out["db_repo_map"] = fallback_db_map
            return out
        return {
            "worker_messages": [
                AIMessage(content=f"数据库探查模型调用异常（{exc}），保留已采集证据并结束。")
            ]
        }

    has_tool_calls = bool(getattr(ai_msg, "tool_calls", None))
    if not has_tool_calls and not existing_evidences:
        collected = []
        gaps = []
        fallback_db_map = await _fallback_db_probe(
            registry,
            question=question,
            code_repo_map=code_repo_map,
            collected=collected,
            gaps=gaps,
            cache_dir=cache_dir,
        )
        out = {
            "evidences": collected,
            "gaps": gaps,
            "worker_messages": [ai_msg],
        }
        if fallback_db_map:
            out["db_repo_map"] = fallback_db_map
        return out

    return {"worker_messages": [ai_msg]}


async def db_tools_node(
    state: ProbeWorkerState, runtime: Runtime[AutoDevContext]
) -> dict:
    """数据库 ReAct 工具执行节点：执行 DBRepoMap（写回 state.db_repo_map）、表详情及只读 SELECT 工具调用."""
    ctx = runtime.context
    registry = ctx["datasource_registry"]
    question = state.get("target_question", "")
    code_repo_map = state.get("repo_map", "")
    worker_messages = list(state.get("worker_messages", []))
    last_msg = worker_messages[-1] if worker_messages else None
    tool_calls = getattr(last_msg, "tool_calls", None) or []

    collected: list[Evidence] = []
    # generated_db_maps是列表的原因就是为了引用传递
    generated_db_maps: list[str] = []
    cache_dir = Path(ctx["repo_root"]) / ".auto_dev_cache" / "db_repo_map"
    tools = _build_db_probe_tools(
        registry,
        question,
        code_repo_map,
        collected,
        generated_db_maps=generated_db_maps,
        cache_dir=cache_dir,
    )
    tool_map = {t.name: t for t in tools}

    tool_messages: list[ToolMessage] = []
    for tc in tool_calls:
        t_name = tc.get("name", "")
        t_args = tc.get("args") or {}
        t_id = tc.get("id") or t_name
        target_tool = tool_map.get(t_name)
        if target_tool is None:
            obs = f"未知数据库工具: {t_name}"
        else:
            try:
                # 此时如果工具调用了get_db_repo_map,会运行 get_db_repo_map -> _run_db_map() 去生成大纲并执行 generated_db_maps.append(rendered_map)。
                # list是可变对象,传的是引用generated_db_maps直接拿到repomap
                obs = str(await target_tool.ainvoke(t_args))
            except Exception as exc:
                obs = f"数据库工具参数错误: {exc}"
        tool_messages.append(ToolMessage(content=obs, tool_call_id=t_id, name=t_name))

    result: dict[str, Any] = {
        "worker_messages": tool_messages,
        "evidences": collected,
    }
    if generated_db_maps:
        result["db_repo_map"] = generated_db_maps[-1]
    return result


def route_db_probe_tools(
    state: ProbeWorkerState,
) -> Literal["db_tools_node", "__end__"]:
    """ReAct 条件边：若模型产生了 DB tool_calls 且未达上限，路由至 db_tools_node."""
    return _route_react_tools(  # type: ignore[return-value]
        state,
        tools_node="db_tools_node",
        max_turns=MAX_DB_PROBE_TURNS,
    )


def build_db_probe_subgraph():
    """构建自主数据库探查 ReAct 子图（db_agent_node <-> db_tools_node）."""
    builder = StateGraph(
        state_schema=ProbeWorkerState,
        context_schema=AutoDevContext,
        output_schema=DBProbeOutputState,
    )
    builder.add_node("db_agent_node", db_agent_node)
    builder.add_node("db_tools_node", db_tools_node)

    builder.add_edge(START, "db_agent_node")
    builder.add_conditional_edges(
        "db_agent_node",
        route_db_probe_tools,
        {
            "db_tools_node": "db_tools_node",
            END: END,
        },
    )
    builder.add_edge("db_tools_node", "db_agent_node")
    return builder.compile()


db_probe_subgraph = build_db_probe_subgraph()
db_probe_worker = db_probe_subgraph


async def merge_evidences_node(
    state: EvidenceSubState, runtime: Runtime[AutoDevContext]
) -> dict:
    """对并发探针签发的证据按 ID 去重汇总."""
    seen: set[str] = set()
    unique: list[Evidence] = []
    for ev in state.get("evidences", []):
        if ev.id not in seen:
            seen.add(ev.id)
            unique.append(ev)
    return {"evidences": unique}


def build_evidence_subgraph():
    """构建证据探查子图."""
    builder = StateGraph(
        state_schema=EvidenceSubState,
        context_schema=AutoDevContext,
    )
    builder.add_node("plan_probes_node", plan_probes_node)
    builder.add_node("cache_probe_worker", cache_probe_worker)
    builder.add_node("code_probe_worker", code_probe_subgraph)
    builder.add_node("log_probe_worker", log_probe_worker)
    builder.add_node("db_probe_worker", db_probe_subgraph)
    builder.add_node("merge_evidences_node", merge_evidences_node)

    builder.add_edge(START, "plan_probes_node")
    builder.add_conditional_edges(
        "plan_probes_node",
        route_probes_edge,
        [
            "cache_probe_worker",
            "code_probe_worker",
            "log_probe_worker",
            "db_probe_worker",
        ],
    )
    builder.add_edge("cache_probe_worker", "merge_evidences_node")
    builder.add_edge("code_probe_worker", "merge_evidences_node")
    builder.add_edge("log_probe_worker", "merge_evidences_node")
    builder.add_edge("db_probe_worker", "merge_evidences_node")
    builder.add_edge("merge_evidences_node", END)
    return builder.compile()


evidence_probe_subgraph = build_evidence_subgraph()
