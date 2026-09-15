---
title: LHGP Wiki — Index
type: moc
status: permanent
tags: [moc, audience/ai, audience/human]
audience: [human, ai]
created: 2026-09-07
last_reviewed: 2026-09-07
---

# LHGP Wiki

协议内部工作方法与知识库。**所有 wiki 页是纯 Markdown 文件,git tracked,
IDE 可搜索** —— wiki 不是数据库,是源码的延伸。

## 设计原则(参考 Obsidian 守则)

1. **tags 描述是什么,links 表达怎么连** —— 分离两个轴
2. **每页 3-5 tags** 上限;Related Topics **5-7 个**上限
3. **shallow folders**:最大 2 层,深度靠 tags
4. **frontmatter 强制 schema** —— `type / status / tags / audience` 四件套,AI 拿来就懂
5. **wikilink 锚点用 `^blockid`** —— heading 改名引用不破
6. **transclusion `![[page]]`** 用于"单一真相源"内联,避免复制

## 目录

- **[[glossary]]** — 协议术语表(每个新概念必须先在这里注册)
- **[[playbook/index|Playbook]]** — 怎么做事(添加 event type、写测试、迁移 schema …)
- **[[case-studies/index|Case Studies]]** — 实战记录(P6 fresh-machine smoke、P6 端到端验证)
- **[[topics/index|Topics]]** — 按领域聚合(每个 topic 页是 MOC:linked playbooks + case studies)

## 与 docs/ 的关系

| 容器 | 谁写 | 谁读 | 何时用 |
|---|---|---|---|
| `docs/wiki/` | 人,集体 | 人 + AI(本协议) | 工作方法,跨项目 |
| `docs/decisions/` | 人,决议当时 | 人 + AI | 已做的决策,带 ADR 编号 |
| `docs/evidence/` | 人,测试当时 | 人 + AI | 测试/验证记录,带日期 |
| `docs/ideas/` | 人,任何时候 | 人 | 想法池,未决议 |
| `docs/LHGP-SPEC.md` | 协议本体 | 人 + AI | 协议语义权威,稳定 |
| `docs/LHGP-ROADMAP.md` | 协议本体 | 人 | 路线图,长生命周期 |

如果新页内容只跟"某次具体决策"或"某次具体测试"相关,放 `decisions/` 或 `evidence/`;如果"可复用方法",放 `wiki/playbook/`;如果"协议级抽象",更新 `LHGP-SPEC.md`。

## AI 消费指南

- **入口**: `docs/wiki/.index.json`(由 `scripts/build_wiki_index.py` 重新生成)
- **入口字段**:每页的 `type` / `tags` / `related` / `audience` / `requires` 让 AI 一次性判断"这页对我有没有用"
- **不要靠人肉搜索** —— AI 拉 `.index.json` 然后按需取整页内容,而不是逐页 grep
