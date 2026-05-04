# Retrieval Eval Report — phase3_bge_m3_with_reranker
_2026-05-03 22:53, k=5_

## Headline
- **Recall (any expected source hit) @5**: **96.7%** (29/30)
- **Recall (all expected sources hit) @5**: **80.0%** (24/30)
- Adversarial queries: 5 (refusal not yet evaluated — needs LLM step)

## By category

| Category | Count | Recall (any) | Recall (all) |
|---|---|---|---|
| factual | 14 | 100% (14/14) | 79% (11/14) |
| multi_source | 2 | 100% (2/2) | 0% (0/2) |
| multilingual | 3 | 100% (3/3) | 100% (3/3) |
| real_qa | 9 | 89% (8/9) | 89% (8/9) |
| summarization | 2 | 100% (2/2) | 100% (2/2) |

## By language

| Language | Count | Recall (any) | Recall (all) |
|---|---|---|---|
| ar | 1 | 100% (1/1) | 100% (1/1) |
| en | 28 | 96% (27/28) | 79% (22/28) |
| fr | 1 | 100% (1/1) | 100% (1/1) |

## Failures (any expected source MISSED)

### Q002 — `real_qa`, lang=`en`
**Query**: What should I include in my CV as an ENSIA student?

**Expected sources**:
  - chat msg 393
  - chat msg 575

**Top 5 retrieved**:
  - [0.205] `chat_394` (chat msg 394 | Q&A) — Anything you would like to say  Any advice about what  should | do after graduation from ensia?  This includes, but is n…
  - [0.191] `chat_884` (chat msg 884 | Research) — [Platform to Connect Students with Research Teams]  Salam everyone, As you know, students at our school currently lack a…
  - [0.102] `chat_363` (chat msg 363 | Q&A) — I’d like to ask you: if I were an ENSIA student, how could I create value?
  - [0.089] `chat_410` (chat msg 410 | Welcome board) — Hi everyone,   The "ENSIA Impact" server has two main goals:  1. Explore Career Opportunities: We’re here to share infor…
  - [0.058] `chat_846` (chat msg 846 | ENSIA Incubator) — I'm receiving many questions about the Pre-Incubation Program, so here are some key answers:  Q1: Can students from all …

## Adversarial queries (top-1 retrieval scores)

_Low scores here are good — they mean the retriever isn't confident._

| ID | Query | Top-1 score | Top-1 source |
|---|---|---|---|
| Q031 | What is the salary of an ENSIA professor?… | 0.006 | `chat_803` (Startups) |
| Q032 | Who is the dean of ENSIA in 2026?… | 0.176 | `chat_806` (ENSIA Incubator) |
| Q033 | What is the password to access the ENSIA Impact group?… | 0.083 | `chat_526` (Ai resources) |
| Q034 | Did Sami Belkacem write a book on entrepreneurship?… | 0.002 | `chat_1028` (Q&A) |
| Q035 | What are the official tuition fees at ENSIA for internationa… | 0.029 | `chat_1255` (Ai resources) |

## Per-query detail

| ID | Cat | Lang | Recall (any) | Recall (all) | Expected → top-k positions |
|---|---|---|---|---|---|
| Q001 | real_qa | en | ✅ | ✅ | 392: #1 |
| Q002 | real_qa | en | ❌ | ⚠️ | 393: ❌ / 575: ❌ |
| Q003 | real_qa | en | ✅ | ✅ | 389: #1 |
| Q004 | multilingual | ar | ✅ | ✅ | 389: #1 |
| Q005 | real_qa | en | ✅ | ✅ | 391: #1 |
| Q006 | real_qa | en | ✅ | ✅ | 396: #1 |
| Q007 | real_qa | en | ✅ | ✅ | 390: #2 |
| Q008 | real_qa | en | ✅ | ✅ | 394: #2 / 410: #1 |
| Q009 | real_qa | en | ✅ | ✅ | 395: #1 |
| Q010 | real_qa | en | ✅ | ✅ | 398: #1 |
| Q011 | factual | en | ✅ | ✅ | 832: #1 / 846: #2 |
| Q012 | factual | en | ✅ | ✅ | 846: #1 |
| Q013 | factual | en | ✅ | ⚠️ | 611: ❌ / 899: #2 |
| Q014 | factual | en | ✅ | ✅ | 899: #1 |
| Q015 | factual | en | ✅ | ✅ | 314: #1 |
| Q016 | factual | en | ✅ | ✅ | 1374: #1 / ENSIA.txt: #4 |
| Q017 | multi_source | en | ✅ | ⚠️ | 1389: #5 / اليات_تنفيذ_مشروع_القرار_1275.txt: ❌ / دليل_مشروع_للحصول_على_شهادة_مؤسسة_ناشئة.txt: ❌ |
| Q018 | factual | en | ✅ | ✅ | 867: #1 |
| Q019 | factual | en | ✅ | ✅ | Internship_Analysis.txt: #2 |
| Q020 | summarization | en | ✅ | ✅ | The_Ultimate_Guide_to_Pitch_Decks_for_Entrepreneurs.txt: #1 |
| Q021 | factual | en | ✅ | ✅ | 1323: #1 / Flyer_Company_program.txt: #2 |
| Q022 | factual | en | ✅ | ✅ | 903: #1 |
| Q023 | multilingual | fr | ✅ | ✅ | Startup_Factory_1_1.txt: #1 |
| Q024 | factual | en | ✅ | ⚠️ | 1060: #3 / 1367: ❌ |
| Q025 | summarization | en | ✅ | ✅ | Research-Lab-Reports-IMRAD.txt: #1 |
| Q026 | factual | en | ✅ | ✅ | 410: #1 |
| Q027 | multilingual | en | ✅ | ✅ | 779: #1 |
| Q028 | multi_source | en | ✅ | ⚠️ | 1389: #2 / Arreté_008.txt: ❌ |
| Q029 | factual | en | ✅ | ⚠️ | 884: #4 / 1387: ❌ / 1421: #1 |
| Q030 | factual | en | ✅ | ✅ | 1396: #1 |
| Q031 | adversarial | en | (refusal) | (refusal) | top-1: 0.006 |
| Q032 | adversarial | en | (refusal) | (refusal) | top-1: 0.176 |
| Q033 | adversarial | en | (refusal) | (refusal) | top-1: 0.083 |
| Q034 | adversarial | en | (refusal) | (refusal) | top-1: 0.002 |
| Q035 | adversarial | en | (refusal) | (refusal) | top-1: 0.029 |
