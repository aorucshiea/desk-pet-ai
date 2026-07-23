"""``/learn`` — build the prompt that turns a described workflow into a skill.

Ported from hermes-agent's ``agent/learn_prompt.py`` (a ~150-line pure
function that constructs a prompt). Adapted to the desk pet:

  - The tool surface is the pet's, not Hermes' — ``memory`` /
    ``observe_screen`` / ``screen_click`` / ``/skill`` instead of
    ``terminal`` / ``read_file`` / ``skill_manage``.
  - Voice is Chinese-first and matches the pet persona (direct, no fluff,
    "writes to memory = it happened" — see the persona prompt in
    ``minicpm-chat-renderer.js``).
  - The authoring standards are distilled to the pet's scale (~100 lines,
    no scripts/ subdirectory machinery — the pet authors a single SKILL.md).

The external-distillation thesis (see the project memory
``external-distillation-framework``): a capability the pet can write down as
an auditable text file is a capability it can keep, migrate, and audit —
unlike weight-level "inner distillation" which is a black box and breaks
identity continuity. ``/learn`` is the entry of the 引导→创建→索引→加载
closed loop.
"""

from __future__ import annotations


# The house-style rules for authoring a desk-pet SKILL.md. Embedded in the
# prompt so the model authors skills the way a maintainer would by hand.
# Distilled from Hermes' HARDLINE standards, scaled to the pet's scope.
_AUTHORING_STANDARDS = """\
按下面的规范写技能。这不是建议，是硬规则：

frontmatter：
- name：小写-连字符，≤64 字符，无空格。
- description：一句话，≤80 字符，以句号结尾。说能力，不说实现。不用"强大/全面/无缝/智能"这种营销词。不要重复 name。description 含冒号时整个值用双引号包起来。写完数一下字符，超了就改短——系统提示词里的技能索引会截断到 80 字符，超的部分永远路由不到。
  好：按关键词搜索用户的屏幕内容。
  坏：一个能让桌宠通过 OmniParser 智能地分析并理解用户当前屏幕内容的强大技能。
- version：0.1.0（刚学的新技能用 0.1.0，稳定后改 1.0.0）
- tags：几个相关的小写标签。

body 段落顺序（没内容的段直接省略）：
1. `# 标题` + 2-3 句话：这个技能做什么、不做什么、关键依赖（比如"只用标准库"）。
2. `## 何时用` — 具体触发场景的 bullet 列表。
3. `## 前置条件` — 需要的权限、已加载的模型、已开的开关。
4. `## 怎么做` — 规范调用方式。
5. `## 步骤` — 编号步骤，可复制粘贴的精确命令/输入。
6. `## 陷阱` — 已知限制、看起来坏了其实没坏的事。
7. `## 验证` — 一条命令或检查，证明技能生效了。

工具引用（这才是技能，不是 shell 文档）：
- 涉及记忆的，引用 `memory` 工具。
- 涉及看屏幕的，引用 `observe_screen` / `screen_click`。
- 涉及加载别的技能的，用 `/skill <name>`。
- 不要发明桌宠没有的工具。

质量：
- 优先用源里逐字出现的命令、路径、函数签名、配置键。没在源里见过的 flag/路径/API 绝不编。
- 紧凑、可扫读：简单技能 ~50 行，复杂 ~150 行。不要把源文档整段抄进来。
- 不要写一个只指向别的技能的"索引/路由"技能。
"""


def build_learn_prompt(user_request: str) -> str:
    """Build the model prompt for an open-ended ``/learn`` request.

    Args:
        user_request: the free-text the user gave after ``/learn`` — a
            description of the workflow, or empty to mean "the workflow we
            just went through in this conversation".

    Returns:
        A complete instruction the model runs as a normal turn. The model
        gathers the described sources and authors a SKILL.md via the
        ``skill_create`` builtin tool.
    """
    req = (user_request or "").strip()
    if not req:
        req = (
            "我们刚才在这段对话里走过的流程——回看刚才做了什么，把它蒸馏成一个可复用的技能。"
        )

    return (
        "[/learn] 用户想让你把下面描述的东西学成一个可复用的技能，并保存下来。\n\n"
        f"请求：\n{req}\n\n"
        "这个请求是开放式的，可能混着两类内容（顺序任意）：要采集的来源"
        "（目录、文件路径、URL、「刚才我们做了什么」、贴进来的笔记）"
        "和塑造这个技能的要求（关注什么、跳过什么、范围、命名、角度）。"
        "把请求里每一部分都当成有意义的。特别是路径或链接后面的散文不是废话——"
        "那是用户在告诉你他想从那个来源里要什么。"
        "比如「<url> 只关注认证流程，跳过废弃接口」的意思是：既采集那个 URL，"
        "又把「只关注认证、跳过废弃」当成编写技能时的约束。绝不能抓了第一个来源就不管其余。\n\n"
        "你要做的：\n"
        "1. 采集用户提到的所有来源——用你已有的工具：本地文件/目录用 `read_file`/`search_files`，"
        "URL 用 `web_extract`，刚才做过的事用当前对话历史，贴进来的文本直接用。"
        "如果范围含糊，做个合理选择并记下来，不要卡住。\n"
        "1b. 把请求里的每个要求、关注点、约束都应用到你要写的技能上——"
        "这些决定 SKILL.md 覆盖什么、强调什么，不只是你读哪些来源。\n"
        "2. 写一个 SKILL.md，用 `skill_create` 工具保存（action=\"create\"）。"
        "选个合理的分类。如果流程需要脚本，先用 `skill_create` 写主 SKILL.md，"
        "脚本内容写进 body 里引用（桌宠暂不支持 scripts/ 子目录，所以脚本片段直接嵌进 body 的代码块）。\n\n"
        f"{_AUTHORING_STANDARDS}\n\n"
        "写完后告诉用户：技能叫什么、属于哪个分类、一句话总结它抓住了什么。"
    )
