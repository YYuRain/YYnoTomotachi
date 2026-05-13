# 进展汇总 inbox

> 每次有新进展按日期追加一节。**只写做了什么**，细节走 `document/dialog-tuning-log.md` 等专门文档。

---

## 2026-05-12

- 项目推送到 GitHub 私有仓库（`YYuRain/YYnoTomotachi`）
- `me/img/` 纳入 git，文档图片链接补 GitHub 兼容格式
- 主聊天模型切换：`kimi-k2.6` → `anthropic/claude-sonnet-4.6`（via OpenRouter）
- memU 抽取/分类模型切换：MiniMax → `deepseek/deepseek-v4-flash`（via OpenRouter）
- 修复链接读取鉴权失败（Jina API key + 显式代理绕开 macOS LibreSSL bug）
- 短期上下文 `_recent` 持久化（写盘 `data/recent.json`，重启不再清）
- memU 召回的每条记忆带上形成日期 `(YYYY-MM-DD)`，让 AI 区分新旧背景
- 日常聊天风格微调：从偏 INTP 平淡 → 略 ENTP，偶尔抖机灵但不刻意做作

## 2026-05-13

- 多用户化改造：所有存储（SQLite 5 表 + memU postgres + recent.json）加 `user_id` 维度
- 邀请码准入机制：admin `/invite <n>` 生成、用户 `/start <code>` 激活
- bot 入口重写：未激活用户 silent drop；新增 `/myid` `/invite` `/users` 命令
- scheduler 多用户 fan-out：4 个 job 都按活跃用户列表 `asyncio.gather` 派发，`Semaphore(5)` 限并发
- 写迁移脚本 `scripts/migrate_to_multiuser.py`：备份 → 加 `user_id` 列 → 复制旧 me 数据归到 admin → wrap recent.json → UPDATE memU postgres
- 部署形态：Docker Compose（bot + postgres + admin），HF 模型烤进镜像
- 本地迁移并冒烟通过：旧 me 数据（144 interests / 314 reply_samples / 552 memU 行 / 24 短期上下文）全部归到 admin chat_id 名下，单用户路径仍跑得通
