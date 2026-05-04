"""Run the golden test set through the retriever and produce a report.

For each non-adversarial query:
  - Retrieve top-k chunks
  - Mark which expected_sources were hit
  - Report Recall@k (per query and aggregated)

For adversarial queries (must_refuse=true):
  - Retrieve top-k anyway
  - Report the top-1 score as a refusal-confidence proxy
    (the chatbot will eventually use this score + LLM judgment to decide
     whether to refuse; for now we just record what the retriever returned)

Output: eval/reports/<timestamp>_<label>.md

Usage:
    .venv/bin/python eval/run_eval.py
    .venv/bin/python eval/run_eval.py --k 10 --label phase1_top10
"""

from __future__ import annotations

import argparse
import json
import unicodedata
from datetime import datetime
from pathlib import Path

from chatbot.retrieve import Retriever

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SET = ROOT / "eval/golden_set.json"
REPORTS = ROOT / "eval/reports"


def nfc(s: str) -> str:
    return unicodedata.normalize("NFC", s)


def hit_matches_expected(hit: dict, expected: dict) -> bool:
    md = hit["metadata"]
    if expected["kind"] == "message":
        return (
            md.get("source_type") == "chat"
            and md.get("message_id") == expected["ref"]
        )
    if expected["kind"] == "pdf":
        if md.get("source_type") != "pdf":
            return False
        if nfc(md.get("pdf_file", "")) != nfc(expected["ref"]):
            return False
        if "must_contain" in expected:
            return expected["must_contain"].lower() in hit["text"].lower()
        return True
    return False


def evaluate(k: int, label: str, query_set_path: Path) -> dict:
    with open(query_set_path, encoding="utf-8") as f:
        bundle = json.load(f)
    queries = bundle["queries"]

    retriever = Retriever()

    results = []
    for q in queries:
        hits = retriever.search(q["query"], k=k)
        is_adversarial = q.get("must_refuse", False)

        if is_adversarial:
            top_score = hits[0]["score"] if hits else 0.0
            results.append({
                "query": q,
                "hits": hits,
                "is_adversarial": True,
                "top_score": top_score,
                "any_expected_hit": None,
                "all_expected_hit": None,
                "expected_hits": [],
            })
            continue

        expected_hits = []
        for exp in q["expected_sources"]:
            hit_idx = next(
                (i for i, h in enumerate(hits) if hit_matches_expected(h, exp)),
                None,
            )
            expected_hits.append({"expected": exp, "hit_index": hit_idx})

        any_hit = any(eh["hit_index"] is not None for eh in expected_hits)
        all_hit = all(eh["hit_index"] is not None for eh in expected_hits)
        results.append({
            "query": q,
            "hits": hits,
            "is_adversarial": False,
            "any_expected_hit": any_hit,
            "all_expected_hit": all_hit,
            "expected_hits": expected_hits,
        })

    # ── aggregate ─────────────────────────────────────────────────────────
    non_adv = [r for r in results if not r["is_adversarial"]]
    adv = [r for r in results if r["is_adversarial"]]

    summary = {
        "total_queries": len(results),
        "non_adversarial": len(non_adv),
        "adversarial": len(adv),
        "recall_any@k": sum(r["any_expected_hit"] for r in non_adv) / len(non_adv) if non_adv else 0,
        "recall_all@k": sum(r["all_expected_hit"] for r in non_adv) / len(non_adv) if non_adv else 0,
    }

    # Per-category breakdown
    categories = {}
    for r in non_adv:
        cat = r["query"]["category"]
        categories.setdefault(cat, {"count": 0, "any_hit": 0, "all_hit": 0})
        categories[cat]["count"] += 1
        categories[cat]["any_hit"] += int(r["any_expected_hit"])
        categories[cat]["all_hit"] += int(r["all_expected_hit"])

    # Per-language breakdown
    languages = {}
    for r in non_adv:
        lang = r["query"]["language"]
        languages.setdefault(lang, {"count": 0, "any_hit": 0, "all_hit": 0})
        languages[lang]["count"] += 1
        languages[lang]["any_hit"] += int(r["any_expected_hit"])
        languages[lang]["all_hit"] += int(r["all_expected_hit"])

    return {
        "label": label,
        "k": k,
        "summary": summary,
        "categories": categories,
        "languages": languages,
        "results": results,
    }


def render_report(report: dict) -> str:
    out = []
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    out.append(f"# Retrieval Eval Report — {report['label']}\n")
    out.append(f"_{timestamp}, k={report['k']}_\n\n")

    s = report["summary"]
    out.append("## Headline\n")
    out.append(f"- **Recall (any expected source hit) @{report['k']}**: "
               f"**{s['recall_any@k']:.1%}** ({sum(r['any_expected_hit'] for r in report['results'] if not r['is_adversarial'])}/{s['non_adversarial']})\n")
    out.append(f"- **Recall (all expected sources hit) @{report['k']}**: "
               f"**{s['recall_all@k']:.1%}** ({sum(r['all_expected_hit'] for r in report['results'] if not r['is_adversarial'])}/{s['non_adversarial']})\n")
    out.append(f"- Adversarial queries: {s['adversarial']} (refusal not yet evaluated — needs LLM step)\n\n")

    out.append("## By category\n\n")
    out.append("| Category | Count | Recall (any) | Recall (all) |\n")
    out.append("|---|---|---|---|\n")
    for cat, c in sorted(report["categories"].items()):
        ra = c["any_hit"] / c["count"]
        rl = c["all_hit"] / c["count"]
        out.append(f"| {cat} | {c['count']} | {ra:.0%} ({c['any_hit']}/{c['count']}) | {rl:.0%} ({c['all_hit']}/{c['count']}) |\n")
    out.append("\n")

    out.append("## By language\n\n")
    out.append("| Language | Count | Recall (any) | Recall (all) |\n")
    out.append("|---|---|---|---|\n")
    for lang, c in sorted(report["languages"].items()):
        ra = c["any_hit"] / c["count"]
        rl = c["all_hit"] / c["count"]
        out.append(f"| {lang} | {c['count']} | {ra:.0%} ({c['any_hit']}/{c['count']}) | {rl:.0%} ({c['all_hit']}/{c['count']}) |\n")
    out.append("\n")

    out.append("## Failures (any expected source MISSED)\n\n")
    failures = [r for r in report["results"]
                if not r["is_adversarial"] and not r["any_expected_hit"]]
    if not failures:
        out.append("_None — every non-adversarial query had at least one expected source in top-k._\n\n")
    else:
        for r in failures:
            q = r["query"]
            out.append(f"### {q['id']} — `{q['category']}`, lang=`{q['language']}`\n")
            out.append(f"**Query**: {q['query']}\n\n")
            out.append(f"**Expected sources**:\n")
            for exp in q["expected_sources"]:
                if exp["kind"] == "message":
                    out.append(f"  - chat msg {exp['ref']}\n")
                else:
                    mc = f' (must contain "{exp["must_contain"]}")' if "must_contain" in exp else ""
                    out.append(f"  - PDF `{exp['ref']}`{mc}\n")
            out.append(f"\n**Top {report['k']} retrieved**:\n")
            for h in r["hits"]:
                md = h["metadata"]
                if md["source_type"] == "chat":
                    loc = f"chat msg {md['message_id']} | {md.get('topic') or '—'}"
                else:
                    loc = f"PDF {md['pdf_file']} | chunk {md['chunk_index']}"
                preview = h["text"][:120].replace("\n", " ") + ("…" if len(h["text"]) > 120 else "")
                out.append(f"  - [{h['score']:.3f}] `{h['id']}` ({loc}) — {preview}\n")
            out.append("\n")

    out.append("## Adversarial queries (top-1 retrieval scores)\n\n")
    out.append("_Low scores here are good — they mean the retriever isn't confident._\n\n")
    out.append("| ID | Query | Top-1 score | Top-1 source |\n")
    out.append("|---|---|---|---|\n")
    for r in report["results"]:
        if not r["is_adversarial"]:
            continue
        q = r["query"]
        score = r["top_score"]
        if r["hits"]:
            md = r["hits"][0]["metadata"]
            top_src = f"`{r['hits'][0]['id']}` ({md.get('topic') or md.get('pdf_file', '—')})"
        else:
            top_src = "—"
        out.append(f"| {q['id']} | {q['query'][:60]}… | {score:.3f} | {top_src} |\n")
    out.append("\n")

    out.append("## Per-query detail\n\n")
    out.append("| ID | Cat | Lang | Recall (any) | Recall (all) | Expected → top-k positions |\n")
    out.append("|---|---|---|---|---|---|\n")
    for r in report["results"]:
        q = r["query"]
        if r["is_adversarial"]:
            out.append(f"| {q['id']} | {q['category']} | {q['language']} | (refusal) | (refusal) | top-1: {r['top_score']:.3f} |\n")
        else:
            any_mark = "✅" if r["any_expected_hit"] else "❌"
            all_mark = "✅" if r["all_expected_hit"] else "⚠️"
            positions = []
            for eh in r["expected_hits"]:
                ref = eh["expected"]["ref"]
                if eh["hit_index"] is None:
                    positions.append(f"{ref}: ❌")
                else:
                    positions.append(f"{ref}: #{eh['hit_index'] + 1}")
            out.append(f"| {q['id']} | {q['category']} | {q['language']} | {any_mark} | {all_mark} | {' / '.join(positions)} |\n")

    return "".join(out)


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument("--k", type=int, default=5, help="Top-k to retrieve (default 5)")
    parser.add_argument("--label", default="phase1", help="Run label for the report filename")
    parser.add_argument("--set", dest="query_set", default=str(DEFAULT_SET),
                        help="Path to query set JSON (default: eval/golden_set.json)")
    args = parser.parse_args()

    REPORTS.mkdir(parents=True, exist_ok=True)
    query_set_path = Path(args.query_set)

    print(f"Running eval (k={args.k}, label={args.label}, set={query_set_path.name})…")
    report = evaluate(k=args.k, label=args.label, query_set_path=query_set_path)
    md = render_report(report)

    out_path = REPORTS / f"{datetime.now().strftime('%Y-%m-%d_%H%M')}_{args.label}.md"
    out_path.write_text(md, encoding="utf-8")

    s = report["summary"]
    print(f"\nRecall (any) @{args.k}: {s['recall_any@k']:.1%}")
    print(f"Recall (all) @{args.k}: {s['recall_all@k']:.1%}")
    print(f"Report: {out_path.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
