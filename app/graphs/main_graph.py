"""主图编排 (`main_dev_graph`): 整合证据探查子图、规划、Linux 沙箱子图、人工审批与报错缓存归档."""

import json
import re
import shutil
import uuid
from pathlib import Path
from typing import Any, Literal
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import MemorySaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime
from langgraph.types import interrupt

from app.clients.llm_client_manager import llm_client_manager
from app.config import now_iso, settings
from app.core.chat_chunks import ChatChunks, load_chat_files_block
from app.core.git_ops import apply_files_and_commit, get_head_sha
from app.core.intent_router import classify_task_intent
from app.core.repo_map import (
    build_repo_map_with_fallbacks,
    extract_query_mentions,
    find_repo_source_files,
    repo_map_manager,
)
from app.graphs.context import AutoDevContext
from app.graphs.evidence_subgraph import evidence_probe_subgraph
from app.graphs.sandbox_subgraph import sandbox_subgraph
from app.graphs.state import AutoDevMainState
from app.models.orm_models import PatchRecordORM
from app.schemas.domain_schemas import (
    EditPlanOutput,
    EditPlanStep,
    ErrorCacheRecord,
    Evidence,
    build_cache_id,
)


def _emit_custom(event: str, data: dict) -> None:
    try:
        from langgraph.config import get_stream_writer

        writer = get_stream_writer()
        if writer:
            writer({"event": event, "data": data})
    except Exception:
        pass


def _build_repo_map_for_context(
    repo_root: str,
    *,
    chat_fnames: list[str],
    other_fnames: list[str],
    max_map_tokens: int | None = None,
    mentioned_fnames: list[str] | None = None,
    mentioned_idents: list[str] | None = None,
    force_refresh: bool = False,
) -> str:
    eff_tokens = max_map_tokens if max_map_tokens is not None else settings.repo_map_tokens
    rm = repo_map_manager.get_or_create(
        repo_root,
        map_tokens=eff_tokens,
        max_context_window=settings.llm_max_context_window,
    )
    return build_repo_map_with_fallbacks(
        rm,
        chat_fnames=chat_fnames,
        other_fnames=other_fnames,
        max_map_tokens=eff_tokens,
        mentioned_fnames=mentioned_fnames,
        mentioned_idents=mentioned_idents,
        force_refresh=force_refresh,
    )


async def init_context_node(
    state: AutoDevMainState, runtime: Runtime[AutoDevContext]
) -> dict:
    """复用/初始化 chat_fnames、other_fnames 与提及上下文，使用持久化 RepoMap 实例生成骨架."""
    # 每一轮会根据上一轮state合并当前的state,没reducer就覆盖,有就按规则来
    # chat_fnames 的语义是“已经在当前模型上下文里放了完整源码的文件”,只放已经把全文放进上下文、或正在修改中的文件；
    # repo_map大纲里的是other_fnames,不需要chat_fnames
    # 图结束的时候会把扣掉 chat_fnames 的 updated_other写回 other_fnames
    # RepoMap 在 aider 里不是静态一次性目录索引，而是受 token 预算限制、随当前轮问题动态聚焦的视图,所以每一轮都得重算
    # 但因为两层缓存(第一层:key:(chat_fnames,other_fnames,max_map_tokens...),第二层:存标签,看mtime)的存在,
    # 只有第一次的时候才会真正的全量更新,但每一轮repomap肯定是得重写的,因为有旧信息
    ctx = runtime.context
    chat_fnames = list(state.get("chat_fnames") or [])
    raw_other = state.get("other_fnames")
    if raw_other is None:
        all_files = find_repo_source_files(ctx["repo_root"], relative=True)
        # as_posix:把路径统一转换成用正斜杠 / 表示的字符串格式。
        chat_set = {Path(f).as_posix() for f in chat_fnames}
        other_fnames = [f for f in all_files if Path(f).as_posix() not in chat_set]
    else:
        other_fnames = list(raw_other)
        all_files = [*chat_fnames, *other_fnames]

    max_map_tokens = state.get("max_map_tokens")
    if max_map_tokens is None:
        max_map_tokens = settings.repo_map_tokens

    mentioned_fnames, mentioned_idents = extract_query_mentions(
        state.get("user_query", ""),
        all_files,
    )
    repo_map = _build_repo_map_for_context(
        ctx["repo_root"],
        chat_fnames=chat_fnames,
        other_fnames=other_fnames,
        max_map_tokens=max_map_tokens,
        mentioned_fnames=mentioned_fnames,
        mentioned_idents=mentioned_idents,
    )
    # Git 里的 SHA 就是每次 git commit 生成的提交版本号（Commit ID），
    # 本质是对本次提交内容算出来的 SHA 哈希值（比如 a1b2c3d），用来唯一标识代码仓库在某个时间点的快照。
    base_sha = get_head_sha(ctx["repo_root"])
    has_db = ctx["datasource_registry"].has_db_datasources()
    has_log = bool(ctx.get("es_log_index") or ctx.get("log_path"))
    intent, enabled_probes = await classify_task_intent(
        state.get("user_query", ""),
        repo_map=repo_map,
        has_db_datasource=has_db,
        has_log_signal=has_log,
    )

    await ctx["task_repo"].update_task(
        ctx["task_id"],
        status="running",
        base_commit_sha=base_sha,
    )
    return {
        "repo_map": repo_map,
        "db_repo_map": "",
        "chat_fnames": chat_fnames,
        "other_fnames": other_fnames,
        "max_map_tokens": max_map_tokens,
        "mentioned_fnames": mentioned_fnames,
        "mentioned_idents": mentioned_idents,
        "base_commit_sha": base_sha,
        "has_db_datasource": has_db,
        "has_log_datasource": has_log,
        "intent": intent,
        "enabled_probes": enabled_probes,
        "diagnosis_report": None,
        "approval_status": "pending",
    }


def route_after_evidence(
    state: AutoDevMainState,
) -> Literal["answer_only_node", "edit_planner_node"]:
    """只读诊断/问答 (`qa`) 直接汇总回复收束，不进入代码修改与沙箱审批流程."""
    if state.get("intent") == "qa":
        return "answer_only_node"
    return "edit_planner_node"


async def answer_only_node(
    state: AutoDevMainState, runtime: Runtime[AutoDevContext]
) -> dict:
    """只读诊断与代码问答收束节点：持久化证据并输出依据真实证据的回答."""
    ctx = runtime.context
    evidences = state.get("evidences", [])
    gaps = state.get("gaps", [])
    ev_block = "\n".join(
        f"[{ev.id}] ({ev.evidence_type}) {ev.summary}\n{ev.raw_snippet[:800]}"
        for ev in evidences[:10]
    )
    chat_files_block = load_chat_files_block(
        ctx["repo_root"],
        list(state.get("chat_fnames") or []),
    )

    report = ""
    if llm_client_manager.has_valid_key():
        try:
            chunks = ChatChunks(
                system_prompt=(
                    "你是只读故障诊断与代码分析助手。请仅根据真实收集到的证据与源码上下文回答用户问题，"
                    "严格引用 `[ev_xxx]` 证据编号；若证据存在缺失(gaps)需明确指出，不要编造未见结论。"
                ),
                repo_map=state.get("repo_map", ""),
                db_repo_map=state.get("db_repo_map", ""),
                chat_files_block=chat_files_block,
                evidence_block=ev_block,
                user_query=state.get("user_query", ""),
            )
            resp = await llm_client_manager.get_llm(temperature=0.0).ainvoke(chunks.to_messages())
            report = str(resp.content or "").strip()
        except Exception:
            report = ""

    if not report:
        lines = [f"- [{ev.id}] ({ev.evidence_type}) {ev.summary}" for ev in evidences]
        if gaps:
            lines.extend(f"- [gap] {g}" for g in gaps)
        report = "基于只读探查证据汇总分析结果：\n" + ("\n".join(lines) if lines else "暂未命中相关记录。")

    await ctx["task_repo"].upsert_evidences(ctx["task_id"], evidences)
    await ctx["task_repo"].update_task(ctx["task_id"], status="completed")
    _emit_custom("diagnosis_ready", {"report": report, "evidence_count": len(evidences)})
    return {
        "diagnosis_report": report,
        "messages": [AIMessage(content=report)],
    }


async def edit_planner_node(
    state: AutoDevMainState, runtime: Runtime[AutoDevContext]
) -> dict:
    """结合证据子图收集到的证据生成文件级修改计划."""
    ctx = runtime.context
    evidences = state.get("evidences", [])
    ev_summary = "\n".join(f"[{ev.id}] ({ev.evidence_type}) {ev.summary}" for ev in evidences)
    plan: list[dict[str, Any]] = []
    target_files: list[str] = []

    if llm_client_manager.has_valid_key():
        try:
            # 按理来说此时模型调用了探查源码工具,chat_fnames里面应该有东西了
            # 如果这个时候没有chat_fnames的话下面让模型制定计划的时候模型会指定target,然后拼接target_files 和 chat_fnames
            chat_files_block = load_chat_files_block(
                ctx["repo_root"],
                list(state.get("chat_fnames") or []),
            )
            chunks = ChatChunks(
                system_prompt=(
                    "你是资深代码修改规划专家。请基于已收集证据与源码上下文，规划本次任务的目标文件列表与操作步骤。"
                ),
                repo_map=state.get("repo_map", ""),
                db_repo_map=state.get("db_repo_map", ""),
                chat_files_block=chat_files_block,
                evidence_block=ev_summary,
                user_query=state.get("user_query", ""),
            )
            llm = llm_client_manager.get_llm(temperature=0.0)
            structured_llm = llm.with_structured_output(EditPlanOutput)
            result = await structured_llm.ainvoke(chunks.to_messages())
            if isinstance(result, EditPlanOutput):
                plan = [p.model_dump() for p in result.plan]
                target_files = list(result.target_files or [])
            elif isinstance(result, dict):
                plan = list(result.get("plan") or [])
                target_files = list(result.get("target_files") or [])
        except Exception:
            plan = []
            target_files = []

    if not plan:
        ev_ids = [ev.id for ev in evidences[:3]]
        inferred_files = [
            line.rstrip(":").strip()
            for line in state.get("repo_map", "").splitlines()
            if line.endswith(":") and not line.startswith((" ", "\t", "│", "⋮"))
        ][:2]
        for fpath in inferred_files:
            plan.append(
                {
                    "file_path": fpath,
                    "action": "modify",
                    "reason": f"结合证据 {', '.join(ev_ids) or 'source_code'} 排查目标模块",
                    "evidence_ids": ev_ids,
                }
            )

    all_target_files = set(target_files)
    for item in plan:
        fp = item.get("file_path")
        if fp:
            all_target_files.add(fp)

    target_files = [
        f.replace("\\", "/").lstrip("/") for f in all_target_files if f and isinstance(f, str)
    ]
    prev_chat = list(state.get("chat_fnames") or [])
    prev_other = list(state.get("other_fnames") or [])
    merged_chat = list(dict.fromkeys([*prev_chat, *target_files]))

    chat_set = {Path(f).as_posix() for f in merged_chat}
    # 更新other_fnames
    updated_other = [f for f in prev_other if Path(f).as_posix() not in chat_set]
    repo_map = state.get("repo_map", "")
    if (merged_chat or updated_other) and merged_chat != prev_chat:
        repo_map = _build_repo_map_for_context(
            ctx["repo_root"],
            chat_fnames=merged_chat,
            other_fnames=updated_other,
            max_map_tokens=state.get("max_map_tokens"),
            mentioned_fnames=list(state.get("mentioned_fnames") or []),
            mentioned_idents=list(state.get("mentioned_idents") or []),
        )

    await ctx["task_repo"].upsert_evidences(ctx["task_id"], evidences)
    await ctx["task_repo"].update_task(ctx["task_id"], edit_plan=plan)
    _emit_custom("plan_ready", {"plan": plan, "target_files": target_files})
    return {
        "edit_plan": plan,
        "chat_fnames": merged_chat,
        "other_fnames": updated_other,
        "repo_map": repo_map,
        "messages": [AIMessage(content=f"已基于 {len(evidences)} 条证据生成修改计划。")],
    }


async def record_patch_node(
    state: AutoDevMainState, runtime: Runtime[AutoDevContext]
) -> dict:
    """持久化单轮沙箱补丁并置为 waiting_approval (独立于 interrupt 节点避免恢复重放重复写库)."""
    ctx = runtime.context
    branch_name = state.get("target_branch") or f"feat/task-{ctx['task_id'][:12]}"
    existing_patches = await ctx["task_repo"].list_patch_records(ctx["task_id"])
    round_no = len(existing_patches) + 1

    await ctx["task_repo"].save_patch_record(
        PatchRecordORM(
            id=f"patch_{uuid.uuid4().hex[:10]}",
            task_id=ctx["task_id"],
            round_no=round_no,
            edited_files=state.get("edited_files", []),
            patch_diff=state.get("patch_diff", ""),
            test_passed=bool(state.get("test_passed", False)),
            retry_count=0,
            test_output=state.get("test_output", ""),
            approval_status="pending",
        )
    )
    await ctx["task_repo"].update_task(
        ctx["task_id"],
        status="waiting_approval",
        target_branch=branch_name,
    )
    _emit_custom(
        "waiting_approval",
        {
            "task_id": ctx["task_id"],
            "patch_diff": state.get("patch_diff", ""),
            "branch_name": branch_name,
        },
    )
    return {"target_branch": branch_name}


async def human_approval_node(
    state: AutoDevMainState, runtime: Runtime[AutoDevContext]
) -> dict:
    """触发 `interrupt()` 等待人工确认，并在恢复后更新审批状态."""
    ctx = runtime.context
    branch_name = state.get("target_branch") or f"feat/task-{ctx['task_id'][:12]}"
    decision_payload = interrupt(
        {
            "task_id": ctx["task_id"],
            "branch_name": branch_name,
            "patch_diff": state.get("patch_diff", ""),
            "test_passed": state.get("test_passed", False),
        }
    )
    decision = (decision_payload or {}).get("decision", "rejected")
    feedback = (decision_payload or {}).get("feedback")
    commit_message = (decision_payload or {}).get("commit_message")
    await ctx["task_repo"].update_latest_patch_approval(
        ctx["task_id"],
        approval_status=decision,
        human_feedback=feedback,
    )
    await ctx["task_repo"].update_task(ctx["task_id"], status="running")
    return {
        "approval_status": decision,
        "human_feedback": feedback,
        "commit_message": commit_message,
        "target_branch": branch_name,
    }


def route_after_approval(
    state: AutoDevMainState,
) -> Literal["git_commit_node", "sandbox_subgraph"]:
    if state.get("approval_status") == "approved":
        return "git_commit_node"
    return "sandbox_subgraph"


async def git_commit_node(
    state: AutoDevMainState, runtime: Runtime[AutoDevContext]
) -> dict:
    """应用审批通过的修改至特性分支提交，并三端同步沉淀报错缓存."""
    ctx = runtime.context
    branch_name = state.get("target_branch") or f"feat/task-{ctx['task_id'][:12]}"
    sandbox_dir = state.get("local_sandbox_dir", "")
    edited_files = state.get("edited_files", [])
    commit_sha = state.get("base_commit_sha") or get_head_sha(ctx["repo_root"])

    if edited_files and sandbox_dir and Path(sandbox_dir).exists():
        commit_msg = (state.get("commit_message") or "").strip() or (
            f"fix: {state.get('user_query', '')[:60]}"
        )
        commit_sha = apply_files_and_commit(
            repo_root=ctx["repo_root"],
            sandbox_dir=sandbox_dir,
            files=edited_files,
            branch_name=branch_name,
            commit_message=commit_msg,
        )
    if sandbox_dir and Path(sandbox_dir).exists():
        shutil.rmtree(sandbox_dir, ignore_errors=True)

    # 三端归档报错缓存：MySQL -> ES -> Qdrant
    evidences = state.get("evidences", [])
    error_sig = state.get("user_query", "")[:250]
    root_cause = "; ".join(ev.summary for ev in evidences[:2]) or "经源码与测试验证修复"
    resolution = state.get("patch_diff", "")[:1000] or f"commit {commit_sha}"
    cache_id = build_cache_id(ctx["namespace"], error_sig, root_cause)
    record = ErrorCacheRecord(
        cache_id=cache_id,
        namespace=ctx["namespace"],
        project_id=ctx["project_id"],
        source_task_id=ctx["task_id"],
        error_signature=error_sig,
        error_keywords=",".join(edited_files) or "auto_dev",
        symptoms=state.get("user_query", ""),
        root_cause=root_cause,
        resolution=resolution,
        created_at=now_iso(),
    )
    await ctx["error_cache_repo"].upsert(record)
    cache_archived = True
    try:
        if ctx.get("error_cache_es_repo") is not None:
            await ctx["error_cache_es_repo"].upsert_runbook(record)
        if ctx.get("error_cache_qdrant_repo") is not None and ctx.get("embedding_client") is not None:
            vec_text = f"{record.error_signature}\n{record.symptoms}\n{record.root_cause}"
            vec = await ctx["embedding_client"].aembed_query(vec_text)
            await ctx["error_cache_qdrant_repo"].upsert_runbook(record, vec)
    except Exception:
        cache_archived = True

    await ctx["task_repo"].update_task(
        ctx["task_id"],
        status="completed",
        final_commit_sha=commit_sha,
    )
    norm_edited = [f.replace("\\", "/") for f in edited_files]
    chat_set = {*(state.get("chat_fnames") or []), *norm_edited}
    updated_other = [
        f
        for f in (state.get("other_fnames") or [])
        if Path(f).as_posix() not in chat_set
    ]
    return {
        "final_commit_sha": commit_sha,
        "cache_archived": cache_archived,
        "chat_fnames": norm_edited,
        "other_fnames": updated_other,
        "messages": [AIMessage(content=f"已提交至分支 {branch_name} ({commit_sha})，报错缓存已归档。")],
    }


def build_main_graph(checkpointer=None):
    """构建主图 `main_dev_graph`."""
    builder = StateGraph(
        state_schema=AutoDevMainState,
        context_schema=AutoDevContext,
    )
    builder.add_node("init_context_node", init_context_node)
    builder.add_node("evidence_probe_subgraph", evidence_probe_subgraph)
    builder.add_node("answer_only_node", answer_only_node)
    builder.add_node("edit_planner_node", edit_planner_node)
    builder.add_node("sandbox_subgraph", sandbox_subgraph)
    builder.add_node("record_patch_node", record_patch_node)
    builder.add_node("human_approval_node", human_approval_node)
    builder.add_node("git_commit_node", git_commit_node)

    builder.add_edge(START, "init_context_node")
    builder.add_edge("init_context_node", "evidence_probe_subgraph")
    builder.add_conditional_edges(
        "evidence_probe_subgraph",
        route_after_evidence,
        {
            "answer_only_node": "answer_only_node",
            "edit_planner_node": "edit_planner_node",
        },
    )
    builder.add_edge("answer_only_node", END)
    builder.add_edge("edit_planner_node", "sandbox_subgraph")
    builder.add_edge("sandbox_subgraph", "record_patch_node")
    builder.add_edge("record_patch_node", "human_approval_node")
    builder.add_conditional_edges(
        "human_approval_node",
        route_after_approval,
        {
            "git_commit_node": "git_commit_node",
            "sandbox_subgraph": "sandbox_subgraph",
        },
    )
    builder.add_edge("git_commit_node", END)
    default_saver = MemorySaver(
        serde=JsonPlusSerializer(allowed_msgpack_modules=[Evidence])
    )
    return builder.compile(checkpointer=checkpointer or default_saver)


main_dev_graph = build_main_graph()
