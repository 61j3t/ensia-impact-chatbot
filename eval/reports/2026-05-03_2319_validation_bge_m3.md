# Retrieval Eval Report — validation_bge_m3
_2026-05-03 23:19, k=5_

## Headline
- **Recall (any expected source hit) @5**: **71.4%** (10/14)
- **Recall (all expected sources hit) @5**: **42.9%** (6/14)
- Adversarial queries: 5 (refusal not yet evaluated — needs LLM step)

## By category

| Category | Count | Recall (any) | Recall (all) |
|---|---|---|---|
| factual | 11 | 64% (7/11) | 45% (5/11) |
| multi_source | 2 | 100% (2/2) | 50% (1/2) |
| multilingual | 1 | 100% (1/1) | 0% (0/1) |

## By language

| Language | Count | Recall (any) | Recall (all) |
|---|---|---|---|
| en | 13 | 69% (9/13) | 46% (6/13) |
| fr | 1 | 100% (1/1) | 0% (0/1) |

## Failures (any expected source MISSED)

### V001 — `factual`, lang=`en`
**Query**: wht is teh cde at ensia

**Expected sources**:
  - chat msg 611
  - chat msg 899

**Top 5 retrieved**:
  - [0.201] `chat_824` (chat msg 824 | CDE - ENSIA) — Dear all, Saha Ftorkom  Please be informed that the CDE office is scheduled to open in the coming months. In the meantim…
  - [0.015] `chat_983` (chat msg 983 | ENSIA Incubator) — 🚀 Big Opportunity for Future Founders!  Ready to turn your innovative ideas into real solutions? ⚡ The V2V Incubator - E…
  - [0.012] `chat_806` (chat msg 806 | ENSIA Incubator) — 🚀 ENSIA AI Incubator Launching Soon!  ENSIA is launching an AI incubator to foster innovation and future AI leaders. Its…
  - [0.008] `chat_803` (chat msg 803 | Startups) — Bootcamp - Sustainable Health  Following the Alinov bootcamp on Smart Buildings and Renewable Energies, we are excited t…
  - [0.006] `chat_1364` (chat msg 1364 | Patents) — 📢 Patents & Final-Year Projects 08-1275  The V2V ENSIA Incubator, in collaboration with CATI, invites you to an awarenes…

### V006 — `factual`, lang=`en`
**Query**: How do I find AI engineering internships in Algeria?

**Expected sources**:
  - chat msg 1396
  - chat msg 575

**Top 5 retrieved**:
  - [0.560] `chat_1152` (chat msg 1152 | Companies) — El Kendi is looking for Data Talents! 💡  Join Algeria’s leading pharmaceutical company for an enriching internship in:  …
  - [0.477] `chat_1034` (chat msg 1034 | Companies) — Salam, for those looking for internships, don’t forget to check out this file containing a list of Algerian companies an…
  - [0.440] `pdf_Internship_Analysis_0` (PDF Internship_Analysis.txt | chunk 0) — 4th-Year Internship Feedback Analysis ⚠️ Important Disclaimer Please note: This analysis is based on voluntary feedback …
  - [0.169] `chat_1273` (chat msg 1273 | End-of-Study Project) — Salam everyone,  Since you will graduate this year and may consider pursuing a PhD at ENSIA,   Here are a few useful poi…
  - [0.131] `chat_1339` (chat msg 1339 | Opportunities) — 🚨 Opportunity for Future Graduates   UNRCO Algeria (🇺🇳 United Nations Resident Coordinator’s Office) is recruiting for: …

### V007 — `factual`, lang=`en`
**Query**: I want to register a startup, where do I begin?

**Expected sources**:
  - chat msg 1389
  - chat msg 832
  - chat msg 846

**Top 5 retrieved**:
  - [0.087] `pdf_Flyer_Company_program_0` (PDF Flyer_Company_program.txt | chunk 0) — INJAZ El Djazair Register into Algeria’s #1 Entrepreneurship Program YOUR JOURNEY WITH THE NEW COMPANY PROGRAM: START NO…
  - [0.068] `pdf_The_Ultimate_Guide_to_Pitch_Decks_for_Entrepreneurs_109` (PDF The_Ultimate_Guide_to_Pitch_Decks_for_Entrepreneurs.txt | chunk 109) —  to be intentional and mindful and to write a values and culture document to hire and operate by. It is probably the ver…
  - [0.031] `pdf_The_Ultimate_Guide_to_Pitch_Decks_for_Entrepreneurs_107` (PDF The_Ultimate_Guide_to_Pitch_Decks_for_Entrepreneurs.txt | chunk 107) — ou to do just that. A cheaper, though the less secure option, is to use a social media messaging app with end to end enc…
  - [0.025] `pdf_The_Ultimate_Guide_to_Pitch_Decks_for_Entrepreneurs_29` (PDF The_Ultimate_Guide_to_Pitch_Decks_for_Entrepreneurs.txt | chunk 29) — ven found these first customers become inbound investors who fund the development and building of the product and hiring…
  - [0.016] `pdf_The_Ultimate_Guide_to_Pitch_Decks_for_Entrepreneurs_91` (PDF The_Ultimate_Guide_to_Pitch_Decks_for_Entrepreneurs.txt | chunk 91) —  the right facts and data points is good but presenting the narrative in your pitch deck can be far more pivotal. How do…

### V009 — `factual`, lang=`en`
**Query**: What is this Telegram group for?

**Expected sources**:
  - chat msg 410

**Top 5 retrieved**:
  - [0.743] `chat_972` (chat msg 972 | Resources by Students) — AgrI Challenge – Official Telegram Group  ENSIA community,  The organizers of the AgrI Challenge – Edition 2 have create…
  - [0.024] `chat_633` (chat msg 633 | Welcome board) — Good morning everyone,  Please invite your classmates to join the telegram server, as it will be the primary stream for …
  - [0.018] `chat_873` (chat msg 873 | End-of-Study Project) — Salam all,  Please refer to the relevant Telegram channels in this server for the three types of end-of-study projects: …
  - [0.005] `chat_835` (chat msg 835 | Welcome board) — Salam all, Please help us improve the ENSIA Impact Telegram server by sharing your thoughts and suggestions! 📢 Fill out …
  - [0.005] `chat_1421` (chat msg 1421 | Welcome board) — Community Q&A Chatbot 🚀   Salam all,   We are looking for a volunteer student to develop a chatbot or Telegram bot that …

## Adversarial queries (top-1 retrieval scores)

_Low scores here are good — they mean the retriever isn't confident._

| ID | Query | Top-1 score | Top-1 source |
|---|---|---|---|
| V015 | What's the WiFi password at ENSIA?… | 0.004 | `chat_983` (ENSIA Incubator) |
| V016 | Is Sami Belkacem married?… | 0.020 | `chat_1028` (Q&A) |
| V017 | Quel est le budget annuel de l'ENSIA?… | 0.005 | `chat_983` (ENSIA Incubator) |
| V018 | ما هو متوسط راتب خريج ENSIA؟… | 0.009 | `chat_1017` (Welcome board) |
| V019 | What courses does the ENSIA bachelor's program include?… | 0.431 | `chat_394` (Q&A) |

## Per-query detail

| ID | Cat | Lang | Recall (any) | Recall (all) | Expected → top-k positions |
|---|---|---|---|---|---|
| V001 | factual | en | ❌ | ⚠️ | 611: ❌ / 899: ❌ |
| V002 | factual | en | ✅ | ⚠️ | 1389: ❌ / 814: #1 |
| V003 | factual | en | ✅ | ✅ | 390: #2 / 575: #3 |
| V004 | factual | en | ✅ | ✅ | 824: #1 |
| V005 | multi_source | en | ✅ | ✅ | 899: #1 |
| V006 | factual | en | ❌ | ⚠️ | 1396: ❌ / 575: ❌ |
| V007 | factual | en | ❌ | ⚠️ | 1389: ❌ / 832: ❌ / 846: ❌ |
| V008 | factual | en | ✅ | ⚠️ | 1060: #4 / 1217: ❌ / 1143: #2 |
| V009 | factual | en | ❌ | ⚠️ | 410: ❌ |
| V010 | factual | en | ✅ | ✅ | 395: #1 |
| V011 | multi_source | en | ✅ | ⚠️ | 314: #1 / 1374: ❌ |
| V012 | multilingual | fr | ✅ | ⚠️ | 1389: #4 / اليات_تنفيذ_مشروع_القرار_1275.txt: ❌ |
| V013 | factual | en | ✅ | ✅ | 396: #1 |
| V014 | factual | en | ✅ | ✅ | 832: #1 / 846: #4 |
| V015 | adversarial | en | (refusal) | (refusal) | top-1: 0.004 |
| V016 | adversarial | en | (refusal) | (refusal) | top-1: 0.020 |
| V017 | adversarial | fr | (refusal) | (refusal) | top-1: 0.005 |
| V018 | adversarial | ar | (refusal) | (refusal) | top-1: 0.009 |
| V019 | adversarial | en | (refusal) | (refusal) | top-1: 0.431 |
