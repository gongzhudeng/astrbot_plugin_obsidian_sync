"""Obsidian 知识库同步插件 for AstrBot
==================================
v1.0.0 — 对接老知识库插件版

- 支持每日定时或定时间隔同步
- 通过 WebUI 配置面板设置参数
- 检测 Obsidian 目录变更后自动增量写入知识库
- 全量替换：每次同步先删除旧集合再重建
- 支持命令权限控制与同步状态记录
- 支持配置面板手动同步与状态回显
- 依赖 astrbot_plugin_knowledge_base 插件提供向量数据库
"""

import json
import pathlib
import threading
import datetime
import os
import asyncio
from typing import Any

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api import logger

# ── 常量 ─────────────────────────────────────────────────
MAX_FILE_SIZE_BYTES = 512 * 1024  # 512KB — 超过此大小的 md 不做嵌入


def _detect_data_dir() -> pathlib.Path:
    """自动检测 AstrBot 数据目录，兼容不同部署方式。"""
    # 1. 环境变量
    env = os.environ.get("ASTRBOT_DATA")
    if env:
        p = pathlib.Path(env)
        if p.exists():
            return p
    # 2. 向上查找 plugins/ 和 knowledge_base/ 共存的目录
    this_file = pathlib.Path(__file__).resolve()
    for parent in this_file.parents:
        if (parent / "plugins").is_dir() and (parent / "knowledge_base").is_dir():
            return parent
    # 3. 兜底：插件目录上三级
    return this_file.parent.parent.parent


ASTRBOT_DATA = _detect_data_dir()
TMP_DIR = ASTRBOT_DATA / "plugin_data"
STATUS_FILE = ASTRBOT_DATA / "plugin_data" / "obsidian_sync_status.json"
REPORT_FILE = ASTRBOT_DATA / "plugin_data" / "obsidian_sync_status.md"
FILE_STATE_FILE = ASTRBOT_DATA / "plugin_data" / "obsidian_sync_file_states.json"
CONFIG_FILE = ASTRBOT_DATA / "config" / "obsidian_sync_config.json"


def _posix_relative(md: pathlib.Path, obsidian_dir: pathlib.Path) -> str:
    """统一缓存 key 格式：POSIX 正斜杠相对路径。"""
    return md.relative_to(obsidian_dir).as_posix()


@register("obsidian_sync", "gongzhudeng", "监听本地 Obsidian 目录，定时同步到 AstrBot 知识库，支持按文件名差异化分块", "1.1.0")
class ObsidianSync(Star):
    def __init__(self, context: Context, config: dict = None):
        super().__init__(context)
        self._config = config or {}
        self._stop_event = threading.Event()
        self._manual_trigger = threading.Event()
        self._sync_lock = threading.Lock()
        self._thread = threading.Thread(target=self._sync_loop, daemon=True)
        self._obsidian_dir = pathlib.Path(self._config.get("obsidian_dir", "D:/AstrBotData/Obsidian"))
        self._sync_mode = self._config.get("sync_mode", "daily")
        self._sync_daily_time = self._config.get("sync_daily_time", "03:00")
        self._sync_interval_hours = max(1, int(self._config.get("sync_interval_hours", 24)))
        self._kb_name = self._config.get("kb_name", "Obsidian-Vault")
        self._kb_file_id = self._config.get("kb_file_id", "obsidian_vault")
        self._restrict_commands = bool(self._config.get("restrict_commands", True))
        self._admin_user_ids = set(str(x) for x in self._config.get("admin_user_ids", []))
        self._allowed_user_ids = set(str(x) for x in self._config.get("allowed_user_ids", []))
        self._sync_on_startup = bool(self._config.get("sync_on_startup", False))
        # chunk_rules: list of {pattern, chunk_size, overlap} applied in order on filename
        self._chunk_rules: list[dict] = self._config.get("chunk_rules", [])
        self._memory_dir = pathlib.Path(self._config.get("memory_dir", "")) if self._config.get("memory_dir") else None
        try:
            self._loop = asyncio.get_event_loop()
        except RuntimeError:
            self._loop = None

        self._thread.start()
        logger.info(
            f"[ObsidianSync] 已启动 | 目录: {self._obsidian_dir} | 模式: {self._sync_mode} | "
            f"{'定时: ' + self._sync_daily_time if self._sync_mode == 'daily' else '间隔: ' + str(self._sync_interval_hours) + 'h'} | 知识库: {self._kb_name}"
        )

    # ── 配置文件统一读写 ──────────────────────────────────
    def _read_config_file(self) -> dict:
        """读取 WebUI 配置文件，失败返回空字典。"""
        if not CONFIG_FILE.exists():
            return {}
        try:
            return json.loads(CONFIG_FILE.read_text(encoding="utf-8-sig"))
        except (json.JSONDecodeError, OSError) as e:
            logger.warning(f"[ObsidianSync] 读取配置文件失败: {e}")
            return {}

    def _write_config_file(self, cfg: dict):
        """原子写入 WebUI 配置文件。"""
        CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = CONFIG_FILE.with_suffix(".json.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
        os.replace(tmp, CONFIG_FILE)

    # ── 权限检查 ──────────────────────────────────────────
    def _is_admin_or_allowed(self, event: AstrMessageEvent) -> bool:
        if not self._restrict_commands:
            return True
        try:
            uid = str(event.get_sender_id())
        except Exception:
            uid = ""
        return uid in self._admin_user_ids or uid in self._allowed_user_ids

    # ── 状态文件写入 ──────────────────────────────────────
    def _write_status(self, **kwargs):
        STATUS_FILE.parent.mkdir(parents=True, exist_ok=True)
        status = {
            "updated_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            **kwargs,
        }
        tmp = STATUS_FILE.with_suffix(".json.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(status, f, ensure_ascii=False, indent=2)
        os.replace(tmp, STATUS_FILE)

        md_lines = [
            "# Obsidian Sync Status",
            f"- Updated: {status['updated_at']}",
            f"- OK: {status.get('ok')}",
            f"- Stage: {status.get('stage', '')}",
            f"- Message: {status.get('message', '')}",
            f"- Knowledge Base: {status.get('kb_name', self._kb_name)}",
            f"- Changed: {status.get('changed', 0)}",
            f"- Deleted: {status.get('deleted', 0)}",
        ]
        try:
            REPORT_FILE.parent.mkdir(parents=True, exist_ok=True)
            REPORT_FILE.write_text("\n".join(md_lines) + "\n", encoding="utf-8")
        except OSError:
            pass

    def _persist_readonly_status(self, ok: bool, message: str, stage: str, changed: int = 0, deleted: int = 0):
        """将同步结果写回 WebUI 配置面板的只读字段。"""
        try:
            cfg = self._read_config_file()
            if not cfg:
                return
            cfg["last_sync_time"] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            cfg["last_sync_status"] = "成功" if ok else "失败"
            cfg["last_sync_message"] = message[:300]
            cfg["sync_now_request"] = False
            cfg["last_sync_stage"] = stage
            cfg["last_sync_changed"] = changed
            cfg["last_sync_deleted"] = deleted
            self._write_config_file(cfg)
            logger.debug("[ObsidianSync] 配置面板状态已写回")
        except Exception as e:
            logger.warning(f"[ObsidianSync] 写回状态到配置失败: {e}")

    # ── 配置热更新 ────────────────────────────────────────
    def _reload_config(self):
        cfg = self._read_config_file()
        if not cfg:
            return
        try:
            self._obsidian_dir = pathlib.Path(cfg.get("obsidian_dir", str(self._obsidian_dir)))
            self._sync_mode = cfg.get("sync_mode", self._sync_mode)
            self._sync_daily_time = cfg.get("sync_daily_time", self._sync_daily_time)
            self._sync_interval_hours = max(1, int(cfg.get("sync_interval_hours", self._sync_interval_hours)))
            self._kb_name = cfg.get("kb_name", self._kb_name)
            self._kb_file_id = cfg.get("kb_file_id", self._kb_file_id)
            self._restrict_commands = bool(cfg.get("restrict_commands", self._restrict_commands))
            self._admin_user_ids = set(str(x) for x in cfg.get("admin_user_ids", list(self._admin_user_ids)))
            self._allowed_user_ids = set(str(x) for x in cfg.get("allowed_user_ids", list(self._allowed_user_ids)))
            self._sync_on_startup = bool(cfg.get("sync_on_startup", self._sync_on_startup))
            self._chunk_rules = cfg.get("chunk_rules", self._chunk_rules)
            memory_dir_str = cfg.get("memory_dir", "")
            self._memory_dir = pathlib.Path(memory_dir_str) if memory_dir_str else None
        except Exception as e:
            logger.warning(f"[ObsidianSync] 配置热更新失败，使用旧配置: {e}")

    def _check_manual_sync(self) -> bool:
        cfg = self._read_config_file()
        return bool(cfg.get("sync_now_request", False))

    # ── 定时计算（基于上次同步结束时间） ─────────────────
    def _get_wait_seconds(self) -> float:
        now = datetime.datetime.now()
        if self._sync_mode == "daily":
            try:
                hour, minute = map(int, self._sync_daily_time.split(":"))
            except (ValueError, AttributeError):
                hour, minute = 3, 0
            target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if target <= now:
                target += datetime.timedelta(days=1)
            wait = (target - now).total_seconds()
            logger.info(f"[ObsidianSync] 下次定时同步: {target.strftime('%Y-%m-%d %H:%M')}（{wait / 3600:.1f}h 后）")
            return wait

        # 间隔模式：从上次同步结束时算起
        elapsed = now.timestamp() - self._last_sync_end_time
        interval = self._sync_interval_hours * 3600
        remaining = max(0.0, interval - elapsed)
        logger.info(f"[ObsidianSync] 下次间隔同步: {remaining / 3600:.1f}h 后")
        return remaining

    # ── 文件状态持久化 ────────────────────────────────────
    def _load_state(self) -> dict[str, Any]:
        if FILE_STATE_FILE.exists():
            try:
                return json.loads(FILE_STATE_FILE.read_text(encoding="utf-8-sig"))
            except (json.JSONDecodeError, OSError):
                return {}
        return {}

    def _save_state(self, state: dict[str, Any]):
        FILE_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = FILE_STATE_FILE.with_suffix(".json.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
        os.replace(tmp, FILE_STATE_FILE)

    # ── 分块规则匹配 ──────────────────────────────────────
    def _get_chunk_params(self, filename: str) -> tuple[int | None, int | None]:
        """Return (chunk_size, overlap) for a given filename.

        Iterates chunk_rules in order; first rule whose 'pattern' is a
        case-insensitive substring of the filename wins.
        Returns (None, None) to fall back to the KB plugin defaults.
        """
        name_lower = filename.lower()
        for rule in self._chunk_rules:
            pattern = str(rule.get("pattern", "")).lower()
            if pattern and pattern in name_lower:
                cs = rule.get("chunk_size") or None
                ov = rule.get("overlap") or None
                return (int(cs) if cs else None, int(ov) if ov else None)
        return (None, None)

    # ── 文件扫描 ──────────────────────────────────────────
    def _scan_files(self) -> list[pathlib.Path]:
        if not self._obsidian_dir.exists():
            logger.warning(f"[ObsidianSync] 目录不存在: {self._obsidian_dir}")
            return []
        return [f for f in self._obsidian_dir.rglob("*.md") if ".obsidian" not in f.parts]

    # ── 文档名规范化 ──────────────────────────────────────
    @staticmethod
    def _normalize_doc_name(path: pathlib.Path, text: str) -> str:
        """从文件前 8 行提取 # 标题作为文档名，找不到就用文件名。"""
        lines = text.lstrip("\ufeff").splitlines()
        for line in lines[:8]:
            s = line.strip()
            if s.startswith("# "):
                title = s[2:].strip()
                if title:
                    return title
        return path.stem

    # ── 核心同步逻辑 ─────────────────────────────────────
    def _do_sync(self) -> tuple[bool, str]:
        """
        核心同步入口。通过 _sync_lock 保证同一时刻只有一个同步任务在执行。
        返回 (ok, message)。
        """
        if not self._sync_lock.acquire(blocking=False):
            logger.info("[ObsidianSync] 另一个同步任务正在执行，跳过本次")
            return False, "sync already in progress"
        try:
            return self._do_sync_inner()
        finally:
            self._last_sync_end_time = datetime.datetime.now().timestamp()
            self._sync_lock.release()

    def _run_async(self, coro):
        """Run a coroutine from a background thread using the captured event loop."""
        if self._loop is None or not self._loop.is_running():
            raise RuntimeError("Event loop not available")
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return future.result(timeout=300)

    def _do_sync_inner(self) -> tuple[bool, str]:
        """
        Sync logic: scan md files, detect changes, write all valid files
        into the legacy KB plugin's VectorDB (full replacement on every sync).
        """
        all_files = self._scan_files()
        if not all_files:
            msg = "obsidian dir empty or missing"
            self._write_status(ok=False, stage="scan", message=msg, changed=0, kb_name=self._kb_name)
            self._persist_readonly_status(ok=False, message=msg, stage="scan", changed=0)
            return False, msg

        # Skip oversized files
        oversized = [f for f in all_files if f.stat().st_size > MAX_FILE_SIZE_BYTES]
        if oversized:
            logger.warning(
                f"[ObsidianSync] {len(oversized)} files exceed {MAX_FILE_SIZE_BYTES // 1024}KB, skipped: "
                + ", ".join(f.name for f in oversized[:5])
                + ("..." if len(oversized) > 5 else "")
            )
        valid_files = [f for f in all_files if f.stat().st_size <= MAX_FILE_SIZE_BYTES]

        # Detect changes (mtime + size)
        old_state = self._load_state().get("files", {})
        current_paths = set()
        new_state = {}
        changed_files = []
        for md in valid_files:
            path_str = str(md)
            current_paths.add(path_str)
            st = md.stat()
            new_state[path_str] = {"mtime_ns": st.st_mtime_ns, "size": st.st_size}
            prev = old_state.get(path_str)
            if not prev or prev.get("mtime_ns") != st.st_mtime_ns or prev.get("size") != st.st_size:
                changed_files.append(md)

        deleted_paths = [p for p in old_state.keys() if p not in current_paths]
        has_changes = bool(changed_files) or bool(deleted_paths)

        if not has_changes:
            logger.info("[ObsidianSync] No changes, skipping sync")
            self._write_status(ok=True, stage="idle", message="no changes", changed=0, deleted=0, kb_name=self._kb_name)
            self._persist_readonly_status(ok=True, message="无变更，跳过", stage="idle", changed=0, deleted=0)
            return True, "no changes"

        logger.info(f"[ObsidianSync] Changes detected: {len(changed_files)} modified, {len(deleted_paths)} deleted")
        self._write_status(ok=True, stage="scan", message="changes detected", changed=len(changed_files), deleted=len(deleted_paths), kb_name=self._kb_name)

        # Read all valid files
        docs_to_write: list[tuple[str, str]] = []  # (source_rel_path, text)
        for md in valid_files:
            try:
                text = md.read_text(encoding="utf-8")
                if text.strip():
                    rel = _posix_relative(md, self._obsidian_dir)
                    docs_to_write.append((rel, text))
            except (OSError, UnicodeDecodeError) as e:
                logger.warning(f"[ObsidianSync] Failed to read {md.name}: {e}")

        if not docs_to_write:
            msg = "all files empty"
            self._write_status(ok=True, stage="build", message=msg, changed=len(changed_files), kb_name=self._kb_name)
            self._persist_readonly_status(ok=True, message="文件均为空", stage="build", changed=len(changed_files))
            return True, msg

        # Write to legacy KB plugin
        try:
            total = self._run_async(self._write_to_legacy_kb(docs_to_write))
        except Exception as e:
            logger.error(f"[ObsidianSync] KB write failed: {e}")
            self._write_status(ok=False, stage="build", message=str(e)[:300], changed=len(changed_files), kb_name=self._kb_name)
            self._persist_readonly_status(ok=False, message=f"写入失败: {str(e)[:200]}", stage="build", changed=len(changed_files))
            return False, str(e)

        # Only persist state after successful KB write
        self._save_state({"files": new_state, "updated_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")})
        result_msg = f"sync ok (总{total}条向量)"
        logger.info(f"[ObsidianSync] Sync complete. {total} chunks written to KB '{self._kb_name}'")
        self._write_status(ok=True, stage="build", message="sync ok", changed=len(changed_files), deleted=len(deleted_paths), kb_name=self._kb_name)
        self._persist_readonly_status(ok=True, message=result_msg, stage="build", changed=len(changed_files), deleted=len(deleted_paths))
        return True, result_msg

    async def _write_to_legacy_kb(self, docs_to_write: list[tuple[str, str]]) -> int:
        """Write all Obsidian files into the legacy KB plugin's VectorDB.

        Full replacement: delete + recreate collection, then bulk-insert chunks.
        Returns the total number of chunks written.
        """
        import sys as _sys

        # Locate legacy KB plugin
        legacy_meta = self.context.get_registered_star("astrbot_plugin_knowledge_base")
        if legacy_meta is None:
            raise RuntimeError("astrbot_plugin_knowledge_base not loaded")
        kb_plugin = getattr(legacy_meta, "star_cls", None)
        if kb_plugin is None or getattr(kb_plugin, "vector_db", None) is None:
            raise RuntimeError("astrbot_plugin_knowledge_base not fully initialized")

        vector_db = kb_plugin.vector_db
        text_splitter = kb_plugin.text_splitter

        # Locate Document class from already-loaded module
        Document = None
        for key, mod in _sys.modules.items():
            if key.endswith("astrbot_plugin_knowledge_base.vector_store.base"):
                Document = getattr(mod, "Document", None)
                break
        if Document is None:
            raise RuntimeError("Cannot find Document class from astrbot_plugin_knowledge_base")

        # Full replacement
        if await vector_db.collection_exists(self._kb_name):
            await vector_db.delete_collection(self._kb_name)
        await vector_db.create_collection(self._kb_name)

        total_chunks = 0
        for rel_path, text in docs_to_write:
            filename = rel_path.split("/")[-1]
            cs, ov = self._get_chunk_params(filename)
            chunks = text_splitter.split_text(text, chunk_size=cs, overlap=ov)
            if not chunks:
                continue
            documents = [
                Document(
                    text_content=chunk,
                    metadata={"source": rel_path},
                )
                for chunk in chunks
            ]
            await vector_db.add_documents(self._kb_name, documents)
            total_chunks += len(chunks)

        return total_chunks

    # ── 同步循环（后台线程） ─────────────────────────────
    def _sync_loop(self):
        if self._sync_on_startup:
            try:
                logger.info("[ObsidianSync] 启动时执行首次同步...")
                self._do_sync()
            except Exception as e:
                logger.exception(f"[ObsidianSync] 初始同步出错: {e}")
                self._write_status(ok=False, stage="startup", message=str(e)[:300], changed=0, kb_name=self._kb_name)
                self._persist_readonly_status(ok=False, message=str(e)[:300], stage="startup", changed=0)
        else:
            logger.info("[ObsidianSync] 已跳过启动时同步，等待下一个计划时间点")

        while not self._stop_event.is_set():
            self._reload_config()
            wait = self._get_wait_seconds()
            remaining = wait
            while remaining > 0 and not self._stop_event.is_set():
                chunk = min(15, remaining)
                self._stop_event.wait(chunk)
                remaining -= chunk

            # 检测两种手动触发方式
            if self._check_manual_sync() or self._manual_trigger.is_set():
                logger.info("[ObsidianSync] 检测到手动同步请求，立即执行...")
                self._manual_trigger.clear()
                break

            if self._stop_event.is_set():
                break

            try:
                self._do_sync()
            except Exception as e:
                logger.exception(f"[ObsidianSync] 同步出错: {e}")
                self._write_status(ok=False, stage="sync", message=str(e)[:300], changed=0, kb_name=self._kb_name)
                self._persist_readonly_status(ok=False, message=str(e)[:300], stage="sync", changed=0)

    # ── 聊天指令 ──────────────────────────────────────────
    @filter.command("obsync")
    async def manual_sync(self, event: AstrMessageEvent):
        '''手动触发 Obsidian 知识库同步'''
        if not self._is_admin_or_allowed(event):
            yield event.plain_result("你没有权限使用这个命令。")
            return
        try:
            # 在线程池中执行同步，不阻塞事件循环
            loop = asyncio.get_running_loop()
            ok, msg = await loop.run_in_executor(None, self._do_sync)
            yield event.plain_result(f"Obsidian 同步{'完成' if ok else '失败'}！{msg}")
        except Exception as e:
            yield event.plain_result(f"同步出错: {e}")

    @filter.command("obsync_status")
    async def sync_status(self, event: AstrMessageEvent):
        '''查看 Obsidian 同步状态'''
        if not self._is_admin_or_allowed(event):
            yield event.plain_result("你没有权限查看这个状态。")
            return
        try:
            if STATUS_FILE.exists():
                data = json.loads(STATUS_FILE.read_text(encoding="utf-8-sig"))
                msg = (
                    f"最后同步: {data.get('updated_at', 'unknown')}\n"
                    f"状态: {'成功' if data.get('ok') else '失败'}\n"
                    f"阶段: {data.get('stage', 'unknown')}\n"
                    f"说明: {data.get('message', '')}\n"
                    f"知识库: {data.get('kb_name', self._kb_name)}\n"
                    f"变更数: {data.get('changed', 0)}"
                )
            else:
                msg = "还没有同步状态记录。"
            yield event.plain_result(msg)
        except (json.JSONDecodeError, OSError) as e:
            yield event.plain_result(f"读取状态失败: {e}")

    # ── 记忆文件读写 LLM Tool ─────────────────────────────

    @filter.llm_tool(name="memory_write")
    async def memory_write(
        self,
        event: AstrMessageEvent,
        operation: str,
        filename: str = "",
        content: str = "",
        search_text: str = "",
        replace_text: str = "",
        section: str = "",
    ):
        """当用户要求记住、更新、修改或删除某件事时，调用此工具读写本地 Markdown 记忆文件。

        操作流程：
        1. 先调用 operation=list 获取所有可用文件名，根据文件名语义判断目标文件。
        2. 确定目标文件后，必须先调用 operation=read 读取文件全文，理解其已有的
           Markdown 格式、章节结构和表述风格，再决定如何自然融合新信息。
           不允许在未读取文件内容的情况下直接写入。
        3. 根据读取结果判断：若信息已存在则用 edit 修改，若是新增信息则用 add 追加，
           若要删除则用 delete，尽量将内容融入合适的章节，而不是生硬地追加到末尾。
        4. 若没有合适的文件，先询问用户是否新建，用户确认后再调用 create_file。

        Args:
            operation(string): 操作类型：
                list = 列出所有记忆文件名（不读取内容）；
                read = 读取指定文件完整内容，用于写入前理解结构；
                add  = 在指定章节末追加内容，section 为空则追加到文件末尾；
                edit = 将文件中的 search_text 精确替换为 replace_text；
                delete = 删除文件中的 search_text（replace_text 留空）；
                create_file = 新建文件并写入初始内容（需先获得用户确认）
            filename(string): 目标文件名，含扩展名，如 "国哥.md"；list 操作可留空
            content(string): 要写入或追加的内容；add / create_file 操作使用
            search_text(string): 要查找的文本；edit / delete 操作使用
            replace_text(string): 替换后的文本；edit 操作使用，delete 操作留空
            section(string): add 操作的目标章节标题，如 "作息"；找不到时追加到末尾
        """
        if self._memory_dir is None:
            return self._llm_tool_text_result("记忆目录未配置，请在插件设置中填写 memory_dir 路径。")

        memory_dir = self._memory_dir

        if operation == "list":
            if not memory_dir.exists():
                return self._llm_tool_text_result(f"记忆目录不存在: {memory_dir}")
            files = sorted(p.name for p in memory_dir.glob("*.md"))
            if not files:
                return self._llm_tool_text_result("记忆目录中暂无 .md 文件。")
            return self._llm_tool_text_result("可用记忆文件：\n" + "\n".join(f"- {f}" for f in files))

        if operation == "read":
            if not filename:
                return self._llm_tool_text_result("read 操作需要提供 filename。")
            target = memory_dir / filename
            if not target.exists():
                return self._llm_tool_text_result(f"文件不存在: {filename}")
            try:
                text = target.read_text(encoding="utf-8")
                return self._llm_tool_text_result(f"文件内容（{filename}）：\n\n{text}")
            except OSError as e:
                return self._llm_tool_text_result(f"读取失败: {e}")

        if operation == "add":
            if not filename or not content:
                return self._llm_tool_text_result("add 操作需要提供 filename 和 content。")
            target = memory_dir / filename
            if not target.exists():
                return self._llm_tool_text_result(f"文件不存在: {filename}，如需新建请使用 create_file。")
            try:
                text = target.read_text(encoding="utf-8")
                if section:
                    # Find the section heading and insert after the last line of that section
                    lines = text.splitlines(keepends=True)
                    insert_pos = None
                    in_section = False
                    for i, line in enumerate(lines):
                        stripped = line.strip()
                        if stripped.lstrip("#").strip() == section and stripped.startswith("#"):
                            in_section = True
                            continue
                        if in_section:
                            # Next heading at same or higher level marks end of section
                            if stripped.startswith("#"):
                                insert_pos = i
                                break
                    if insert_pos is None and in_section:
                        insert_pos = len(lines)
                    if insert_pos is not None:
                        lines.insert(insert_pos, content.rstrip("\n") + "\n")
                        text = "".join(lines)
                    else:
                        # Section not found, append to end
                        text = text.rstrip("\n") + "\n\n" + content.rstrip("\n") + "\n"
                else:
                    text = text.rstrip("\n") + "\n\n" + content.rstrip("\n") + "\n"
                target.write_text(text, encoding="utf-8")
                return self._llm_tool_text_result(f"已写入 {filename}。")
            except OSError as e:
                return self._llm_tool_text_result(f"写入失败: {e}")

        if operation in ("edit", "delete"):
            if not filename or not search_text:
                return self._llm_tool_text_result(f"{operation} 操作需要提供 filename 和 search_text。")
            target = memory_dir / filename
            if not target.exists():
                return self._llm_tool_text_result(f"文件不存在: {filename}")
            try:
                text = target.read_text(encoding="utf-8")
                if search_text not in text:
                    return self._llm_tool_text_result(
                        f"未在 {filename} 中找到指定文本，请确认内容是否正确。"
                    )
                replacement = replace_text if operation == "edit" else ""
                text = text.replace(search_text, replacement, 1)
                target.write_text(text, encoding="utf-8")
                action = "修改" if operation == "edit" else "删除"
                return self._llm_tool_text_result(f"已在 {filename} 中完成{action}。")
            except OSError as e:
                return self._llm_tool_text_result(f"操作失败: {e}")

        if operation == "create_file":
            if not filename:
                return self._llm_tool_text_result("create_file 操作需要提供 filename。")
            if not filename.endswith(".md"):
                filename = filename + ".md"
            target = memory_dir / filename
            if target.exists():
                return self._llm_tool_text_result(f"文件 {filename} 已存在，请使用 add 或 edit 操作。")
            try:
                memory_dir.mkdir(parents=True, exist_ok=True)
                initial = content if content else f"# {filename[:-3]}\n"
                target.write_text(initial, encoding="utf-8")
                return self._llm_tool_text_result(f"已创建 {filename}。")
            except OSError as e:
                return self._llm_tool_text_result(f"创建失败: {e}")

        return self._llm_tool_text_result(f"未知操作类型: {operation}")

    @staticmethod
    def _llm_tool_text_result(text: str) -> str:
        """Return a plain text string as an LLM tool result."""
        return text

    # ── 生命周期 ──────────────────────────────────────────
    async def terminate(self):
        self._stop_event.set()
        self._manual_trigger.set()  # 唤醒可能在等待的循环
        if self._thread:
            self._thread.join(timeout=5)
        logger.info("[ObsidianSync] 已停止")
