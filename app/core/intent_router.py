"""入口意图分流器：区分故障修复 (`bugfix`)、功能创建 (`feature`) 与只读诊断问答 (`qa`) 并裁剪探针."""

import re
from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, Field

from app.clients.llm_client_manager import llm_client_manager
from app.schemas.domain_schemas import EvidenceType, TaskIntent

_QA_SIGNALS_RE = re.compile(
    r"(?:不(?:要|用)?(?:改|修改|编辑|动)代码|别改(?:代码)?|只(?:做)?(?:分析|排查|查看|诊断|解释)|仅(?:分析|诊断)|"
    r"什么原因|为什么会|解释一下|原理是什么|怎么运作|有哪些(?:接口|模块))",
    re.IGNORECASE,
)
_EDIT_OVERRIDE_RE = re.compile(
    r"(?:并修复|帮我修|顺便修|修复一下|改一下|修改|重构|创建|新增|新建|实现|编写)",
    re.IGNORECASE,
)
_FEATURE_SIGNALS_RE = re.compile(
    r"(?:创建|新建|新增|开发|实现|编写|添加|扩展|补充|重构|\b(?:create|add|implement|build|introduce)\b)",
    re.IGNORECASE,
)
_BUG_SIGNALS_RE = re.compile(
    r"(?:报错|异常|失败|崩溃|闪退|卡死|超时|排查|修复|解决|bug|error|exception|traceback|stacktrace|500|4\d\d|5\d\d|\bfix\b)",
    re.IGNORECASE,
)
_LOG_QUERY_RE = re.compile(
    r"(?:日志|log|trace|tr_[a-zA-Z0-9_\-]+|报错|异常|崩溃|超时|失败|error|exception|500)",
    re.IGNORECASE,
)
_DB_QUERY_RE = re.compile(
    r"(?:数据库|表结构|schema|sql|mysql|postgres|订单|字段|数据)",
    re.IGNORECASE,
)


class _IntentLLMOutput(BaseModel):
    intent: TaskIntent = Field(
        description=(
            "bugfix=需要排查或修复代码缺陷/报错; "
            "feature=需要新建文件、新增功能或增量开发改造(非修Bug); "
            "qa=只读故障诊断、日志排查或代码问答，明确不需要修改代码。"
        )
    )
    need_source_code: bool = Field(default=True, description="是否需要探查源码上下文")
    need_log_error: bool = Field(default=False, description="是否需要检索运行日志")
    need_error_cache: bool = Field(default=False, description="是否需要检索历史报错 Runbook 缓存")
    need_db_query: bool = Field(default=False, description="是否需要探查数据库 Schema/数据")


def select_probes_for_intent(
    intent: TaskIntent,
    *,
    user_query: str = "",
    has_db_datasource: bool = False,
    has_log_signal: bool = False,
    llm_probes: list[EvidenceType] | None = None,
) -> list[EvidenceType]:
    """根据意图与可用数据源确定启用的探针清单（保持 source_code 兜底）."""
    if llm_probes is not None:
        selected: list[EvidenceType] = []
        for p in llm_probes:
            if p == "db_query" and not has_db_datasource:
                continue
            if p not in selected:
                selected.append(p)
        if "source_code" not in selected:
            selected.insert(0, "source_code")
        return selected
    # 兜底
    if intent == "feature":
        probes: list[EvidenceType] = ["source_code"]
        if has_db_datasource:
            probes.append("db_query")
        return probes

    if intent == "qa":
        probes = ["source_code"]
        if has_log_signal or _LOG_QUERY_RE.search(user_query or ""):
            probes.extend(["error_cache", "log_error"])
        if has_db_datasource and (
            _DB_QUERY_RE.search(user_query or "") or _LOG_QUERY_RE.search(user_query or "")
        ):
            probes.append("db_query")
        return probes

    # bugfix 默认启用的证据链
    probes = ["error_cache", "source_code", "log_error"]
    if has_db_datasource:
        probes.append("db_query")
    return probes


def heuristic_classify_intent(
    user_query: str,
    *,
    has_db_datasource: bool = False,
    has_log_signal: bool = False,
) -> tuple[TaskIntent, list[EvidenceType]]:
    """无 LLM Key 或调用失败时的确定性意图分流规则."""
    text = (user_query or "").strip()
    explicit_qa = bool(_QA_SIGNALS_RE.search(text))
    explicit_no_edit = bool(re.search(r"不(?:要|用)?(?:改|修改|编辑|动)代码|别改|只(?:做)?(?:分析|排查|查看|诊断)", text))

    if explicit_no_edit or (explicit_qa and not _EDIT_OVERRIDE_RE.search(text)):
        intent: TaskIntent = "qa"
    elif _FEATURE_SIGNALS_RE.search(text) and not _BUG_SIGNALS_RE.search(text):
        intent = "feature"
    else:
        intent = "bugfix"

    probes = select_probes_for_intent(
        intent,
        user_query=text,
        has_db_datasource=has_db_datasource,
        has_log_signal=has_log_signal,
    )
    return intent, probes


async def classify_task_intent(
    user_query: str,
    *,
    repo_map: str = "",
    has_db_datasource: bool = False,
    has_log_signal: bool = False,
) -> tuple[TaskIntent, list[EvidenceType]]:
    """优先用结构化 LLM 判断任务意图与探针需求，失败或无 Key 时平滑降级规则分类."""
    fallback_intent, fallback_probes = heuristic_classify_intent(
        user_query,
        has_db_datasource=has_db_datasource,
        has_log_signal=has_log_signal,
    )
    if not llm_client_manager.has_valid_key():
        return fallback_intent, fallback_probes

    try:
        llm = llm_client_manager.get_llm(temperature=0.0).with_structured_output(
            _IntentLLMOutput
        )
        sys_msg = SystemMessage(
            content=(
                "你是研发 Agent 入口分流器，请判断用户请求属于哪类任务并勾选必要的只读探针：\n"
                "- bugfix: 排查并修复代码缺陷/线上报错\n"
                "- feature: 创建新文件、新增功能或代码改造（一般无需查报错日志和报错缓存）\n"
                "- qa: 只读排查日志、分析报错原因或代码问答，明确不需要生成补丁改代码"
            )
        )
        human_msg = HumanMessage(
            content=(
                f"用户需求: {user_query}\n"
                f"附加信号: has_log_signal={has_log_signal}, has_db_datasource={has_db_datasource}\n"
                f"仓库大纲摘录:\n{(repo_map or '')[:1200]}"
            )
        )
        parsed = await llm.ainvoke([sys_msg, human_msg])
        llm_probes: list[EvidenceType] = []
        if parsed.need_error_cache:
            llm_probes.append("error_cache")
        if parsed.need_source_code:
            llm_probes.append("source_code")
        if parsed.need_log_error:
            llm_probes.append("log_error")
        if parsed.need_db_query:
            llm_probes.append("db_query")
        probes = select_probes_for_intent(
            parsed.intent,
            user_query=user_query,
            has_db_datasource=has_db_datasource,
            has_log_signal=has_log_signal,
            llm_probes=llm_probes,
        )
        return parsed.intent, probes
    except Exception:
        return fallback_intent, fallback_probes
