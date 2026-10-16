from sqlalchemy import select, text
from .models import Template, TemplateVersion

BRIEF = """# 一句话贡献
用一句话说明研究问题与相对已有工作的增量。[E1]

## 关键成果
写出最重要的结果、比较对象、实验条件和代价。

## 核心方法
用输入 → 关键操作 → 输出解释机制，避免只列术语。

## 可借鉴的经验
最多三条：条件 → 做法 → 观察到的效果 → 适用边界 → 来源。

## 边界与反例
区分已知失败、未经验证与外部不同证据。"""
FULL = """# 研究问题与相关工作
说明论文解决的问题及相对最相关工作的具体差异。

## 贡献与成果
列出关键比较条件、指标、结果和成本，并引用原文。

## 方法如何运作
解释输入、每个关键步骤、输出及创新所在，必要时用公式或表格。

## 实验与消融
解释哪些证据支持哪些结论，哪些变量没有得到控制。

## 可迁移经验
条件 → 做法 → 效果 → 适用边界 → 来源；区分作者解释与系统推断。

## 局限、反例与相关论文
核对跨论文设置是否可比，标注外部全文覆盖情况。

## 仍需验证的问题
明确证据缺失和未核验图表。"""
INSTRUCTIONS = "默认中文，保留必要英文术语。精华版约600–1000字，优先知识密度。关键主张使用[E1](evidence:E1)格式引用。证据不足时少写，不编造经验，不将单篇失败泛化为路线不可行。理论、评测、数据集论文按实际贡献解释。"

def ensure_builtin(db):
    db.execute(text("SELECT pg_advisory_xact_lock(921832)"))
    template = db.scalar(select(Template).where(Template.is_builtin.is_(True)))
    if template:
        return template
    template = Template(name="研究贡献与可迁移经验", is_builtin=True)
    db.add(template)
    db.flush()
    version = TemplateVersion(template_id=template.id, number=1, brief_markdown=BRIEF, full_markdown=FULL, instructions=INSTRUCTIONS)
    db.add(version)
    db.flush()
    template.current_version_id = version.id
    return template
