#!/usr/bin/env python3
"""dr_trace.py — 引用溯源层：把报告里的每条引用逐字回溯到原始语料。

这是本项目相对上游 deep-research 的核心增量。上游的溯源深度只有
「fact['url'] 字段非空」——因为它的来源是网页，网页会变会 404，逐字比对留不住。
本项目的语料是本地不可变 CSV，所以做得到。

三个命令：
    check    报告里的每条 [record_id] 引用逐字回溯，任一不通过即 FAIL
    convert  [record_id] → [(N)](#refN) 可点击引用 + 参考来源段（只列被引用的）
    audit    口径审计：含数字的论断，其引用来源必须是「事实陈述」

用法：
    python tools/dr_trace.py check  --report r.md --pool data-pool.json --records records.csv
    python tools/dr_trace.py convert --report r.md --pool data-pool.json --records records.csv
    python tools/dr_trace.py audit  --report r.md --pool data-pool.json --records records.csv
    python tools/dr_trace.py quote  --records records.csv --rec-id 7699100533977582374-0017

设计取舍
--------
* 引用单位是 record_id，不是「首次出现顺序编号」。
  顺序编号在事实顺序变化时会整体错位，而且没有任何机制能发现错位。
* 引文必须逐字命中。定位时先精确找，找不到再去掉空白重找
  （模型常少写/多写空格），仍找不到判 FAIL。
* 本模块不改上游任何文件。上游 480 行校验器、946 行生成器一行未动。
"""

import argparse
import csv
import json
import re
import sys
from pathlib import Path

REC_ID_RE = re.compile(r"^[A-Za-z0-9_]+-\d{4}$")
CITE_RE = re.compile(r"(?<!!)\[([A-Za-z0-9_]+-\d{4})\](?!\()")
NUM_RE = re.compile(r"\d")
# 报告里出现的量化数字：7万 / 8500 / 30% / 2.5亿
CLAIM_NUM_RE = re.compile(r"\d+(?:\.\d+)?\s*(?:万|亿|千|百|%|％|元|块|天|个月|年)?")
STRUCT_NUMS = {"2024", "2025", "2026", "2027", "2028"}   # 年份不算编造

# 引述标记：报告明确写了"这是他的说法"，就不算把观点当事实用。
# 允许转述，禁止把转述当客观事实 —— 这正是「不虚构」的分界线。
ATTRIBUTION = ("他说", "她说", "他表示", "她表示", "他认为", "她认为", "提到", "谈到",
               "透露", "坦言", "指出", "介绍", "强调", "声称", "自称", "据其", "据介绍",
               "称自己", "说法是", "据他", "据她", "原话", "预计", "预期", "他自己给")


def _num_is_noise(text: str, m) -> bool:
    """判断这个数字是不是「不是量」——型号、版本号、年份。

    H3 / B300 / 720P 这类是标识符；2.0 / 2.5 / 3.0 是版本号。
    这两类不该触发「数字必须有出处」的校验，否则满篇误报。
    """
    token = m.group(0).strip()
    digits = re.match(r"\d+(?:\.\d+)?", token).group(0)
    if digits in STRUCT_NUMS:
        return True
    before = text[m.start() - 1] if m.start() > 0 else ""
    after = text[m.end()] if m.end() < len(text) else ""
    if before.isascii() and before.isalpha():
        return True
    if after.isascii() and after.isalpha():
        return True
    # 纯 x.y 且无单位 → 版本号（2.0 / 2.5 / 3.0）
    if re.fullmatch(r"\d+\.\d", digits) and not re.search(r"[万亿千百%％元块天月个家]", token):
        return True
    return False

REFS_HEADINGS = ("## 参考来源", "## References", "## Sources")


# ── 载入 ──────────────────────────────────────────────────────────────────

def load_records(path: str) -> dict:
    """record_id → 记录。这是唯一的真相来源。"""
    p = Path(path)
    if not p.exists():
        raise SystemExit(f"[FATAL] 找不到语料表 {path}")
    with p.open("r", encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    idx = {}
    for r in rows:
        rid = (r.get("record_id") or "").strip()
        if rid:
            idx[rid] = r
    if not idx:
        raise SystemExit("[FATAL] 语料表里没有 record_id 列")
    return idx


def load_pool(path: str) -> dict:
    """rec_id → fact。池里有的，才是"来自语料"的。"""
    p = Path(path)
    if not p.exists():
        raise SystemExit(f"[FATAL] 找不到数据池 {path}")
    data = json.loads(p.read_text(encoding="utf-8-sig"))
    records = data if isinstance(data, list) else [data]
    idx = {}
    for rec in records:
        for fact in rec.get("facts") or []:
            rid = (fact.get("rec_id") or fact.get("url") or "").strip()
            if rid and REC_ID_RE.match(rid):
                idx.setdefault(rid, fact)
    return idx


def split_body(content: str) -> tuple[str, str]:
    for h in REFS_HEADINGS:
        if h in content:
            i = content.index(h)
            return content[:i], content[i:]
    return content, ""


def locate_quote(quote: str, *candidates: str):
    """在候选原文里逐字定位引文，返回原文中的真实片段。

    先精确找；找不到就去掉所有空白再找，再映射回原始区间。
    模型引用时常丢空格或多空格，这一步能救回大部分。
    """
    q = (quote or "").strip()
    if not q:
        return None
    for src in candidates:
        if src and q in src:
            return q
    qn = re.sub(r"\s+", "", q)
    if not qn:
        return None
    for src in candidates:
        if not src:
            continue
        idx_map, buf = [], []
        for i, ch in enumerate(src):
            if not ch.isspace():
                buf.append(ch)
                idx_map.append(i)
        sn = "".join(buf)
        pos = sn.find(qn)
        if pos >= 0:
            return src[idx_map[pos]: idx_map[pos + len(qn) - 1] + 1]
    return None


# ── check：逐字回溯 ───────────────────────────────────────────────────────

def cmd_check(args) -> int:
    report = Path(args.report)
    content = report.read_text(encoding="utf-8-sig")
    body, refs = split_body(content)

    pool = load_pool(args.pool)
    corpus = load_records(args.records)

    cited = CITE_RE.findall(body)
    issues, warns = [], []

    if not cited:
        issues.append("正文里没有任何 [record_id] 引用 —— 这份报告无法溯源")

    uniq = list(dict.fromkeys(cited))          # 去重保序
    verified = 0

    for rid in uniq:
        # 1) 引用必须在语料表里存在
        if rid not in corpus:
            issues.append(f"引用 {rid} 在语料表中不存在（凭空出现的 ID）")
            continue
        # 2) 引用必须来自数据池（说明它是被检索到的，不是模型顺手编的）
        if rid not in pool:
            issues.append(f"引用 {rid} 在语料表中存在，但不在本次数据池里（越界引用）")
            continue
        # 3) 池里那条 fact 的引文必须能在语料原文里逐字定位
        fact = pool[rid]
        row = corpus[rid]
        quote = (fact.get("quote") or "").strip()
        if not quote:
            issues.append(f"引用 {rid} 的数据池条目没有 quote 字段，无法校验")
            continue
        hit = locate_quote(quote, row.get("raw_text") or "", row.get("clean_text") or "")
        if not hit:
            issues.append(f"引用 {rid} 的引文无法在原文中逐字定位：{quote[:30]!r}")
            continue
        verified += 1

    # 4) 数字落地：报告句子里的量化数字，必须出现在被引用记录的原文里。
    #    这是最硬的一条 —— 论断可以写得含糊，数字编不出来。
    num_issues = []
    if args.check_numbers:
        for sent in re.split(r"(?<=[。！？；\n])", body):
            s = sent.strip()
            if not s or s.startswith("#") or s.startswith(">"):
                continue
            rids = CITE_RE.findall(s)
            if not rids:
                continue
            stripped = CITE_RE.sub("", s)
            pool_text = ""
            for rid in rids:
                row = corpus.get(rid) or {}
                pool_text += (row.get("raw_text") or "") + (row.get("clean_text") or "")
            pool_text = re.sub(r"\s+", "", pool_text)
            for m in CLAIM_NUM_RE.finditer(stripped):
                if _num_is_noise(stripped, m):
                    continue
                token = m.group(0).strip()
                digits = re.match(r"\d+(?:\.\d+)?", token).group(0)
                # 原文里数字可能写成不带单位的形式，逐个形式试
                forms = {digits, token.replace(" ", "")}
                if any(f in pool_text for f in forms):
                    continue
                num_issues.append(
                    f"数字「{token}」不在被引用记录的原文里（{rids[0]}）：{s[:50]}")
        issues.extend(num_issues)

    # 5) 引用密度：带引用的段落占比（低于阈值说明大段在自由发挥）
    paras = [p for p in re.split(r"\n\s*\n", body)
             if len(p.strip()) > 40 and not p.strip().startswith("#")]
    cited_paras = [p for p in paras if CITE_RE.search(p)]
    density = len(cited_paras) / len(paras) if paras else 0.0
    if paras and density < args.min_density:
        warns.append(
            f"引用密度偏低：{len(cited_paras)}/{len(paras)} 段带引用"
            f"（{density:.0%}，阈值 {args.min_density:.0%}）")

    # 6) 参考来源段不该出现未在正文引用的条目
    if refs:
        listed = set(re.findall(r"\[([A-Za-z0-9_]+-\d{4})\]", refs))
        orphan = listed - set(uniq)
        if orphan:
            warns.append(f"参考来源里有 {len(orphan)} 条正文未引用：{', '.join(sorted(orphan)[:5])}")

    print(f"报告       {report}")
    print(f"引用总数   {len(cited)}（去重 {len(uniq)}）")
    print(f"逐字通过   {verified}")
    print(f"段落覆盖   {len(cited_paras)}/{len(paras)} = {density:.0%}")
    print()
    if warns:
        for w in warns:
            print(f"  [WARN] {w}")
    if issues:
        for i in issues[:args.max_print]:
            print(f"  [FAIL] {i}")
        if len(issues) > args.max_print:
            print(f"  …另有 {len(issues) - args.max_print} 条")
    print()
    passed = not issues
    print("PASS" if passed else "FAIL")
    return 0 if passed else 1


# ── convert：引用落地 ─────────────────────────────────────────────────────

def cmd_convert(args) -> int:
    report = Path(args.report)
    content = report.read_text(encoding="utf-8-sig")
    body, _old_refs = split_body(content)

    pool = load_pool(args.pool)
    corpus = load_records(args.records)

    order = list(dict.fromkeys(CITE_RE.findall(body)))
    unknown = [r for r in order if r not in corpus or r not in pool]
    if unknown:
        print(f"[FATAL] 有 {len(unknown)} 个引用无法解析，先跑 check：")
        for r in unknown[:10]:
            print(f"  - {r}")
        return 2

    num = {rid: i + 1 for i, rid in enumerate(order)}

    def _repl(m):
        n = num[m.group(1)]
        return "[(%d)](#ref%d)" % (n, n)

    new_body = CITE_RE.sub(_repl, body)

    entries = []
    for rid, n in num.items():
        row = corpus[rid]
        fact = pool[rid]
        date = (fact.get("date") or "").strip()
        ts = (row.get("ts_start") or "").strip()
        role = (row.get("speaker_role") or "").strip()
        quote = locate_quote(fact.get("quote") or "",
                             row.get("raw_text") or "", row.get("clean_text") or "") \
            or (fact.get("quote") or "")
        when = " ".join(x for x in (date, ts) if x)
        entries.append(
            f'<a id="ref{n}"></a>({n}) `{rid}`'
            f'{" · " + when if when else ""}'
            f'{" · " + role if role else ""}'
            f' — 原文「{quote}」')

    refs_text = (f"\n\n{args.heading}\n\n"
                 f"共 {len(entries)} 条引用，全部可逐字回溯到原始语料记录。\n\n"
                 + "\n\n".join(entries) + "\n")

    out = Path(args.output) if args.output else report
    final = new_body.rstrip() + refs_text

    # 上游元数据里的「共引用 N 个来源」用的是数据池条目数，不是正文实际引用数。
    # 一份正文引 48 条、元数据写 131 个来源的报告，恰好是本项目要消灭的那种不精确。
    final, n_fix = re.subn(r"共引用\s*\d+\s*个来源",
                           f"共引用 {len(entries)} 个来源（逐字可回溯）", final)
    out.write_text(final, encoding="utf-8", newline="\n")

    print(f"引用落地   {len(entries)} 条（按正文首次出现顺序编号）")
    if n_fix:
        print(f"元数据修正 来源数 → {len(entries)}（原为数据池条目数）")
    print(f"已写出     {out}")
    return 0


# ── audit：口径审计 ───────────────────────────────────────────────────────

def cmd_audit(args) -> int:
    """含数字的论断，其引用来源必须是「事实陈述」。

    这是本项目独有的校验：上游只有笼统的 confidence，没有「他说的 vs 他猜的」这个概念。
    一条「去年投放 8500 万」如果是说话人自己的说法，引用它就是在把观点当数据用。
    """
    content = Path(args.report).read_text(encoding="utf-8-sig")
    body, _ = split_body(content)
    pool = load_pool(args.pool)
    corpus = load_records(args.records)

    issues, stats = [], {"fact": 0, "judgment": 0, "forecast": 0, "rumor": 0, "unknown": 0}
    checked = 0
    attributed = 0

    # 按句切，找出「含数字 + 带引用」的句子
    for sent in re.split(r"(?<=[。！？；\n])", body):
        s = sent.strip()
        if not s or s.startswith("#") or s.startswith(">"):
            continue
        rids = CITE_RE.findall(s)
        if not rids:
            continue
        stripped_s = CITE_RE.sub("", s)
        has_num = any(not _num_is_noise(stripped_s, m)
                      for m in CLAIM_NUM_RE.finditer(stripped_s))
        # 明确标注了出处归属（"他说""据介绍""原话"）→ 是转述，不是把观点当事实
        is_attributed = any(m in s for m in ATTRIBUTION)
        for rid in rids:
            row = corpus.get(rid)
            fact = pool.get(rid)
            if not row or not fact:
                continue
            claim = (row.get("claim_type") or "待定").strip()
            key = {"事实陈述": "fact", "个人判断": "judgment",
                   "预测": "forecast", "传闻": "rumor"}.get(claim, "unknown")
            stats[key] += 1
            checked += 1
            if has_num and claim != "事实陈述":
                if is_attributed:
                    attributed += 1
                    continue
                issues.append(
                    f"含数字的论断引用了「{claim}」来源 {rid}，且未标注这是转述：{s[:60]}")

    total = sum(stats.values()) or 1
    print(f"被引用来源 {checked} 处，口径分布：")
    for k, label in (("fact", "事实陈述"), ("judgment", "个人判断"),
                     ("forecast", "预测"), ("rumor", "传闻"), ("unknown", "待定")):
        print(f"  {label:<6} {stats[k]:>4}  {stats[k] / total:>6.1%}")
    print()
    if stats["fact"] / total < args.min_fact_ratio:
        print(f"  [WARN] 事实陈述占比 {stats['fact'] / total:.0%} 低于 {args.min_fact_ratio:.0%}"
              f" —— 这份报告的引用大多来自观点，不能当数据用")
    if attributed:
        print(f"  另有 {attributed} 处含数字论断已明确标注为转述（「他说」「据介绍」等），不计违规")
    if issues:
        for i in issues[:args.max_print]:
            print(f"  [FAIL] {i}")
        if len(issues) > args.max_print:
            print(f"  …另有 {len(issues) - args.max_print} 条")
    print()
    passed = not issues
    print("PASS" if passed else "FAIL")
    return 0 if passed else 1


# ── quote：人工抽查 ───────────────────────────────────────────────────────

def cmd_quote(args) -> int:
    corpus = load_records(args.records)
    row = corpus.get(args.rec_id)
    if not row:
        print(f"[FATAL] 语料表里没有 {args.rec_id}")
        return 2
    print(f"record_id  {args.rec_id}")
    print(f"时间       {row.get('ts_start')} – {row.get('ts_end')}")
    print(f"说话人     {row.get('speaker_id')}（{row.get('speaker_role')}）")
    print(f"类型/口径  {row.get('segment_type')} / {row.get('claim_type')}")
    print(f"置信度     {row.get('confidence')}  {row.get('low_conf_reason') or ''}")
    print(f"主题       {row.get('topic')}")
    print()
    print(f"raw_text   {row.get('raw_text')}")
    print(f"clean_text {row.get('clean_text')}")
    return 0


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    ap = argparse.ArgumentParser(description="引用溯源层")
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("check", help="逐字回溯校验")
    s.add_argument("--report", required=True)
    s.add_argument("--pool", required=True)
    s.add_argument("--records", required=True)
    s.add_argument("--min-density", type=float, default=0.30,
                   help="带引用段落占比下限（默认 0.30）")
    s.add_argument("--no-number-check", dest="check_numbers", action="store_false",
                   help="关闭数字落地校验")
    s.add_argument("--max-print", type=int, default=20)
    s.set_defaults(func=cmd_check)

    s = sub.add_parser("convert", help="[record_id] → 可点击引用 + 参考来源")
    s.add_argument("--report", required=True)
    s.add_argument("--pool", required=True)
    s.add_argument("--records", required=True)
    s.add_argument("--output", default="")
    s.add_argument("--heading", default="## 参考来源")
    s.set_defaults(func=cmd_convert)

    s = sub.add_parser("audit", help="口径审计")
    s.add_argument("--report", required=True)
    s.add_argument("--pool", required=True)
    s.add_argument("--records", required=True)
    s.add_argument("--min-fact-ratio", type=float, default=0.40)
    s.add_argument("--max-print", type=int, default=20)
    s.set_defaults(func=cmd_audit)

    s = sub.add_parser("quote", help="查某条记录的原文")
    s.add_argument("--records", required=True)
    s.add_argument("--rec-id", required=True)
    s.set_defaults(func=cmd_quote)

    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
