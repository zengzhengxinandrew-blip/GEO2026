"""派生指标层：把原始采样数据算成设计稿里的那套统一口径。

统一命名（全站唯一叫法，UI 与报告都用这套）：
  问题       真实用户会向 AI 提的一句问题，是所有采样的单位
  阵地       一个能被 AI 引用的站点
  提及率     未点名品牌、且有品牌/供应商选择机会的问题中，AI 主动提到品牌的样本比例
  引用份额   AI 列出的来源域名里属于品牌自有域名的比例
  GEO 健康分 五项加权：提及率30 引用份额25 阵地覆盖20 内容承接15 事实一致性10
  任务       一条可分派、可验收的动作（原「工单」）
  内容       为某个问题写的可被抽取的答案段落（原「资产/成稿」）

诚实纪律：算不出来的指标显示「未测」并把权重重新归一，绝不编数。
事实一致性需要人工比对样本后记录在 factcheck.json，没有记录就是未测。
"""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import urlparse

import geolib as G
import sample as S

WEIGHTS = {"mention": 30, "cite": 25, "channel": 20, "content": 15, "fact": 10}


def _own_host(cfg) -> str:
    return urlparse(cfg["brand"]["site"]).netloc.lower().removeprefix("www.")


def _is_own(domain: str, own: str) -> bool:
    d = (domain or "").lower().removeprefix("www.")
    return d == own or d.endswith("." + own)


def _sample_files(pdir: Path):
    d = pdir / "samples"
    return sorted(d.glob("2*.jsonl")) if d.exists() else []


def _rows(path: Path):
    return [r for r in S.read_sample_rows(path) if r.get("ok")]


def comparison_key(r):
    """比较必须是同一问题、引擎、模型、轮次、模式及联网条件。"""
    return (r.get("platform"), r.get("question_id"), r.get("question"),
            str(r.get("round", 1)), r.get("sample_mode"), r.get("market"),
            r.get("raw_model"), r.get("sampling_protocol"), r.get("search_enabled"),
            r.get("session_mode"), r.get("evaluation_config"))


def comparable(before, after):
    if not before or not after:
        return False
    if not all(r.get("raw_model") or r.get("sample_mode") in ("manual", "extension")
               for r in before + after):
        return False
    return {comparison_key(r) for r in before} == {comparison_key(r) for r in after}


def sample_quality(slug: str, rows) -> dict:
    cfg = G.load_config(slug)
    valid = [r for r in rows if r.get("ok")]
    up = _visibility(valid, cfg)
    failed = [r for r in rows if not r.get("ok")]
    expected = [p for p in cfg.get("platforms", []) if p in S.PROVIDERS and S.questions_for(cfg, p)]
    actual = {r.get("platform") for r in valid}
    missing = [p for p in expected if p not in actual]
    incomplete = []
    for p in expected:
        wanted = {q.get("id") for q in S.questions_for(cfg, p)}
        got = {r.get("question_id") for r in valid if r.get("platform") == p}
        if wanted - got:
            incomplete.append(p)
    reasons = []
    if failed:
        reasons.append(f"{len(failed)} 条采样失败，已排除")
    if missing:
        reasons.append("未取得有效答案的引擎：" + "、".join(S.label_of(p) for p in missing))
    if incomplete:
        reasons.append("部分引擎尚未完成当前问题库")
    if len(up) < 30:
        reasons.append(f"仅 {len(up)} 条有效选型样本，少量答案变化就会明显影响比例")
    return {"attempted": len(rows), "successful": len(valid), "failed": len(failed),
            "unprompted": len(_unprompted(valid)), "visibility": len(up),
            "content": len(_unprompted(valid)) - len(up), "missing_platforms": missing,
            "provisional": bool(failed or missing or incomplete or len(up) < 30),
            "warnings": reasons}


def _unprompted(rows):
    return [r for r in rows if not r.get("brand_in_question")]


def _visibility(rows, cfg):
    return [r for r in rows if S.visibility_sample(r, cfg)]


def _cite_share(rows, own) -> tuple[float | None, int, int]:
    """own 域名条数 / 全部引用域名条数。没有任何引用时返回 None。"""
    total = mine = 0
    for r in rows:
        for d in (r.get("analysis") or {}).get("cited_domains") or []:
            total += 1
            if _is_own(d, own):
                mine += 1
    return (mine / total if total else None), mine, total


def _mention(rows) -> float | None:
    if not rows:
        return None
    return sum(1 for r in rows if r["analysis"]["brand_mentioned"]) / len(rows)


def _median(vals):
    vals = sorted(vals)
    return vals[len(vals) // 2] if vals else None


# ---------------------------------------------------------------- 健康分

def health(slug: str, bp: dict | None, factcheck: list, rows_latest) -> dict:
    cfg = G.load_config(slug)
    own = _own_host(cfg)
    up = _unprompted(rows_latest)
    vis = _visibility(rows_latest, cfg)
    # 无自有网站时「引用官网率」无从谈起——记 None（不适用），
    # 由权重重整摊到其余维度，绝不退化成 0 假装"一次都没被引用"
    subs: dict[str, float | None] = {
        "mention": _mention(vis),
        "cite": _cite_share(up, own)[0] if G.has_site(cfg) else None,
        "channel": None, "content": None, "fact": None,
    }
    if bp:
        cov = bp["coverage"]
        subs["channel"] = cov["channel_covered"] / cov["channel_total"] if cov["channel_total"] else None
        subs["content"] = cov["content_done"] / cov["content_total"] if cov["content_total"] else None
    # 事实一致性：只有人工比对记录了才可测
    checked = [f for f in factcheck if f.get("state") in ("一致", "被说错", "缺失")]
    if checked:
        subs["fact"] = sum(1 for f in checked if f["state"] == "一致") / len(checked)

    wsum = sum(WEIGHTS[k] for k, v in subs.items() if v is not None)
    score = (sum(WEIGHTS[k] * v for k, v in subs.items() if v is not None) / wsum * 100) if wsum else None
    return {"score": round(score, 1) if score is not None else None,
            "subs": {k: (round(v, 3) if v is not None else None) for k, v in subs.items()},
            "weights": WEIGHTS,
            "measured": [k for k, v in subs.items() if v is not None]}


# ---------------------------------------------------------------- 各引擎

def _verdict(m, own_cited, peers) -> str:
    """peers：本期同市场其他平台的提及率。「最好」只在可比较且严格领先时说。"""
    if m is None:
        return "本期无选型样本"
    if m == 0:
        return "完全不可见——先看该引擎偏好的阵地缺什么"
    vals = [m] + [p for p in peers if p is not None]
    if len(vals) < 2 or len(set(vals)) < 2:
        rel = "样本不足，不下结论"
    elif m >= max(vals):
        rel = "表现最好的引擎，值得优先加固"
    else:
        rel = "有存在感但不稳定" if m >= 0.05 else "偶发提及，尚未进入候选集"
    parts = [rel]
    if not own_cited:
        parts.append("从未引用你的域名")
    return "；".join(parts)


def _brand_dist(rows) -> list[dict]:
    """回答里出现的实体（你 + 已配置竞品，别名并入）的提及分布。

    分母 = 样本数，分子 = 出现该实体的样本数——同一样本提多次只算一次。
    口径：candidates 只含配置过的实体，未配置的品牌不计入；竞品清单越全，分布越真实。"""
    n = len(rows)
    if not n:
        return []
    cnt: dict[str, int] = {}
    for r in rows:
        for name in r["analysis"].get("candidates") or []:
            cnt[name] = cnt.get(name, 0) + 1
    return [{"name": k, "hits": v, "rate": round(v / n, 3)}
            for k, v in sorted(cnt.items(), key=lambda x: -x[1])]


def engines(slug: str, rows_latest, metrics: dict | None) -> list[dict]:
    cfg = G.load_config(slug)
    own = _own_host(cfg)
    by: dict[str, list] = {}
    for r in rows_latest:
        by.setdefault(r["platform"], []).append(r)
    mkt = {p: rs[0].get("market", "cn") for p, rs in by.items()}
    ment = {p: _mention(_visibility(rs, cfg)) for p, rs in by.items()}
    out = []
    for plat, rs in by.items():
        up = _unprompted(rs)
        vis = _visibility(rs, cfg)
        m = ment[plat]
        ranks = [r["analysis"]["brand_rank"] for r in vis
                 if r["analysis"]["brand_mentioned"] and r["analysis"]["brand_rank"]]
        share, mine, total = _cite_share(up, own)
        meta = (metrics or {}).get("platforms", {}).get(plat, {})
        # 样本回放：优先取「无提示且被提及」的一条真实样本
        ex = next((r for r in vis if r["analysis"]["brand_mentioned"]), vis[0] if vis else (up[0] if up else (rs[0] if rs else None)))
        example = None
        if ex:
            ans = ex.get("answer", "")
            # 提及判定是大小写不敏感 + 含别名的，摘录定位也得是，否则
            # 答案里写 "geolook" 时摘录会错切到开头、brand_pos 变 -1
            names = [cfg["brand"]["name"]] + list(cfg["brand"].get("aliases", []) or [])
            hits = [ans.lower().find(n.lower()) for n in names if n]
            i = min((h for h in hits if h >= 0), default=-1)
            lo = max(0, (i if i >= 0 else 0) - 120)
            example = {"question": ex.get("question", ""), "date": ex.get("date", ""),
                       "excerpt": ans[lo:lo + 320], "brand_pos": (i - lo) if i >= 0 else -1,
                       "mentioned": ex["analysis"]["brand_mentioned"],
                       "rank": ex["analysis"]["brand_rank"],
                       "n_cites": len(ex["analysis"].get("cited_domains") or []),
                       "own_cited": ex["analysis"].get("own_domain_cited", False),
                       "negative_cues": ex["analysis"].get("negative_cues") or []}
        lat = sorted(r["elapsed_ms"] for r in rs if r.get("elapsed_ms"))
        out.append({
            "platform": plat, "label": meta.get("label", plat),
            "market": rs[0].get("market", "cn"),
            "searched": any(r.get("search_enabled") for r in rs),
            "samples": len(vis), "content_samples": len(up) - len(vis),
            "mention": round(m, 3) if m is not None else None,
            "pos_median": _median(ranks),
            "cite_share": round(share, 3) if share is not None else None,
            "cite_counts": [mine, total],
            "top_sources": list((meta.get("top_cited_domains") or {}).keys())[:4],
            "avg_ms": lat[len(lat) // 2] if lat else None,  # 中位耗时，抗超时长尾
            "neg_n": sum(1 for r in up if r["analysis"].get("negative_cues")),
            "brand_dist": _brand_dist(vis)[:8],
            "verdict": _verdict(m,
                                any(r["analysis"].get("own_domain_cited") for r in rs),
                                [ment[p] for p in by if p != plat and mkt[p] == mkt[plat]]),
            "example": example,
        })
    out.sort(key=lambda x: -(x["mention"] or 0))
    return out


# ---------------------------------------------------------------- 竞品

def competitors(slug: str, rows_latest) -> dict:
    cfg = G.load_config(slug)
    comps = cfg.get("competitors", [])
    up = _visibility(rows_latest, cfg)
    bym = {"cn": [r for r in up if r.get("market", "cn") == "cn"],
           "global": [r for r in up if r.get("market") == "global"]}

    def comp_markets(c) -> list[str]:
        m = c.get("market")
        return [m] if m in ("cn", "global") else ["cn", "global"]

    own_site = (cfg.get("brand", {}).get("site") or "").lower()
    own_host = own_site.split("//")[-1].split("/")[0].removeprefix("www.")

    # 国内/海外各一张表：竞品只在自己市场（market 缺失时两边都算）的样本上算，分母各用各的
    tables: dict[str, list] = {}
    source_gap: dict[str, list] = {}
    for market, rows in bym.items():
        n = len(rows)
        byp: dict[str, list] = {}
        for r in rows:
            byp.setdefault(r["platform"], []).append(r)
        # 你被提及的样本引用过哪些域名——竞品信源与它做差集，剩下的就是「它有你没有」
        mine_doms = {d for r in rows if r["analysis"].get("brand_mentioned")
                     for d in (r["analysis"].get("cited_domains") or [])}
        gap_count: dict[str, int] = {}
        t = []
        for c in comps:
            if market not in comp_markets(c):
                continue
            crows = [r for r in rows if c["name"] in (r["analysis"].get("competitors_mentioned") or [])]
            hit = len(crows)
            # 该竞品在各引擎的出现率——它最强的引擎就是最该去研究信源的地方
            tops = []
            for plat, rs in byp.items():
                h = sum(1 for r in rs if c["name"] in (r["analysis"].get("competitors_mentioned") or []))
                if h:
                    tops.append({"platform": plat,
                                 "label": rs[0].get("platform_name", plat),
                                 "rate": round(h / len(rs), 2)})
            tops.sort(key=lambda x: -x["rate"])
            # 信源构成：提到该竞品的答案都引用了谁（含你也被引用的，单独标出差集）
            doms: dict[str, int] = {}
            for r in crows:
                for d in (r["analysis"].get("cited_domains") or []):
                    if d and d != own_host:
                        doms[d] = doms.get(d, 0) + 1
            srcs = sorted(doms.items(), key=lambda x: -x[1])[:6]
            for d, cnt in doms.items():
                if d not in mine_doms:
                    gap_count[d] = gap_count.get(d, 0) + cnt
            t.append({"name": c["name"], "market": c.get("market", "both"),
                      "presence": round(hit / n, 3) if n else None, "hits": hit,
                      "top_engines": tops[:3],
                      "top_sources": [{"domain": d, "hits": h, "covered": d in mine_doms}
                                      for d, h in srcs]})
        t.sort(key=lambda x: -(x["presence"] or 0))
        tables[market] = t
        # 阵地差集：竞品语境里被引用 ≥2 次、且从未在你的语境里出现的域名
        source_gap[market] = [{"domain": d, "hits": h}
                              for d, h in sorted(gap_count.items(), key=lambda x: -x[1])
                              if h >= 2][:10]
    # 兼容旧前端：合并平铺（同名取最高 presence）
    flat: dict[str, dict] = {}
    for x in sorted((x for t in tables.values() for x in t),
                    key=lambda x: -(x["presence"] or 0)):
        flat.setdefault(x["name"], x)
    table = list(flat.values())

    # 按问题聚合：失守（你 0、竞品在）与独占（你在、竞品不在）
    byq: dict[str, list] = {}
    for r in up:
        byq.setdefault(r.get("question_id") or r.get("question", ""), []).append(r)
    lost, won = [], []
    for qid, rs in byq.items():
        me = _mention(rs) or 0
        rivals: dict[str, int] = {}
        for r in rs:
            for cname in r["analysis"].get("competitors_mentioned") or []:
                rivals[cname] = rivals.get(cname, 0) + 1
        top_rival = max(rivals.items(), key=lambda x: x[1]) if rivals else None
        row = {"qid": qid, "question": rs[0].get("question", ""),
               "market": rs[0].get("market", "cn"), "samples": len(rs),
               "mine": round(me, 2),
               "rival": top_rival[0] if top_rival else None,
               "rival_rate": round(top_rival[1] / len(rs), 2) if top_rival else 0}
        if me == 0 and top_rival:
            lost.append(row)
        elif me > 0 and not rivals:
            won.append(row)
    lost.sort(key=lambda x: -x["rival_rate"])
    won.sort(key=lambda x: -x["mine"])
    return {"tables": tables, "table": table, "lost": lost[:8], "won": won[:8],
            "source_gap": source_gap,
            "sample_n": len(up), "sample_ns": {m: len(rs) for m, rs in bym.items()}}


# ---------------------------------------------------------------- 问题层

def _diagnose(m, rank_med, rival, rival_rate, neg_n):
    """问题级诊断分型（参考 GEO 诊断引擎的四类问题，规则固定可复现）。

    优先级：疑似负面 > 竞品主导 > 完全缺席 > 排名靠后 > 表现正常。
    「疑似负面」只是线索词命中，定性要人工复核——见样本回放。
    """
    if m is None:
        return None
    if neg_n:
        return {"type": "疑似负面", "sev": "P0",
                "detail": f"{neg_n} 条样本在品牌附近出现负面语境，到样本回放人工复核"}
    if m == 0 and rival and rival_rate >= 0.5:
        return {"type": "竞品主导", "sev": "P0",
                "detail": f"你 0%，{rival} 出现率 {round(rival_rate * 100)}%——看它被谁引用"}
    if m == 0:
        return {"type": "完全缺席", "sev": "P1", "detail": "选型样本中从未被提及"}
    if rank_med and rank_med > 3:
        return {"type": "排名靠后", "sev": "P2",
                "detail": f"被提及但位次中位 {rank_med}——内容要争首推理由"}
    return {"type": "表现正常", "sev": "ok", "detail": ""}


# 意图分组的展示顺序与释义。价格/推荐/比较/替代 = 买家意图（离成交最近），
# 场景/风险 = 需求教育，品牌验证是点名探测题（单独口径，不进可见性）。
GROUP_META = [
    ("推荐", "买家", "「有什么推荐」——最直接的选型入口"),
    ("比较", "买家", "「A 和 B 哪个好」——竞品同框，输赢最明显"),
    ("替代", "买家", "「有没有 X 的替代」——对手用户在换供应商"),
    ("价格", "买家", "「多少钱/怎么收费」——离付款最近"),
    ("场景", "教育", "具体场景怎么用——承接长尾需求"),
    ("风险", "教育", "顾虑与边界——不答就被别人替你答"),
    ("品牌验证", "探测", "点名问你是谁——考的是认知准确度，不计入提及率"),
]


def question_groups(qs: list[dict]) -> list[dict]:
    """按意图分组聚合；mention_rate 是被提及题目占比，不是逐条回答提及率。
    内容题的 mention 为 None，点名探测题（brand_probe）也不参与计算。"""
    out = []
    known = {g for g, _, _ in GROUP_META}
    names = [g for g, _, _ in GROUP_META] + sorted({q.get("group") for q in qs
                                                    if q.get("group") and q.get("group") not in known})
    for name in names:
        rows = [q for q in qs if q.get("group") == name]
        if not rows:
            continue
        probe = [q for q in rows if q.get("brand_probe")]
        real = [q for q in rows if not q.get("brand_probe")]
        sampled = [q for q in real if q.get("mention") is not None]
        hit = [q for q in sampled if (q.get("mention") or 0) > 0]
        meta = next((m for m in GROUP_META if m[0] == name), (name, "其他", ""))
        out.append({
            "group": name, "kind": meta[1], "note": meta[2],
            "total": len(rows), "probe": len(probe),
            "selection_total": sum(1 for q in real if q.get("visibility")),
            "sampled": len(sampled),
            "mentioned_questions": len(hit),
            # 未采样时是 None（未测），不要退化成 0——那会读成「全军覆没」
            "mention_rate": round(len(hit) / len(sampled), 3) if sampled else None,
            "no_content": sum(1 for q in real if q.get("content") != "已成稿"),
            "lost": sum(1 for q in real if q.get("diagnosis") and
                        q["diagnosis"].get("type") in ("竞品主导", "完全缺席")),
        })
    return out


def questions(slug: str, rows_latest, bp: dict | None) -> list[dict]:
    cfg = G.load_config(slug)
    status = {c["id"]: c["status"] for c in (bp or {}).get("contents", [])}
    byq: dict[str, list] = {}
    for r in rows_latest:
        byq.setdefault(r.get("question_id"), []).append(r)
    brand = cfg.get("brand", {})
    names = [brand.get("name", "")] + list(brand.get("aliases") or [])

    def is_probe(q, rs) -> bool:
        if any(r.get("brand_in_question") for r in rs):
            return True
        text = (q.get("text") or "").lower()
        return q.get("scope") == "probe" or any(n and n.lower() in text for n in names)

    out = []
    for q in cfg.get("questions", []):
        rs = [r for r in byq.get(q.get("id"), []) if r.get("question") == q.get("text")]
        m = _mention(rs)
        probe = is_probe(q, rs)
        visibility = not probe and S.visibility_question(q.get("text", ""), cfg, q.get("id"))
        ranks = [r["analysis"]["brand_rank"] for r in rs
                 if r["analysis"]["brand_mentioned"] and r["analysis"].get("brand_rank")]
        rivals: dict[str, int] = {}
        for r in rs:
            for c in r["analysis"].get("competitors_mentioned") or []:
                rivals[c] = rivals.get(c, 0) + 1
        top = max(rivals.items(), key=lambda x: x[1]) if rivals else None
        neg_n = sum(1 for r in rs if r["analysis"].get("negative_cues"))
        out.append({"id": q.get("id"), "text": q.get("text", ""), "group": q.get("group", ""),
                    "market": q.get("market", "cn"),
                     "brand_probe": probe,
                     "visibility": visibility,
                     "mention": round(m, 2) if m is not None and (visibility or probe) else None,
                     "observed_mention": round(m, 2) if m is not None else None,
                    "samples": len(rs),
                     "diagnosis": None if not visibility else _diagnose(
                        m, _median(ranks),
                        top[0] if top else None,
                        (top[1] / len(rs)) if top and rs else 0, neg_n),
                    "content": status.get(q.get("id"), "缺口")})
    # 未提及 + 无内容的排最前——这就是选题池；probe 题单独归「品牌认知」，不参与缺口排序
    out.sort(key=lambda x: (x["brand_probe"], not x["visibility"],
                            (x["mention"] or 0), x["content"] == "已成稿"))
    return out


# ---------------------------------------------------------------- 趋势

def trend(slug: str) -> list[dict]:
    cfg = G.load_config(slug)
    own = _own_host(cfg)
    pdir = G.project_dir(slug)
    bp = G.read_json(pdir / "blueprint.json", None)
    fc = G.read_json(pdir / "factcheck.json", []) or []
    pts = []
    for f in _sample_files(pdir):
        rows = _rows(f)
        if not rows:
            continue
        h = health(slug, bp, fc, rows)   # 阵地/内容用当前值近似——历史蓝图未存档
        up = _unprompted(rows)
        mention = _mention(_visibility(rows, cfg))
        share = _cite_share(up, own)[0]
        pts.append({"date": f.stem, "health": h["score"],
                    "mention": round(mention, 3) if mention is not None else None,
                    "cite": round(share, 3) if share is not None else None,
                    "samples": len(rows)})
    return pts


def question_delta(slug: str) -> list[dict]:
    """最近两期采样里，每个问题的提及率变化——效果验收的依据。"""
    files = _sample_files(G.project_dir(slug))
    if len(files) < 2:
        return []
    def per_q(path):
        out = {}
        for r in _visibility(_rows(path), G.load_config(slug)):
            out.setdefault(r.get("question_id"), []).append(r)
        return {k: v for k, v in out.items() if k}
    before, after = per_q(files[-2]), per_q(files[-1])
    cfg = G.load_config(slug)
    qtext = {q["id"]: q["text"] for q in cfg.get("questions", [])}
    qmkt = {q["id"]: q.get("market", cfg.get("market", "cn")) for q in cfg.get("questions", [])}
    rows = []
    for qid in sorted(set(before) | set(after)):
        br, ar = before.get(qid, []), after.get(qid, [])
        matched = comparable(br, ar)
        b, a = _mention(br), _mention(ar)
        note = "本期未测" if a is None else ("" if matched else "采样条件不一致或历史模型信息缺失，不比较升降")
        if b is not None and a is not None and not matched:
            b = None
        rows.append({"qid": qid, "question": qtext.get(qid, qid),
                     "market": qmkt.get(qid, "cn"),
                     "before": round(b, 2) if b is not None else None,
                     "after": round(a, 2) if a is not None else None,
                     "note": note, "comparable": matched,
                     "dates": [files[-2].stem, files[-1].stem]})
    # 本期未测（after=None）不参与升降序，列在最后
    rows.sort(key=lambda x: (x["after"] is None, -(((x["after"] or 0) - (x["before"] or 0)))))
    return rows


# ---------------------------------------------------------------- 汇总入口

def build(slug: str) -> dict:
    pdir = G.project_dir(slug)
    bp = G.read_json(pdir / "blueprint.json", None)
    fc = G.read_json(pdir / "factcheck.json", []) or []
    files = _sample_files(pdir)
    rows_latest = _rows(files[-1]) if files else []
    # 直接从同一天、同一去重口径派生，避免使用旧 metrics 或另一日期的缓存。
    cfg = G.load_config(slug)
    metrics = {"platforms": S.aggregate(rows_latest, cfg)}
    quality = sample_quality(slug, S.read_sample_rows(files[-1]) if files else [])
    before = _visibility(_rows(files[-2]), cfg) if len(files) > 1 else []
    after = _visibility(rows_latest, cfg)
    can_compare = comparable(before, after) and not quality["provisional"]
    if len(files) > 1:
        can_compare = can_compare and not sample_quality(slug, S.read_sample_rows(files[-2]))["provisional"]
    comparison = {"comparable": can_compare,
                  "delta": round(_mention(after) - _mention(before), 4) if can_compare else None,
                  "reason": "同题、同引擎、同模型比较；短期变化仍可能来自回答随机性" if can_compare else
                            "两期采样条件不一致、信息缺失或样本不足，暂不判断升降"}

    qs = questions(slug, rows_latest, bp)
    return {
        "latest_date": files[-1].stem if files else None,
        "sample_quality": quality,
        "comparison": comparison,
        "health": health(slug, bp, fc, rows_latest),
        "engines": engines(slug, rows_latest, metrics),
        "question_groups": question_groups(qs),
        "brand_dist": {m: _brand_dist([r for r in _visibility(rows_latest, cfg)
                                       if r.get("market", "cn") == m])
                       for m in ("cn", "global")},
        "competitors": competitors(slug, rows_latest),
        "questions": qs,
        "trend": trend(slug),
        "factcheck": fc,
        "q_delta": question_delta(slug),
    }


# ---------------------------------------------------------------- 内容预检

BLOCK_LIFT = {"定义": "+57.3%", "数字事实": "+61.6%", "对比": "+55.3%", "操作步骤": "+41.2%", "FAQ": "利于召回"}


def precheck(text: str) -> dict:
    """内容工作台的可被引用度预检——与 audit.py 同一套判据。"""
    import audit as A

    body = re.sub(r"<!--.*?-->", "", text, flags=re.S)
    wc = G.word_count(body)
    h2 = len(re.findall(r"^##\s|^<h2", body, re.M))
    blocks = {
        "定义": bool(A.RE_DEFINITION.search(body)),
        "数字事实": len(A.RE_NUMBER.findall(body)) >= 3,
        "对比": bool(A.RE_COMPARE.search(body)) or bool(re.search(r"^\|.*\|$", body, re.M)),
        "操作步骤": bool(A.RE_HOWTO.search(body)),
        "FAQ": bool(A.RE_FAQ.search(body)),
    }
    hits = sum(blocks.values())
    grade = ("A" if wc >= 1000 and h2 >= 6 and hits >= 5 else
             "B" if wc >= 800 and hits >= 4 else
             "C" if wc >= 400 and hits >= 2 else "D")
    checks = [{"t": f"「{k}」块", "ok": v, "lift": BLOCK_LIFT[k]} for k, v in blocks.items()]
    checks.insert(0, {"t": f"正文 {wc} 词（门槛 1000）", "ok": wc >= 1000, "lift": ""})
    checks.insert(1, {"t": f"H2 小节 {h2} 个（目标 ≥6）", "ok": h2 >= 6, "lift": ""})
    return {"grade": grade, "wc": wc, "h2": h2, "blocks": blocks, "checks": checks}
