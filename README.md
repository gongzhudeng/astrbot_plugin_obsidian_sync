# 灵犀 · Obsidian 知识库同步

**作者：** [gongzhudeng](https://github.com/gongzhudeng)  
**仓库：** https://github.com/gongzhudeng/astrbot_plugin_obsidian_sync  
**版本：** 1.1.0

> 本插件基于 [牧濑红莉栖](https://github.com/Mokarei) 的原始 obsidian_sync 插件重写，感谢原作者的架构设计与思路。

---

## 功能简介

定时监听本地 Obsidian 目录，检测 `.md` 文件变更后，自动将全部笔记全量同步到
`astrbot_plugin_knowledge_base` 插件的向量数据库中，供 AstrBot AI 检索使用。

- 支持每日定时同步与固定间隔同步两种模式
- 文件变更检测（mtime + 文件大小），无变更不触发同步
- 全量替换：每次同步先删除旧集合再重建，保证知识库与本地一致
- **按文件名差异化分块**：通过 `chunk_rules` 配置，对不同文件单独指定 `chunk_size` 和 `overlap`
- WebUI 配置面板：支持所有参数在线配置，无需重启
- 支持手动触发命令 `/obsync`，状态查询命令 `/obsync_status`
- 支持命令权限管控白名单
- 依赖 `astrbot_plugin_knowledge_base` 提供向量数据库，无其他外部依赖

---

## 安装依赖

需提前安装并启用 `astrbot_plugin_knowledge_base` 插件。

---

## WebUI 配置说明

| 字段 | 说明 |
|------|------|
| `obsidian_dir` | 本地 Obsidian vault 完整路径 |
| `sync_mode` | `daily`=每日定时，`interval`=固定间隔 |
| `sync_daily_time` | 每日同步时间，格式 `HH:MM`，如 `03:00` |
| `sync_interval_hours` | 间隔模式下每隔多少小时同步一次 |
| `sync_on_startup` | 启动时是否立即执行一次同步 |
| `sync_now_request` | 勾选后立即触发一次同步 |
| `kb_name` | 写入的知识库集合名称，需与知识库插件中一致 |
| `restrict_commands` | 是否限制 `/obsync` 命令权限 |
| `admin_user_ids` | 管理员 QQ 号列表 |
| `allowed_user_ids` | 额外允许使用命令的 QQ 号列表 |
| `chunk_rules` | 按文件名匹配的差异化分块规则（见下方说明） |

---

## 按文件名差异化分块（chunk_rules）

不同笔记内容长度差异较大时，可以为特定文件单独指定分块大小和重叠量。

**配置格式（JSON 列表）：**

```json
[
  {"pattern": "基础信息", "chunk_size": 150, "overlap": 10},
  {"pattern": "爱好",     "chunk_size": 200, "overlap": 20},
  {"pattern": "邻居",     "chunk_size": 600, "overlap": 60}
]
```

**匹配规则：**

- `pattern` 为文件名子串匹配，不区分大小写
- 列表顺序即优先级，第一条命中的规则生效
- 无命中的文件使用 `astrbot_plugin_knowledge_base` 插件的默认分块配置

**示例效果：**

| 文件名 | 命中规则 | chunk_size | overlap |
|--------|----------|-----------|---------|
| `刘佳怡基础信息 - 副本.md` | `基础信息` | 150 | 10 |
| `楼下邻居详细描述.md` | `邻居` | 600 | 60 |
| `日记2026.md` | 无命中 | （插件默认） | （插件默认） |

---

## 聊天命令

| 命令 | 说明 |
|------|------|
| `/obsync` | 立即触发一次全量同步 |
| `/obsync_status` | 查看上次同步时间、状态和变更数 |

---

## 发布历史

### v1.1.0
- 新增 `chunk_rules`：支持按文件名为不同文件指定独立的 `chunk_size` / `overlap`
- 修复状态缓存过早写入导致变更永远检测不到的 bug（知识库写入失败时现在不会提前标记文件为已同步）
- 更新作者信息与插件说明

### v1.0.0
- 重写插件：删除外部子进程依赖（embed.py / build_kb.py），直接对接 `astrbot_plugin_knowledge_base` 的 `vector_db.add_documents()`
- 全量替换策略：每次同步 delete + create + 写入
- 复用老知识库插件的 `text_splitter` 实例，分块参数与插件设置一致
- 文件变更检测（mtime + size），无变更跳过
- 支持 WebUI 配置面板热更新

### v0.8.1（原作者版本）
- 原始版本，依赖外部嵌入脚本（已废弃）