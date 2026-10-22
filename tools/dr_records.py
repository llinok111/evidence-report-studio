#!/usr/bin/env python3
"""dr_records.py — records 数据源：把结构化语料表转成上游数据池格式。

上游 deep-research 的数据池只认「网页抓取 + URL」。本模块补上一条离线路径：
records.csv（由 douyin-living/建库.py 产出）→ data-pool.json，URL 位置换成 record_id。

用法：
    # 看语料概况
    python tools/dr_records.py stats --records records.csv

    # 给大纲 agent 读的抽样（不写数据池）
    python tools/dr_records.py sample --records records.csv --n 240 --out sample.md

    # 按大纲建池（推荐：主题由大纲定义，池跟着大纲走）
    python tools/dr_records.py build --records records.csv --outline outline.json --out data-pool.json

    # 没有大纲时按 segment_type 分桶（应急路径）
    python tools/dr_records.py build --records records.csv --out data-pool.json --group-by segment

设计要点
--------
1. **主题由大纲驱动**，不自己瞎聚类。语料的 topic 字段是「每句一个标签」，
   1526 个 topic / 1552 条记录，无法直接聚合。所以先出大纲，再按大纲给记录分簇。
2. **url 字段填 record_id**。这是与上游最大的结构差异：
   上游按 (机构, 年份, URL) 三元组首次出现顺序编号，顺序一变引用全错且无法发现；
   record_id 是稳定 ID，报告里直接写，永远不会错位。
3. **每条 fact 带 quote（逐字引文）**，供 check_citations 做逐字回溯。
4. **不联网、不外扩**。语料里没有的东西，池里就不会有。
"""

import argparse
import csv
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

# claim_type → 上游 data_type（actual / estimate / forecast）
CLAIM_TO_DATA_TYPE = {
    "事实陈述": "actual",
    "个人判断": "estimate",
    "预测": "forecast",
    "传闻": "estimate",
    "待定": "estimate",
}

CONF_MAP = {"high": "high", "medium": "medium", "low": "low"}

# 无实义内容，建池时丢弃（不参与任何主题）
NOISE_SEGMENTS = {"闲聊"}
NOISE_TOPICS = {"附和应答", "附和", "附和回应", "插话", "过渡语", "无有效内容",
                "无实质内容", "语句不完整", "表示明白", "回应明白"}


# ── 读取 ──────────────────────────────────────────────────────────────────

def load_records(path: str) -> list[dict]:
    p = Path(path)
    if not p.exists():
        raise SystemExit(f"[FATAL] 找不到 {path}")
    with p.open("r", encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    if not rows:
        raise SystemExit("[FATAL] records.csv 是空的")
    return rows


def _text_of(r: dict) -> str:
    return (r.get("clean_text") or r.get("raw_text") or "").strip()


def _is_noise(r: dict) -> bool:
    if (r.get("segment_type") or "").strip() in NOISE_SEGMENTS:
        return True
    if (r.get("topic") or "").strip() in NOISE_TOPICS:
        return True
    return len(_text_of(r)) < 6


# ── 文本相似度（中文友好）────────────────────────────────────────────────

def _bigrams(s: str) -> set:
    s = re.sub(r"[\s，。、！？,.!?~～…（）()【】\[\]\"'`:：;；]+", "", s)
    if len(s) < 2:
        return {s} if s else set()
    return {s[i:i + 2] for i in range(len(s) - 1)}


def similarity(a: str, b: str) -> float:
    """字符 bigram 的 Jaccard 重叠。

    不用 split() 做关键词匹配 —— 中文没有空格，split() 返回整句，
    子串包含判断几乎永远为假。上游 prepare_chapter 就栽在这里。
    """
    A, B = _bigrams(a), _bigrams(b)
    if not A or not B:
        return 0.0
    return len(A & B) / len(A | B)


def assign_theme(record: dict, themes: list[dict]) -> tuple[int, float]:
    """把一条记录分到最匹配的主题。返回 (主题下标, 得分)。"""
    hay = " ".join(filter(None, [
        (record.get("topic") or ""),
        (record.get("clean_text") or ""),
    ]))
    best_i, best_s = -1, 0.0
    for i, th in enumerate(themes):
        probes = [th["question"]] + list(th.get("keywords") or [])
        s = max((similarity(hay, p) for p in probes if p), default=0.0)
        # 关键词命中额外加权：大纲给的关键词比整句相似度更可靠
        for kw in th.get("keywords") or []:
            if kw and kw in hay:
                s = max(s, 0.5 + 0.1 * min(len(kw), 5))
        if s > best_s:
            best_i, best_s = i, s
    return best_i, best_s


# ── 建池 ──────────────────────────────────────────────────────────────────

def _date_of(r: dict) -> str:
    """从 source_file 名里取日期：实时_20261021_2259.txt → 2026-10-21。取不到就留空。"""
    m = re.search(r"(20\d{2})(\d{2})(\d{2})", r.get("source_file") or "")
    return f"{m.group(1)}-{m.group(2)}-{m.group(3)}" if m else ""


def _freshness(date: str) -> str:
    """上游 standard/deep 模式要求 cur 字段：current / recent / dated。"""
    if not date:
        return "dated"
    try:
        from datetime import date as _d, datetime as _dt
        d = _dt.strptime(date, "%Y-%m-%d").date()
    except Exception:
        return "dated"
    from datetime import date as _d
    days = (_d.today() - d).days
    if days <= 90:
        return "current"
    if days <= 365:
        return "recent"
    return "dated"


def record_to_fact(r: dict) -> dict:
    quote = _text_of(r)[:40]
    claim = (r.get("claim_type") or "待定").strip()
    date = _date_of(r)
    return {
        # 上游基础字段（yr 必须非空，否则下游 convert_citations 会跳过这条）
        "src": (r.get("speaker_role") or "未知").strip(),
        "yr": date[:4] if date else "未知年份",
        "met": (r.get("segment_type") or "待定").strip(),
        "val": None,
        "u": "",
        "ctx": _text_of(r)[:150],
        "url": (r.get("record_id") or "").strip(),      # ← 引用单位：record_id
        "title": (r.get("topic") or "").strip(),
        "cur": _freshness(date),
        "conf": CONF_MAP.get((r.get("confidence") or "").strip(), "medium"),
        "data_type": CLAIM_TO_DATA_TYPE.get(claim, "estimate"),
        # 本项目新增字段
        "rec_id": (r.get("record_id") or "").strip(),
        "quote": quote,
        "ts": (r.get("ts_start") or "").strip(),
        "date": date,
        "speaker": (r.get("speaker_id") or "").strip(),
        "claim_type": claim,
        "has_number": (r.get("has_number") or "no").strip(),
    }


def build(args) -> int:
    rows = load_records(args.records)
    total_in = len(rows)

    kept = [r for r in rows if not _is_noise(r)]
    dropped_noise = total_in - len(kept)

    # 只在「有数字」或「非闲聊」的记录里取事实：纯观点闲聊进池只会稀释信号
    if args.require_number:
        with_num = [r for r in kept if (r.get("has_number") or "").strip() == "yes"]
        if len(with_num) >= args.min_facts * 2:
            kept = with_num

    if not kept:
        raise SystemExit("[FATAL] 过滤后没有任何记录，检查 --require-number 或语料本身")

    pool: list[dict] = []
    unassigned: list[dict] = []

    if args.outline:
        outline = json.loads(Path(args.outline).read_text(encoding="utf-8-sig"))
        themes = []
        for ci, ch in enumerate(outline.get("chapters") or []):
            for sq in ch.get("sub_questions") or []:
                themes.append({
                    "chapter": ch.get("title", ""),
                    "question": sq.get("question", ""),
                    "keywords": list(sq.get("search_keywords") or []) + list(sq.get("counter_keywords") or []),
                    "priority": sq.get("priority", "medium"),
                })
        if not themes:
            raise SystemExit("[FATAL] outline.json 里没有 chapters[].sub_questions[]")

        buckets = defaultdict(list)
        scores = defaultdict(list)
        for r in kept:
            i, s = assign_theme(r, themes)
            if i < 0 or s < args.min_score:
                unassigned.append(r)
            else:
                buckets[i].append(r)
                scores[i].append(s)

        for i, th in enumerate(themes):
            facts = [record_to_fact(r) for r in buckets.get(i, [])]
            facts.sort(key=lambda f: (
                0 if f["has_number"] == "yes" else 1,
                0 if f["claim_type"] == "事实陈述" else 1,
                -len(f["ctx"]),
            ))
            facts = facts[:args.max_facts]
            pool.append({
                "question": th["question"],
                "chapter": th["chapter"],
                "priority": th["priority"],
                "src": sorted({f["rec_id"] for f in facts}),
                "facts": facts,
                "controversies": [],
                "gaps": [] if facts else ["本主题在语料中未匹配到记录"],
                "match_score_avg": round(sum(scores.get(i, [0])) / max(len(scores.get(i, [])), 1), 3),
            })
    else:
        by_seg = defaultdict(list)
        for r in kept:
            by_seg[(r.get("segment_type") or "待定").strip()].append(r)
        for seg, group in sorted(by_seg.items(), key=lambda kv: -len(kv[1])):
            facts = [record_to_fact(r) for r in group]
            facts.sort(key=lambda f: (
                0 if f["has_number"] == "yes" else 1,
                0 if f["claim_type"] == "事实陈述" else 1,
                -len(f["ctx"]),
            ))
            facts = facts[:args.max_facts]
            pool.append({
                "question": f"【{seg}】语料中属于该内容类型的全部记录",
                "chapter": seg,
                "priority": "high" if seg in ("数据报价", "行业分析", "流程拆解") else "medium",
                "src": sorted({f["rec_id"] for f in facts}),
                "facts": facts,
                "controversies": [],
                "gaps": [],
                "match_score_avg": 1.0,
            })

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8", newline="\n") as f:
        json.dump(pool, f, ensure_ascii=False, indent=2)

    fact_total = sum(len(p["facts"]) for p in pool)
    used_ids = {f["rec_id"] for p in pool for f in p["facts"]}

    print(f"输入记录      {total_in}")
    print(f"丢弃噪音      {dropped_noise}（闲聊 / 附和 / 过短）")
    if args.require_number:
        print(f"仅取含数字    {len(kept)}")
    print(f"主题数        {len(pool)}")
    print(f"入池事实      {fact_total}（去重记录 {len(used_ids)}）")
    if args.outline:
        print(f"未匹配主题    {len(unassigned)} 条（得分 < {args.min_score}）")
        weak = [p for p in pool if not p["facts"]]
        if weak:
            print(f"空主题        {len(weak)} 个：")
            for p in weak[:8]:
                print(f"  - {p['question']}")
    print(f"已写出        {out}")
    return 0


# ── 抽样（给大纲 agent 读）──────────────────────────────────────────────

def sample(args) -> int:
    rows = load_records(args.records)
    kept = [r for r in rows if not _is_noise(r)]

    # 分层抽样：每个 segment_type 按比例取，含数字的优先
    by_seg = defaultdict(list)
    for r in kept:
        by_seg[(r.get("segment_type") or "待定").strip()].append(r)
    for seg in by_seg:
        by_seg[seg].sort(key=lambda r: (
            0 if (r.get("has_number") or "") == "yes" else 1,
            0 if (r.get("claim_type") or "") == "事实陈述" else 1,
        ))

    picked, i = [], 0
    segs = sorted(by_seg, key=lambda s: -len(by_seg[s]))
    while len(picked) < args.n and any(by_seg[s] for s in segs):
        s = segs[i % len(segs)]
        if by_seg[s]:
            picked.append(by_seg[s].pop(0))
        i += 1
    picked.sort(key=lambda r: (r.get("ts_start") or ""))

    lines = [f"# 语料抽样（{len(picked)} / {len(rows)} 条）\n",
             "> 用途：给大纲 agent 判断这批语料里到底有什么主题。",
             "> 每条格式：`[record_id] 时间 说话人(角色) 内容类型/口径｜原文`\n"]
    for r in picked:
        lines.append(
            f"- `{r.get('record_id')}` {r.get('ts_start','')} "
            f"{r.get('speaker_id','')}({r.get('speaker_role','')}) "
            f"{r.get('segment_type','')}/{r.get('claim_type','')}"
            f"{'｜含数字' if (r.get('has_number') or '') == 'yes' else ''}"
            f"｜{_text_of(r)[:120]}")

    text = "\n".join(lines) + "\n"
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8", newline="\n")
        print(f"已写出 {args.out}（{len(picked)} 条）")
    else:
        print(text)
    return 0


# ── 概况 ──────────────────────────────────────────────────────────────────

def stats(args) -> int:
    rows = load_records(args.records)
    kept = [r for r in rows if not _is_noise(r)]
    print(f"总记录        {len(rows)}")
    print(f"有效记录      {len(kept)}（丢弃 {len(rows) - len(kept)} 条噪音）")
    print()
    for f in ("segment_type", "claim_type", "has_number", "confidence", "speaker_role"):
        c = Counter((r.get(f) or "").strip() for r in rows)
        c.pop("", None)
        print(f"{f:<14}", "，".join(f"{k} {v}" for k, v in c.most_common(6)))
    num = sum(1 for r in rows if (r.get("has_number") or "") == "yes")
    print()
    print(f"含数字段落    {num}（{num / len(rows):.1%}）")
    print(f"topic 去重数  {len(set(r.get('topic') or '' for r in rows))}"
          f" → 平均 {len(rows) / max(len(set(r.get('topic') or '' for r in rows)), 1):.1f} 条/topic")
    print("               （topic 是「每句一个标签」，不能直接当主题聚合，必须由大纲驱动分簇）")
    return 0


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    ap = argparse.ArgumentParser(description="records 数据源 → 上游数据池")
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("stats", help="语料概况")
    s.add_argument("--records", required=True)
    s.set_defaults(func=stats)

    s = sub.add_parser("sample", help="分层抽样，给大纲 agent 读")
    s.add_argument("--records", required=True)
    s.add_argument("--n", type=int, default=240)
    s.add_argument("--out", default="")
    s.set_defaults(func=sample)

    s = sub.add_parser("build", help="建数据池")
    s.add_argument("--records", required=True)
    s.add_argument("--outline", default="", help="outline.json（推荐）")
    s.add_argument("--out", default="data-pool.json")
    s.add_argument("--max-facts", type=int, default=40, help="每个主题最多取多少条事实")
    s.add_argument("--min-facts", type=int, default=2)
    s.add_argument("--min-score", type=float, default=0.12, help="主题匹配最低分")
    s.add_argument("--require-number", action="store_true", help="只取含数字的记录")
    s.set_defaults(func=build)

    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
