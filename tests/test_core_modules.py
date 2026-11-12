"""核心模块单元测试：命名空间、ID 规范、脱敏证据、SQL 校验、RepoMap、SEARCH/REPLACE 与 RRF."""

import uuid
from pathlib import Path
import pytest

from app.core.datasource_registry import validate_readonly_sql
from app.core.edit_applier import apply_edit_blocks, parse_search_replace_blocks
from app.core.evidence_collector import build_evidence, redact_text
from app.core.repo_map import (
    extract_query_mentions,
    find_repo_source_files,
    generate_repo_map,
    repo_map_manager,
)
from app.exceptions import UnsafeSQLBlockedError
from app.schemas.domain_schemas import (
    ErrorCacheHit,
    build_cache_id,
    build_es_cache_doc_id,
    build_qdrant_point_id,
    derive_project_namespace,
)
from app.tools.cache_tools import rrf_fuse_runbooks


def test_namespace_and_id_composition(tmp_path: Path) -> None:
    repo = tmp_path / "Order-Service"
    repo.mkdir()
    auto_ns = derive_project_namespace(str(repo))
    assert auto_ns.startswith("order-service_")
    assert len(auto_ns.split("_")[-1]) == 4

    explicit_ns = derive_project_namespace(str(repo), "Team Alpha/API")
    assert explicit_ns == "team_alpha_api"

    cid = build_cache_id(auto_ns, "TypeError", "discount is None")
    assert cid.startswith("err_")
    assert len(cid) == 14

    es_id = build_es_cache_doc_id(auto_ns, cid)
    assert es_id == f"es_{auto_ns}_{cid}"

    qid = build_qdrant_point_id(auto_ns, cid)
    parsed = uuid.UUID(qid)
    assert parsed.version == 5
    assert qid != build_qdrant_point_id("other_ns", cid)


def test_evidence_collector_redacts_and_builds_deterministic_ids() -> None:
    raw = "api_key=abcdef123456 phone=13912345678 openai=sk-proj1234567890abcdef"
    redacted = redact_text(raw)
    assert "abcdef123456" not in redacted
    assert "13912345678" not in redacted
    assert "sk-proj1234567890abcdef" not in redacted
    assert "***REDACTED***" in redacted

    ev1 = build_evidence("grep_code", {"pattern": "settle"}, raw)
    ev2 = build_evidence("grep_code", {"pattern": "settle"}, raw)
    assert ev1.id == ev2.id
    assert ev1.id.startswith("ev_code:")
    assert ev1.evidence_type == "source_code"

    ev_cache = build_evidence("search_error_cache", {"query": "TypeError"}, "hit")
    assert ev_cache.id.startswith("ev_cache:")
    assert ev_cache.evidence_type == "error_cache"


def test_readonly_sql_validation() -> None:
    assert validate_readonly_sql("SELECT id, status FROM orders WHERE id = 1")
    assert validate_readonly_sql("EXPLAIN SELECT * FROM orders")
    for unsafe in [
        "DELETE FROM orders",
        "UPDATE orders SET status='x'",
        "DROP TABLE orders",
        "SELECT 1; DROP TABLE orders",
        "INSERT INTO orders VALUES (1)",
    ]:
        with pytest.raises(UnsafeSQLBlockedError):
            validate_readonly_sql(unsafe)


def test_repo_map_and_edit_applier(tmp_path: Path) -> None:
    pkg = tmp_path / "billing"
    pkg.mkdir()
    (pkg / "calc.py").write_text(
        "class PriceCalculator:\n"
        "    def calculate_total(self, price: int, discount: int) -> int:\n"
        "        return price - discount\n",
        encoding="utf-8",
    )
    (pkg / "service.py").write_text(
        "from billing.calc import PriceCalculator\n\n"
        "def run(p, d):\n"
        "    return PriceCalculator().calculate_total(p, d)\n",
        encoding="utf-8",
    )
    # 1. 验证 Tree-sitter + grep-ast TreeContext 骨架输出与 diskcache SQLite 持久化
    rmap = generate_repo_map(
        tmp_path,
        mentioned_idents=["calculate_total"],
        max_tokens=400,
    )
    assert "billing/calc.py:" in rmap
    assert "def calculate_total(self, price: int, discount: int) -> int:" in rmap
    assert (tmp_path / ".autodev.tags.cache.v4").exists()

    # 2. 验证缩进偏差容错 + SEARCH/REPLACE + Tree-sitter 语法检查
    patch_indented = (
        "billing/calc.py\n"
        "<<<<<<< SEARCH\n"
        "def calculate_total(self, price: int, discount: int) -> int:\n"
        "    return price - discount\n"
        "=======\n"
        "def calculate_total(self, price: int, discount: int | None) -> int:\n"
        "    return price - (discount or 0)\n"
        ">>>>>>> REPLACE\n"
    )
    blocks = parse_search_replace_blocks(patch_indented)
    assert len(blocks) == 1
    edited = apply_edit_blocks(tmp_path, blocks)
    assert edited == ["billing/calc.py"]
    content = (pkg / "calc.py").read_text(encoding="utf-8")
    assert "        return price - (discount or 0)" in content

    # 3. 验证 `...` 省略号分段匹配
    patch_dots = (
        "```python\n"
        "billing/service.py\n"
        "<<<<<<< SEARCH\n"
        "from billing.calc import PriceCalculator\n"
        "...\n"
        "def run(p, d):\n"
        "    return PriceCalculator().calculate_total(p, d)\n"
        "=======\n"
        "from billing.calc import PriceCalculator\n"
        "...\n"
        "def run(p, d):\n"
        "    calc = PriceCalculator()\n"
        "    return calc.calculate_total(p, d)\n"
        ">>>>>>> REPLACE\n"
        "```\n"
    )
    blocks2 = parse_search_replace_blocks(patch_dots)
    apply_edit_blocks(tmp_path, blocks2)
    assert "calc = PriceCalculator()" in (pkg / "service.py").read_text(encoding="utf-8")


def test_rrf_fusion_scoring() -> None:
    bm25 = [
        ErrorCacheHit(cache_id="err_a", error_signature="SigA", bm25_score=9.0),
        ErrorCacheHit(cache_id="err_b", error_signature="SigB", bm25_score=5.0),
    ]
    vec = [
        ErrorCacheHit(cache_id="err_b", error_signature="SigB", vector_score=0.92),
        ErrorCacheHit(cache_id="err_c", error_signature="SigC", vector_score=0.80),
    ]
    fused = rrf_fuse_runbooks(bm25, vec, k=60, top_n=3)
    assert fused[0]["cache_id"] == "err_b"
    assert fused[0]["rrf_score"] > fused[1]["rrf_score"]


def test_repo_map_reuses_state_files_and_extracts_mentions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pkg = tmp_path / "billing"
    pkg.mkdir()
    (pkg / "calc.py").write_text(
        "class PriceCalculator:\n"
        "    def calculate_total(self, price: int, discount: int) -> int:\n"
        "        return price - discount\n",
        encoding="utf-8",
    )
    (pkg / "service.py").write_text(
        "from billing.calc import PriceCalculator\n\n"
        "def run_order(p, d):\n"
        "    return PriceCalculator().calculate_total(p, d)\n",
        encoding="utf-8",
    )

    repo_files = find_repo_source_files(tmp_path, relative=True)
    assert repo_files == ["billing/calc.py", "billing/service.py"]

    mentioned_files, mentioned_idents = extract_query_mentions(
        "检查 service 中的 run_order 调用链路",
        repo_files,
    )
    assert "billing/service.py" in mentioned_files
    assert "run_order" in mentioned_idents

    # 传入 chat_fnames / other_fnames 时，禁止再次扫描全仓目录
    monkeypatch.setattr(
        "app.core.repo_map.find_repo_source_files",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("不应重新全仓扫描")),
    )
    rmap = generate_repo_map(
        tmp_path,
        chat_fnames=["billing/service.py"],
        other_fnames=["billing/calc.py"],
        max_map_tokens=400,
        mentioned_fnames=mentioned_files,
        mentioned_idents=mentioned_idents,
    )
    assert "billing/calc.py:" in rmap
    assert "billing/service.py:" not in rmap
    assert "def calculate_total" in rmap

    # 同一仓库同一参数下应复用持久化 RepoMap 实例及其 L1 map_cache
    rm1 = repo_map_manager.get_or_create(tmp_path, map_tokens=400)
    rm2 = repo_map_manager.get_or_create(tmp_path, map_tokens=400)
    assert rm1 is rm2
    assert len(rm1.map_cache) >= 1
