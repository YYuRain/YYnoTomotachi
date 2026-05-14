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

## 2026-05-14

- 项目部署到腾讯香港云（Docker Compose：bot + admin + postgres + mihomo + cloudflared）
- HK 出口 IP 被 OpenRouter 拒 Anthropic 模型（403），加 mihomo 容器走 Clash 订阅美区出口
- admin webUI HTTPS 化：cloudflared 临时 tunnel `*.trycloudflare.com`，零配置
- webUI 鉴权改造两次最终落地：HMAC token URL 一键登录（bot `/memory` 命令铸链接，admin 容器解 token 设 cookie；密钥靠 `data/.webui_secret` 进程间共享）
- webUI 分用户视图：admin 看全部 + 下拉切换；普通用户只看自己；移动端表格转卡片
- 命令重命名：`/admin` → `/memory`；新增 `/mypw`（后又因切 token 路径删掉）
- 新增测试 bot（TEST_BOT_TOKEN 可选）：`/become` 选虚拟身份、`/clear` 清盘、走完整邀请码流程
- 用户激活后 AI 立刻发"开场白"（welcome opener，新指令 + `agent.generate_welcome`）
- 容器时区设为 Asia/Shanghai（Dockerfile + scheduler）
- 修一系列上云/部署期 bug：`huggingface-cli` → `hf`、`.env` 烤进镜像被 dotenv override（加 `.dockerignore`）、`EMBED_MODEL_NAME` 改绝对路径、`/myid` Markdown 解析 chat_id 下划线挂、cloudflared 必须 root 才能写共享卷、cf.log 取最后一个 URL（不是第一个，cloudflared 重启 append 不截断）、login-by-token 用 200 HTML+JS 跳代替 302（移动端 cookie 更稳）
