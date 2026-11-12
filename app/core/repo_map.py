"""全仓 RepoMap 符号拓扑感知模块 (完整移植自 aider/repomap.py，去除 CLI 终端耦合).

核心机制:
1. Tree-sitter + `.scm` 查询提取跨语言 AST 定义(def)与引用(ref)，缺失 ref 时用 Pygments 词法补充
2. diskcache (SQLite) 按文件 mtime 持久化 AST Tags，并支持损坏自动重建与内存降级
3. NetworkX MultiDiGraph + Personalized PageRank 计算文件及具体符号重要度
4. Binary Search 逼近 max_map_tokens 预算，调用 grep-ast TreeContext 渲染折叠代码骨架树
"""

import math
import os
import re
import shutil
import sqlite3
import subprocess
import time
import warnings
from collections import Counter, defaultdict, namedtuple
from collections.abc import Callable
from pathlib import Path

from diskcache import Cache
from grep_ast import TreeContext, filename_to_lang
from pathspec import PathSpec
from pygments.lexers import guess_lexer_for_filename
from pygments.token import Token
from tree_sitter import Query

warnings.simplefilter("ignore", category=FutureWarning)
from grep_ast.tsl import USING_TSL_PACK, get_language, get_parser  # noqa: E402

Tag = namedtuple("Tag", "rel_fname fname line name kind".split())
SQLITE_ERRORS = (sqlite3.OperationalError, sqlite3.DatabaseError, OSError)

CACHE_VERSION = 4 if USING_TSL_PACK else 3

_IGNORE_DIRS = {
    ".git",
    ".venv",
    "venv",
    "__pycache__",
    "node_modules",
    ".sandboxes",
    ".pytest_cache",
    ".idea",
}

_ROOT_IMPORTANT_FILES = {
    os.path.normpath(p)
    for p in [
        ".gitignore",
        "README.md",
        "pyproject.toml",
        "requirements.txt",
        "setup.py",
        "package.json",
        "tsconfig.json",
        "go.mod",
        "Cargo.toml",
        "pom.xml",
        "build.gradle",
        ".env.example",
        "pytest.ini",
        "Dockerfile",
        "docker-compose.yml",
    ]
}


def filter_important_files(file_paths: list[str]) -> list[str]:
    """筛选仓库根目录常见关键配置文件，用于在 RepoMap 顶部保留项目基线."""
    important: list[str] = []
    for path in file_paths:
        norm = os.path.normpath(path)
        dirname = os.path.normpath(os.path.dirname(path))
        fname = os.path.basename(path)
        if dirname == os.path.normpath(".github/workflows") and fname.endswith(".yml"):
            important.append(path)
        elif norm in _ROOT_IMPORTANT_FILES:
            important.append(path)
    return important


def get_scm_fname(lang: str) -> Path | None:
    """定位当前包内置 Tree-sitter `.scm` 标签提取规则文件."""
    queries_root = Path(__file__).resolve().parent / "queries"
    if USING_TSL_PACK:
        candidate = queries_root / "tree-sitter-language-pack" / f"{lang}-tags.scm"
        if candidate.exists():
            return candidate
    fallback = queries_root / "tree-sitter-languages" / f"{lang}-tags.scm"
    return fallback if fallback.exists() else None


def find_repo_source_files(repo_root: str | Path, *, relative: bool = False) -> list[str]:
    """遍历目标仓库源码与重要配置文件 (Git 仓库优先用 git ls-files，否则回退 os.walk + .gitignore)."""
    root = Path(repo_root).resolve()
    if not root.exists():
        return []
    if root.is_file():
        return [root.name if relative else str(root)]

    if (root / ".git").exists():
        try:
            proc = subprocess.run(
                ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
                cwd=str(root),
                capture_output=True,
                timeout=5,
                check=False,
            )
            if proc.returncode == 0:
                git_files: list[str] = []
                for raw in proc.stdout.decode("utf-8", errors="ignore").split("\x00"):
                    if not raw:
                        continue
                    rel_posix = raw.replace("\\", "/")
                    parts = rel_posix.split("/")
                    if any(
                        p in _IGNORE_DIRS
                        or p.startswith(".autodev.tags.cache")
                        or p.startswith(".aider.tags.cache")
                        for p in parts[:-1]
                    ):
                        continue
                    full_path = root / rel_posix
                    if not full_path.is_file():
                        continue
                    if filename_to_lang(str(full_path)) or os.path.normpath(rel_posix) in _ROOT_IMPORTANT_FILES:
                        git_files.append(rel_posix if relative else str(full_path))
                return sorted(set(git_files))
        except (OSError, subprocess.SubprocessError):
            pass

    gitignore_spec: PathSpec | None = None
    gi_file = root / ".gitignore"
    if gi_file.is_file():
        try:
            lines = gi_file.read_text(encoding="utf-8", errors="ignore").splitlines()
            gitignore_spec = PathSpec.from_lines("gitwildmatch", lines)
        except OSError:
            gitignore_spec = None

    collected: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(
            d
            for d in dirnames
            if d not in _IGNORE_DIRS
            and not d.startswith(".autodev.tags.cache")
            and not d.startswith(".aider.tags.cache")
        )
        for fname in sorted(filenames):
            full_path = Path(dirpath) / fname
            rel_posix = full_path.relative_to(root).as_posix()
            if gitignore_spec and gitignore_spec.match_file(rel_posix):
                continue
            if filename_to_lang(str(full_path)) or os.path.normpath(rel_posix) in _ROOT_IMPORTANT_FILES:
                collected.append(rel_posix if relative else str(full_path))
    return collected


def extract_query_mentions(text: str, repo_files: list[str]) -> tuple[list[str], list[str]]:
    """复刻 aider 提及提取逻辑: 解析用户消息中的文件路径、basename 及长度>=5的无后缀模块名."""
    query = text or ""
    # 用“一个或多个非单词字符”作为分隔符，把字符串 query 切开。
    mentioned_idents = sorted({tok for tok in re.split(r"\W+", query) if tok and len(tok) >= 2})
    ident_lowers = {tok.lower() for tok in mentioned_idents if len(tok) >= 5}

    norm_files = [Path(f).as_posix() for f in repo_files]
    stem_index: dict[str, set[str]] = defaultdict(set)
    mentioned_files: set[str] = set()

    for rel in norm_files:
        p = Path(rel)
        base = p.name
        # p.stem 返回去掉扩展名后的文件名部分。
        stem = p.stem.lower()
        if len(stem) >= 5:
            stem_index[stem].add(rel)
        if rel in query or (len(base) >= 3 and ("." in base or "/" in rel) and base in query):
            mentioned_files.add(rel)

    for ident_l in ident_lowers:
        mentioned_files.update(stem_index.get(ident_l, ()))

    return sorted(mentioned_files), mentioned_idents


class RepoMap:
    """工业级全仓 AST 符号拓扑与 PageRank 代码大纲生成器."""

    TAGS_CACHE_DIR = f".autodev.tags.cache.v{CACHE_VERSION}"

    def __init__(
        self,
        *,
        map_tokens: int = 1024,
        root: str | Path | None = None,
        token_counter: Callable[[str], int] | None = None,
        repo_content_prefix: str | None = None,
        max_context_window: int | None = None,
        map_mul_no_files: int = 8,
        refresh: str = "auto",
        cache_dir: str | Path | None = None,
    ) -> None:
        self.root = str(Path(root).resolve()) if root else os.getcwd()
        self.cache_base = Path(cache_dir).resolve() if cache_dir else Path(self.root)
        self.refresh = refresh
        self.max_map_tokens = map_tokens
        self.map_mul_no_files = map_mul_no_files
        self.max_context_window = max_context_window
        self.repo_content_prefix = repo_content_prefix
        self._token_counter = token_counter

        self.TAGS_CACHE: Cache | dict = {}
        self.load_tags_cache()

        self.tree_cache: dict = {}
        self.tree_context_cache: dict = {}
        self.map_cache: dict = {}
        self.map_processing_time: float = 0.0
        self.last_map: str | None = None

    def token_count(self, text: str) -> float:
        """短文本精确计数，长文本按行抽样估算 Token 消耗."""
        len_text = len(text)
        if len_text == 0:
            return 0
        counter = self._token_counter or (lambda s: max(1, math.ceil(len(s) / 4)))
        if len_text < 200:
            return counter(text)

        lines = text.splitlines(keepends=True)
        num_lines = len(lines)
        step = num_lines // 100 or 1
        sampled = "".join(lines[::step])
        if not sampled:
            return counter(text)
        sample_tokens = counter(sampled)
        return sample_tokens / len(sampled) * len_text

    def _abs_fname(self, fname: str) -> str:
        p = Path(fname)
        if p.is_absolute():
            return str(p)
        return str((Path(self.root) / fname).resolve())

    def get_rel_fname(self, fname: str) -> str:
        abs_path = self._abs_fname(fname)
        try:
            return Path(os.path.relpath(abs_path, self.root)).as_posix()
        except ValueError:
            return Path(abs_path).as_posix()

    def get_mtime(self, fname: str) -> float | None:
        try:
            return os.path.getmtime(fname)
        except OSError:
            return None

    def tags_cache_error(self) -> None:
        """SQLite 缓存受损时重建，失败则降级为进程内字典."""
        if isinstance(self.TAGS_CACHE, dict):
            return
        path = self.cache_base / self.TAGS_CACHE_DIR
        try:
            if path.exists():
                shutil.rmtree(path, ignore_errors=True)
            new_cache = Cache(str(path))
            new_cache["__health__"] = "ok"
            _ = new_cache["__health__"]
            del new_cache["__health__"]
            self.TAGS_CACHE = new_cache
            return
        except SQLITE_ERRORS:
            self.TAGS_CACHE = {}

    def load_tags_cache(self) -> None:
        path = self.cache_base / self.TAGS_CACHE_DIR
        try:
            self.TAGS_CACHE = Cache(str(path))
        except SQLITE_ERRORS:
            self.tags_cache_error()

    def close(self) -> None:
        """显式关闭 SQLite diskcache 句柄，避免 Windows 临时目录清理文件锁."""
        if not isinstance(self.TAGS_CACHE, dict):
            try:
                self.TAGS_CACHE.close()
            except Exception:
                pass

    def _run_captures(self, query: Query, node):
        if hasattr(query, "captures"):
            return query.captures(node)
        from tree_sitter import QueryCursor

        cursor = QueryCursor(query)
        return cursor.captures(node)

    # 假设你在 auth.py 的第 10 行写了这段代码：
    # def verify_token(token):
    #   return True
    # 被 Tree-sitter 解析成语法树（AST）后，在内存里其实长成这样一棵“家族树”：\
    # └── function_definition (函数定义节点)
    #     ├── def (关键字)
    #     ├── name: identifier (标识符节点, 文本="verify_token", 行号=10)  <-- 注意这个！
    #     ├── parameters (参数列表节点)
    #     │   └── identifier: "token"
    #     ├── : (冒号)
    #     └── block (函数体代码块)
    #         └── return_statement: "return True"
    # 你的规则文件 python-tags.scm 里写了这样一个“模板框框”：
    # (function_definition
    #   name: (identifier) @name.definition.function)
    # 解释器在树上巡视，发现了一个 function_definition 类型的节点；
    # 解释器往里看，发现它内部刚好有一个字段叫 name:；
    # name: 对应的子节点类型刚好是 identifier（标识符）；
    # 结构完全吻合！这就叫“匹配成功（Match）”！
    # 紧接着，模具后面贴的标签是 @name.definition.function，解释器就会把这个 @ 后面的标签贴在被捕获的节点上。
    # 执行完 captures = self._run_captures(...) 之后,拿到成功匹配的树节点对象,里面存着该节点信息
    # node.text.decode("utf-8")  # 1. 节点对应的代码文本 -> "verify_token"
    # node.start_point           # 2. 物理坐标元组 (行号, 列号) -> (10, 4)
    # tag                        # 3. 刚才贴上的标签名字 -> "name.definition.function"
    # 在下面，立刻把这些信息打包成了Tag 对象：
    # result = Tag(
    #     rel_fname = "services/auth.py",  # 哪个文件
    #     name = "verify_token",          # 叫什么名字
    #     kind = "def",                   # 是定义(def)还是调用(ref)
    #     line = 10                       # 在第 10 行
    # )
    def get_tags_raw(self, fname: str, rel_fname: str):
        """读取文件并执行 Tree-sitter Query 提取 def/ref Tag，缺失 ref 时用 Pygments 补充."""
        lang = filename_to_lang(fname)
        if not lang:
            return
        try:
            language = get_language(lang)  # 1. 词法/文法规则定义 (Language Object)
            parser = get_parser(lang)  # 2. 真正的语法树解析引擎 (Parser Instance)
        except Exception:
            return
        # 取当前语言专用的“语法树检索规则文件”（.scm 文件）的路径。
        query_path = get_scm_fname(lang)
        if not query_path or not query_path.exists():
            return
        query_scm = query_path.read_text(encoding="utf-8", errors="ignore")
        try:
            code = Path(fname).read_text(encoding="utf-8", errors="ignore")
        except OSError:
            return
        if not code:
            return
        # 这段代码的语法树
        tree = parser.parse(bytes(code, "utf-8"))
        # 拿着检索规则，在整个语法树上执行搜索，一键捕获出所有匹配的代码节点
        captures = self._run_captures(Query(language, query_scm), tree.root_node)

        if isinstance(captures, dict):
            all_nodes = [(node, tag) for tag, nodes in captures.items() for node in nodes]
        else:
            all_nodes = captures

        saw: set[str] = set()
        for node, tag in all_nodes:
            if tag.startswith("name.definition."):
                kind = "def"
            elif tag.startswith("name.reference."):
                kind = "ref"
            else:
                continue
            saw.add(kind)
            # list(...) 会一口气把 get_tags_raw 里 yield 出来的所有 Tag 全部消费完
            yield Tag(
                rel_fname=rel_fname,
                fname=fname,
                line=node.start_point[0],
                name=node.text.decode("utf-8", errors="ignore"),
                kind=kind,
            )

        if "ref" in saw or "def" not in saw:
            return
        # 只有 defs, 没有refs - 某些语言的 tags.scm 规则文件只写了提取 def，比如 C++
        # PageRank 算法核心是构建有向图：调用方（referencer） →  定义方（definer）。
        # 如果全仓库的 C++ 文件都只提取出了 def，没有任何 ref
        # 1. 图上将一条有向边都没有 2. PageRank 矩阵(转移矩阵)全为 0，算法直接报 ZeroDivisionError（除以零）崩溃退出，代码大纲彻底瘫痪。
        # 所以我们接下来暴力检索文件代码里出现的所有“单词/名字”,打上ref标签,但是注意由于是所有单词,上半场打上的def标签的单词,
        # 在下面会又打上ref标签,也就是说，同一个文件里，这个名字既被标记了 def，又被标记了 ref。
        # 但这完全不会破坏系统，原因有两点：
        # 1. 原本的 def 绝对不会被弄丢或覆盖
        # def 和 ref 是存放在两个完全隔离的独立数据结构里的：
        # get_ranked_tags 里的处理逻辑：
        # if tag.kind == "def":
        #     defines[tag.name].add(rel_fname)     # 1. 进“定义池”
        # elif tag.kind == "ref":
        #     references[tag.name].append(rel_fname) # 2. 进“引用池”
        # 2. 图算法里，这只是产生了一个“自环”（自己连自己）
        # 自环只会给自身增加一点点保底停留概率，对大局毫无副作用；
        # 保留自身的引用自环，不仅能维持马尔可夫链的连通性，还能让经常被自身多个方法反复调用的核心基础函数，自然而然获得更高的内聚重要性得分。
        try:
            # 据文件名后缀和源码内容，自动猜出并选定一个最合适的【词法分词器（Lexer）】
            lexer = guess_lexer_for_filename(fname, code)
        except Exception:
            return
        # 把代码里出现的所有“单词/名字”，全部打包成调用标签（kind="ref"）吐出来，强行给调用图连线。
        for token_type, token_val in lexer.get_tokens(code):
            if token_type in Token.Name:
                yield Tag(
                    rel_fname=rel_fname,
                    fname=fname,
                    line=-1,
                    name=token_val,
                    kind="ref",
                )

    def get_tags(self, fname: str, rel_fname: str) -> list[Tag]:
        """优先读取 diskcache 中按 mtime 缓存的 AST Tags."""
        file_mtime = self.get_mtime(fname)
        if file_mtime is None:
            return []

        cache_key = str(Path(fname).resolve())
        try:
            cached = self.TAGS_CACHE.get(cache_key)
        except SQLITE_ERRORS:
            self.tags_cache_error()
            cached = self.TAGS_CACHE.get(cache_key)

        if cached is not None and cached.get("mtime") == file_mtime:
            return list(cached.get("data", []))

        data = list(self.get_tags_raw(fname, rel_fname))
        try:
            self.TAGS_CACHE[cache_key] = {"mtime": file_mtime, "data": data}
        except SQLITE_ERRORS:
            self.tags_cache_error()
            self.TAGS_CACHE[cache_key] = {"mtime": file_mtime, "data": data}
        return data

    # 有一个转移矩阵和一个初始分布,一直乘乘乘乘...,最后发现状态不变了,这就是我们要的平稳分布
    # 转移矩阵生成:
    # 1. 确定节点与边的方向
    # 节点（Node）：每个文件（如 app.py、user_service.py、db.py）。
    # 有向边的方向：从“调用方”指向“定义方”
    # 2. 计算每条边的原始权重（Raw Weight）
    # 每条边的权重由两个因素相乘决定：
    # weight=use_mul×sqrt(num_refs)
    # 3. 归一化成“转移概率”（出度归一化）
    # 当你把图 G 交给 nx.pagerank(G, weight="weight") 时，NetworkX 内部会将边的权重转化成马尔可夫转移概率
    # 4. 融合个性化“随机瞬移”（加入 personalization）
    # 真实的 PageRank 转移矩阵并不是单纯顺着调用链走，还包含了 阻尼系数 𝛼（默认 0.85）：
    # M = 0.85×P代码调用跳转+0.15×V随机瞬移偏置
    # 85% 的概率：严格顺着第三步算出来的代码调用链走；
    # 15% 的概率（瞬移）：不看代码调用，随机(personalization决定随机的方向)空降到一个文件。
    def get_ranked_tags(
        self,
        chat_fnames: list[str],
        other_fnames: list[str],
        mentioned_fnames: set[str],
        mentioned_idents: set[str],
    ) -> list:
        """构建 MultiDiGraph 符号引用图，计算 Personalized PageRank 并排序所有 Tag."""
        # 它把所有文件的函数定义与调用关系连接成一张庞大的有向网络图，然后运行 Google 著名的 PageRank 算法，
        # 给全仓库的每个函数、每个类打出重要性分数，并按从高到低排序。
        import networkx as nx

        # 记录某个符号在哪些文件中【被定义】: { "函数/类名": {"file1.py", "file2.py"} }
        defines: dict[str, set[str]] = defaultdict(set)
        # 记录某个符号在哪些文件中【被调用/引用】: { "函数/类名": ["fileA.py", "fileA.py", "fileB.py"] }
        references: dict[str, list[str]] = defaultdict(list)
        # 记录某个文件内部都包含了哪些【具体的定义标签 Tag】: { "file1.py": {Tag1, Tag2, ...} }
        definitions: dict[tuple[str, str], set[Tag]] = defaultdict(set)
        # Personalized PageRank 的个性化偏置字典: { "文件名": 偏置分数 }
        personalization: dict[str, float] = {}

        chat_abs = [self._abs_fname(f) for f in chat_fnames]
        other_abs = [self._abs_fname(f) for f in other_fnames]
        chat_rel_fnames = {self.get_rel_fname(f) for f in chat_abs}
        mentioned_rel_fnames = {self.get_rel_fname(f) for f in mentioned_fnames}

        fnames = sorted(set(chat_abs).union(set(other_abs)))
        if not fnames:
            return []
        # NetworkX 默认普通节点的游走概率是 1 / len(fnames)
        # 这里给 100 / len(fnames)，相当于给重点文件的初始能量放大了整整 100 倍
        personalize = 100.0 / len(fnames)

        for fname in fnames:
            if not Path(fname).is_file():
                continue
            rel_fname = self.get_rel_fname(fname)
            current_pers = 0.0
            if rel_fname in chat_rel_fnames:
                current_pers += personalize
            if rel_fname in mentioned_rel_fnames:
                current_pers = max(current_pers, personalize)

            path_obj = Path(rel_fname)
            # p = Path("backend/services/auth/login.py")
            # print(p.parts)
            # 输出: ('backend', 'services', 'auth', 'login.py')
            path_parts = set(path_obj.parts)
            basename_ext = path_obj.name
            basename_no_ext, _ = os.path.splitext(basename_ext)
            # 当前文件的组件构成于mentioned_idents的交集,如果存在继续加分
            if path_parts.union({basename_ext, basename_no_ext}).intersection(mentioned_idents):
                current_pers += personalize
            if current_pers > 0:
                personalization[rel_fname] = current_pers

            for tag in self.get_tags(fname, rel_fname):
                if tag.kind == "def":
                    defines[tag.name].add(rel_fname)
                    definitions[(rel_fname, tag.name)].add(tag)
                elif tag.kind == "ref":
                    references[tag.name].append(rel_fname)

        if not references:
            references = {k: list(v) for k, v in defines.items()}

        idents = set(defines.keys()).intersection(set(references.keys()))
        G = nx.MultiDiGraph()

        for ident in defines.keys():
            if ident in references:
                continue
            for definer in defines[ident]:
                G.add_edge(definer, definer, weight=0.1, ident=ident)

        for ident in idents:
            definers = defines[ident]
            mul = 1.0
            is_snake = ("_" in ident) and any(c.isalpha() for c in ident)
            is_kebab = ("-" in ident) and any(c.isalpha() for c in ident)
            is_camel = any(c.isupper() for c in ident) and any(c.islower() for c in ident)
            if ident in mentioned_idents:
                mul *= 10
            if (is_snake or is_kebab or is_camel) and len(ident) >= 8:
                mul *= 10
            if ident.startswith("_"):
                mul *= 0.1
            if len(definers) > 5:
                mul *= 0.1

            for referencer, count in Counter(references[ident]).items():
                for definer in definers:
                    use_mul = mul * 50 if referencer in chat_rel_fnames else mul
                    G.add_edge(
                        referencer,
                        definer,
                        weight=use_mul * math.sqrt(count),
                        ident=ident,
                    )

        if G.number_of_nodes() == 0:
            return [(self.get_rel_fname(f),) for f in other_fnames]

        pers_args = (
            {"personalization": personalization, "damping": personalization}
            if personalization
            else {}
        )
        try:
            ranked = nx.pagerank(
                G,
                weight="weight",
                **(
                    {"personalization": personalization, "dangling": personalization}
                    if personalization
                    else {}
                ),
            )
        except ZeroDivisionError:
            try:
                ranked = nx.pagerank(G, weight="weight")
            except ZeroDivisionError:
                return []

        ranked_definitions: dict[tuple[str, str], float] = defaultdict(float)
        for src in G.nodes:
            src_rank = ranked[src]
            total_weight = sum(
                data["weight"] for _s, _d, data in G.out_edges(src, data=True)
            )
            if total_weight <= 0:
                continue
            for _s, dst, data in G.out_edges(src, data=True):
                edge_rank = src_rank * data["weight"] / total_weight
                ranked_definitions[(dst, data["ident"])] += edge_rank

        ranked_tags: list = []
        sorted_defs = sorted(
            ranked_definitions.items(), reverse=True, key=lambda x: (x[1], x[0])
        )
        for (fname, ident), _rank in sorted_defs:
            if fname in chat_rel_fnames:
                continue
            ranked_tags.extend(sorted(definitions.get((fname, ident), [])))

        rel_other_without_tags = {self.get_rel_fname(f) for f in other_fnames}
        included = {rt[0] for rt in ranked_tags}
        top_rank = sorted(((rank, node) for node, rank in ranked.items()), reverse=True)
        for _rank, fname in top_rank:
            rel_other_without_tags.discard(fname)
            if fname not in included and fname not in chat_rel_fnames:
                ranked_tags.append((fname,))
        for fname in sorted(rel_other_without_tags):
            if fname not in included and fname not in chat_rel_fnames:
                ranked_tags.append((fname,))
        return ranked_tags

    def render_tree(self, abs_fname: str, rel_fname: str, lois: list[int]) -> str:
        """调用 grep-ast TreeContext 按核心行号渲染紧凑的代码签名折叠树."""
        mtime = self.get_mtime(abs_fname)
        key = (rel_fname, tuple(sorted(lois)), mtime)
        if key in self.tree_cache:
            return self.tree_cache[key]

        cached_ctx = self.tree_context_cache.get(rel_fname)
        if cached_ctx is None or cached_ctx["mtime"] != mtime:
            try:
                code = Path(abs_fname).read_text(encoding="utf-8", errors="ignore")
            except OSError:
                code = ""
            if code and not code.endswith("\n"):
                code += "\n"
            context = TreeContext(
                rel_fname,
                code,
                color=False,
                line_number=False,
                child_context=False,
                last_line=False,
                margin=0,
                mark_lois=False,
                loi_pad=0,
                show_top_of_file_parent_scope=False,
            )
            self.tree_context_cache[rel_fname] = {"context": context, "mtime": mtime}

        context = self.tree_context_cache[rel_fname]["context"]
        context.lines_of_interest = set()
        context.add_lines_of_interest(lois)
        context.add_context()
        res = context.format()
        self.tree_cache[key] = res
        return res

    def to_tree(self, tags: list, chat_rel_fnames: set[str]) -> str:
        if not tags:
            return ""
        cur_fname: str | None = None
        cur_abs_fname: str | None = None
        lois: list[int] | None = None
        output = ""
        dummy_tag = (None,)

        for tag in sorted(tags, key=lambda t: (t[0] or "", t[2] if isinstance(t, Tag) else -1)) + [dummy_tag]:
            this_rel_fname = tag[0]
            if this_rel_fname in chat_rel_fnames:
                continue
            if this_rel_fname != cur_fname:
                if lois is not None and cur_abs_fname and cur_fname:
                    output += f"\n{cur_fname}:\n"
                    output += self.render_tree(cur_abs_fname, cur_fname, lois)
                    lois = None
                elif cur_fname:
                    output += f"\n{cur_fname}\n"
                if isinstance(tag, Tag):
                    lois = []
                    cur_abs_fname = tag.fname
                cur_fname = this_rel_fname
            if lois is not None and isinstance(tag, Tag):
                lois.append(tag.line)

        return "\n".join(line[:100] for line in output.splitlines()) + "\n"

    def get_ranked_tags_map_uncached(
        self,
        chat_fnames: list[str],
        other_fnames: list[str] | None = None,
        max_map_tokens: int | None = None,
        mentioned_fnames: set[str] | None = None,
        mentioned_idents: set[str] | None = None,
    ) -> str:
        other_fnames = other_fnames or []
        max_map_tokens = max_map_tokens or self.max_map_tokens
        mentioned_fnames = mentioned_fnames or set()
        mentioned_idents = mentioned_idents or set()

        ranked_tags = self.get_ranked_tags(
            chat_fnames, other_fnames, mentioned_fnames, mentioned_idents
        )
        # 下面做的事情:
        # 强行置顶项目根目录特殊配置文件（special_fnames）；
        # 用二分查找法（Binary Search）寻找刚好卡在 max_map_tokens 预算上限的最佳标签数量，并生成最终大纲代码树。
        other_rel = sorted({self.get_rel_fname(f) for f in other_fnames})
        special_fnames = filter_important_files(other_rel)
        # existing_fnames:当前 ranked_tags 列表中已经包含了哪些文件名。
        existing_fnames = {tag[0] for tag in ranked_tags}
        # # 除掉与existing_fnames重复的文件
        special_tags = [(fn,) for fn in special_fnames if fn not in existing_fnames]
        ranked_tags = special_tags + ranked_tags


        # # ranked_tags 里有成百上千个排好序的标签，到底截取前多少个（ranked_tags[:middle]），渲染出来的大纲文本才刚好填满 max_map_tokens（比如 1024），既不超标，也不浪费？
        num_tags = len(ranked_tags)
        if num_tags == 0:
            return ""

        lower_bound = 0
        upper_bound = num_tags
        best_tree = ""
        best_tree_tokens = 0
        chat_rel_fnames = {self.get_rel_fname(f) for f in chat_fnames}
        self.tree_cache = {}
        # 在进入这段代码之前，ranked_tags 是这样一个已经根据重要程度排好序的队列
        # 索引 0：全仓库第 1 重要的函数
        # 索引 1：全仓库第 2 重要的函数
        # 索引 2：全仓库第 3 重要的函数
        # ……
        # 每个标签渲染成一行大纲代码（比如 def verify_token(token): ...），物理上无论如何都需要消耗约 20 ~ 30 个 Token。
        # 我们最终只要前40个标签,那为什么不能直接切片?
        # 因为 25 只是一个极其粗糙的“预估平均值”，真实代码的 Token 波动极其巨大，直接切片要么严重超标，要么严重浪费
        # 40 只是起跑线：算法借此直接空降到正确答案附近；
        # 二分查找做 2~3 次微调校准：
        # 切 40 个一算：才用了 600 tokens？→ 调大到 55 个；
        # 切 55 个一算：用了 1100 tokens 超标了？→调小到 48 个；
        # 切 48 个一算：刚好 1010 tokens！完美符合预算，收工退出。
        # 当没有具体文件时（冷启动）：max_map_tokens会瞬间放大 8 倍
        middle = min(max(1, int(max_map_tokens // 25)), num_tags)
        while lower_bound <= upper_bound:
            tree = self.to_tree(ranked_tags[:middle], chat_rel_fnames)
            num_tokens = self.token_count(tree)
            pct_err = abs(num_tokens - max_map_tokens) / max(max_map_tokens, 1)
            ok_err = 0.15
            if (num_tokens <= max_map_tokens and num_tokens > best_tree_tokens) or pct_err < ok_err:
                best_tree = tree
                best_tree_tokens = num_tokens
                # 只要误差在 15% 以内，直接 break 退出，不用完全相等
                if pct_err < ok_err:
                    break
            if num_tokens < max_map_tokens:
                lower_bound = middle + 1
            else:
                upper_bound = middle - 1
            middle = int((lower_bound + upper_bound) // 2)
        return best_tree

    def get_ranked_tags_map(
        self,
        chat_fnames: list[str],
        other_fnames: list[str] | None = None,
        max_map_tokens: int | None = None,
        mentioned_fnames: set[str] | None = None,
        mentioned_idents: set[str] | None = None,
        force_refresh: bool = False,
    ) -> str:
        other_fnames = other_fnames or []
        mentioned_fnames = mentioned_fnames or set()
        mentioned_idents = mentioned_idents or set()

        cache_key_parts: list = [
            tuple(sorted(chat_fnames)) if chat_fnames else None,
            tuple(sorted(other_fnames)) if other_fnames else None,
            max_map_tokens,
        ]
        if self.refresh == "auto":
            cache_key_parts.extend(
                [
                    tuple(sorted(mentioned_fnames)) if mentioned_fnames else None,
                    tuple(sorted(mentioned_idents)) if mentioned_idents else None,
                ]
            )
        cache_key = tuple(cache_key_parts)

        if not force_refresh:
            if self.refresh == "manual" and self.last_map is not None:
                return self.last_map
            use_cache = (
                self.refresh == "files"
                or (self.refresh == "auto" and self.map_processing_time > 1.0)
            )
            if use_cache and cache_key in self.map_cache:
                return self.map_cache[cache_key]

        t0 = time.time()
        result = self.get_ranked_tags_map_uncached(
            chat_fnames, other_fnames, max_map_tokens, mentioned_fnames, mentioned_idents
        )
        self.map_processing_time = time.time() - t0
        self.map_cache[cache_key] = result
        self.last_map = result
        return result

    def get_repo_map(
        self,
        chat_files: list[str] | None = None,
        other_files: list[str] | None = None,
        mentioned_fnames: set[str] | None = None,
        mentioned_idents: set[str] | None = None,
        max_map_tokens: int | None = None,
        force_refresh: bool = False,
    ) -> str:
        """计算动态预算后返回完整 RepoMap 骨架字符串."""
        chat_files = chat_files or []
        other_files = other_files if other_files is not None else find_repo_source_files(self.root)
        effective_budget = max_map_tokens if max_map_tokens is not None else self.max_map_tokens
        if effective_budget <= 0 or not other_files:
            return ""
        # padding:给系统提示词、证据/工具上下文、用户提问与历史消息、以及模型接下来生成的回复空间
        padding = 4096
        if not chat_files and self.max_context_window and self.max_context_window > padding:
            target = min(
                int(effective_budget * self.map_mul_no_files),
                self.max_context_window - padding,
            )
            if target > 0:
                effective_budget = target

        try:
            files_listing = self.get_ranked_tags_map(
                chat_files,
                other_files,
                effective_budget,
                mentioned_fnames or set(),
                mentioned_idents or set(),
                force_refresh=force_refresh,
            )
        except RecursionError:
            self.max_map_tokens = 0
            return ""

        if not files_listing:
            return ""
        if self.repo_content_prefix:
            other = "other " if chat_files else ""
            return self.repo_content_prefix.format(other=other) + files_listing
        return files_listing


class RepoMapManager:
    """长生命周期 RepoMap 实例池，保留 L1 map_cache / map_processing_time / TreeContext 缓存."""

    def __init__(self) -> None:
        self._instances: dict[tuple[str, int, int | None], RepoMap] = {}

    def get_or_create(
        self,
        repo_root: str | Path,
        *,
        map_tokens: int = 1200,
        max_context_window: int | None = None,
        cache_dir: str | Path | None = None,
    ) -> RepoMap:
        resolved = str(Path(repo_root).resolve())
        key = (resolved, map_tokens, max_context_window)
        inst = self._instances.get(key)
        if inst is None:
            inst = RepoMap(
                map_tokens=map_tokens,
                root=resolved,
                max_context_window=max_context_window,
                cache_dir=cache_dir,
            )
            self._instances[key] = inst
        return inst

    def close_repo(self, repo_root: str | Path) -> None:
        resolved = str(Path(repo_root).resolve())
        for key in [k for k in self._instances if k[0] == resolved]:
            self._instances.pop(key).close()

    def close_all(self) -> None:
        for inst in list(self._instances.values()):
            inst.close()
        self._instances.clear()


repo_map_manager = RepoMapManager()


def build_repo_map_with_fallbacks(
    rm: RepoMap,
    *,
    chat_fnames: list[str] | None = None,
    other_fnames: list[str] | None = None,
    max_map_tokens: int | None = None,
    mentioned_fnames: set[str] | list[str] | None = None,
    mentioned_idents: set[str] | list[str] | None = None,
    force_refresh: bool = False,
) -> str:
    """按 aider BaseCoder.get_repo_map 顺序执行三级降级构图."""
    chat_list = list(chat_fnames or [])
    other_list = (
        list(other_fnames)
        if other_fnames is not None
        else find_repo_source_files(rm.root, relative=True)
    )
    all_files = sorted(set(chat_list) | set(other_list))
    if not all_files:
        return ""

    m_fnames = set(mentioned_fnames or ())
    m_idents = set(mentioned_idents or ())

    repo_content = rm.get_repo_map(
        chat_files=chat_list,
        other_files=other_list,
        mentioned_fnames=m_fnames,
        mentioned_idents=m_idents,
        max_map_tokens=max_map_tokens,
        force_refresh=force_refresh,
    )
    if not repo_content and chat_list:
        repo_content = rm.get_repo_map(
            chat_files=[],
            other_files=all_files,
            mentioned_fnames=m_fnames,
            mentioned_idents=m_idents,
            max_map_tokens=max_map_tokens,
            force_refresh=force_refresh,
        )
    if not repo_content and (m_fnames or m_idents):
        repo_content = rm.get_repo_map(
            chat_files=[],
            other_files=all_files,
            max_map_tokens=max_map_tokens,
            force_refresh=force_refresh,
        )
    return repo_content


def generate_repo_map(
    repo_root: str | Path,
    *,
    chat_fnames: list[str] | None = None,
    other_fnames: list[str] | None = None,
    max_map_tokens: int | None = None,
    mentioned_fnames: set[str] | list[str] | None = None,
    mentioned_idents: set[str] | list[str] | None = None,
    force_refresh: bool = False,
    # 兼容参数别名
    chat_files: list[str] | None = None,
    mentioned_files: list[str] | None = None,
    max_tokens: int = 1200,
    max_context_window: int | None = None,
    cache_dir: str | Path | None = None,
) -> str:
    """对齐 aider get_ranked_tags_map 参数契约生成 RepoMap (不重复全仓扫描)."""
    eff_chat = list(chat_fnames if chat_fnames is not None else (chat_files or []))
    eff_other = list(other_fnames) if other_fnames is not None else None
    eff_tokens = max_map_tokens if max_map_tokens is not None else max_tokens
    eff_m_fnames = mentioned_fnames if mentioned_fnames is not None else (mentioned_files or [])
    eff_m_idents = mentioned_idents or []

    rm = repo_map_manager.get_or_create(
        repo_root,
        map_tokens=eff_tokens,
        max_context_window=max_context_window,
        cache_dir=cache_dir,
    )
    return build_repo_map_with_fallbacks(
        rm,
        chat_fnames=eff_chat,
        other_fnames=eff_other,
        max_map_tokens=eff_tokens,
        mentioned_fnames=eff_m_fnames,
        mentioned_idents=eff_m_idents,
        force_refresh=force_refresh,
    )
