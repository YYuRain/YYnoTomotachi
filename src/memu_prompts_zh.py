"""把 memU 的记忆抽取 / 分类概述提示词改成中文输出版。

memU 默认的 extraction template 是英文 + 注了一句"use same language as resource"，
但 MiniMax-M2 / Claude 面对英文 scaffolding 时倾向于英文输出。这里把整体 prompt 换成中文，
强制中文抽取 + 分类摘要。

使用方式：在 `memory._get_service()` 里把这几个 dict 塞进 `memorize_config`。
"""
from __future__ import annotations

from memu.app.settings import CategoryConfig  # type: ignore


# ============ 记忆项抽取（profile / event）============

_PROFILE_PROMPT = """# 任务
你要阅读下面一段用户与助手的对话，从中**只**提取关于**用户本人**的画像类（profile）记忆项——
即用户持久的特征、偏好、身份、状态、习惯类事实。

## 原始资源
<resource>
{resource}
</resource>

## 可用分类
{categories_str}

## 抽取要求
1. **全部用中文输出**，不要英文，不要翻译原文的英语片段——用自己的话中文重述。
2. 指代用户时一律用"用户"。不要用"他/她/你"。
3. 每条记忆独立自洽——一个人读这一条也能明白在说什么，不依赖其他条。
4. 陈述句，朴素描述。不要反问、不要省略号。
5. **不要提取助手的建议、推测、或对话里的一次性琐事**（今天天气怎样、某一瞬的情绪、临时卡顿等）。
6. 只提取能放进"给定分类"的信息；和分类无关的内容直接忽略，不要硬套。
7. 同一条事实可以挂到多个分类下；也可以一条都不挂（"categories": []）。
8. 不要新建分类。
9. 每条 ≤ 80 字。

## 示例
好：
- 用户正在做一个陪伴型 AI 项目，核心在对话风格与长期记忆。
- 用户最近在减肥，喜欢用土豆替代主食。

坏：
- 用户说了"我累了"。（一次性情绪，不是 profile）
- 用户喜欢这家餐厅。（哪家？信息缺失，不自洽）

## 输出格式（严格 XML，不要多余文字、不要包 ```xml 代码块）
所有记忆放在一个 `<item>` 根节点下，每条记忆一个 `<memory>` 子节点：
<item>
    <memory>
        <content>一条中文画像事实</content>
        <categories>
            <category>personal_info</category>
        </categories>
    </memory>
    <memory>
        <content>另一条中文画像事实</content>
        <categories>
            <category>preferences</category>
            <category>habits</category>
        </categories>
    </memory>
</item>

如果一条记忆不属于任何分类，`<categories>` 留空即可（保留空 tag）。
如果整段对话提取不出 profile 记忆，输出 `<item></item>`。"""


_EVENT_PROMPT = """# 任务
你要阅读下面一段用户与助手的对话，**只**提取"用户参与或身边发生的具体事件"。
事件 = 发生在具体时间点 / 时间段的、有起因和过程的真实经历。

## 原始资源
<resource>
{resource}
</resource>

## 可用分类
{categories_str}

## 抽取要求
1. **全部用中文输出**。不要英文，不要翻译成英文。
2. 指代用户用"用户"。
3. 每条独立自洽——尽量带上时间、地点、参与者、结果。
4. 只提取用户亲口叙述或确认发生的事件。助手的建议/猜想不要。
5. **不是事件不要抽**：画像、习惯、偏好、观点不要放这里。
6. 临时情绪、一次性小事（如"今天下雨"）不要。
7. 相关性弱于所给分类的事件，可以"categories": []，不要硬塞。
8. 每条 ≤ 100 字。

## 示例
好：
- 用户周末和家人去郊外公园徒步，在那里野餐，过得很开心。
- 用户这周试了一个新减脂餐：鸡胸 + 芹菜 + 土豆，吃完觉得还行。

坏：
- 用户去徒步了。（时间、地点、同伴缺失）
- 用户喜欢减脂餐。（偏好不是事件）

## 输出格式（严格 XML，不要多余文字、不要包 ```xml 代码块）
所有事件放在一个 `<item>` 根节点下，每条事件一个 `<memory>` 子节点：
<item>
    <memory>
        <content>一条中文事件描述</content>
        <categories>
            <category>experiences</category>
        </categories>
    </memory>
    <memory>
        <content>另一条中文事件描述</content>
        <categories>
            <category>activities</category>
        </categories>
    </memory>
</item>

如果一条事件不属于任何分类，`<categories>` 留空即可（保留空 tag）。
如果整段对话提取不出事件，输出 `<item></item>`。"""


MEMORY_TYPE_PROMPTS_ZH: dict[str, str] = {
    "profile": _PROFILE_PROMPT,
    "event": _EVENT_PROMPT,
}


# ============ 分类摘要（category summary）============

CATEGORY_SUMMARY_PROMPT_ZH = """# 任务
你在维护一个分类的"综合摘要"。给你：
- 该分类之前的摘要（可能为空）
- 最新加入这个分类的一些记忆条目

目标：把旧摘要 + 新条目 融合成一段**中文**的连贯描述，
用一两段话概括这个分类下用户的总体情况，而不是罗列条目。

## 分类名称
{category}

## 旧摘要
{original_content}

## 新加入的条目
{new_memory_items_text}

## 要求
1. **中文输出**，不要英文。
2. 用第三人称"用户"表述。
3. ≤ {target_length} 字。
4. 如果新条目与旧摘要重复，就合并或忽略，不要堆砌。
5. 不要列 bullet、不要加标题、不要引用原文；直接写一段话。
6. 不要在输出里写多余的解释或"以下是摘要"之类的话。

直接输出摘要正文。"""


# ============ 分类定义（中文描述）============

MEMORY_CATEGORIES_ZH: list[CategoryConfig] = [
    CategoryConfig(name="personal_info", description="用户的基本身份、职业、背景、年龄段、所在城市等"),
    CategoryConfig(name="preferences", description="用户的喜好与厌恶：食物、影视、游戏、音乐、风格等"),
    CategoryConfig(name="relationships", description="和用户关系密切的人：家人、朋友、同事、宠物的信息及互动"),
    CategoryConfig(name="activities", description="用户参与的活动、兴趣爱好、日常在做的事"),
    CategoryConfig(name="goals", description="用户的目标、计划、在推进的事、长期追求"),
    CategoryConfig(name="experiences", description="具体发生过的事件、经历、故事"),
    CategoryConfig(name="knowledge", description="用户懂的领域、掌握的技能、专业方向"),
    CategoryConfig(name="opinions", description="用户的观点、判断、看法、立场"),
    CategoryConfig(name="habits", description="用户的生活习惯、作息规律、重复性行为"),
    CategoryConfig(name="work_life", description="工作内容、工作环境、同事、职业状态、项目"),
]
