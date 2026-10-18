"""
digest_engine.py —— 每日总结"规则引擎"（站点与 GitHub Worker 共用定稿）
=====================================================================
职责：
1. 不依赖任何 AI / 第三方库，纯标准库实现"标题分词 → 停用词过滤 → TF 关键词
   → 关键词重叠聚类 → 每簇选代表 → 产出 8~15 条要点 + Top 关键词"；
2. 同源产出：news_id() 为 source+url 的稳定哈希，Worker 抓取与站点兜底生成
   使用完全相同的 id，保证总结里的"关联新闻 id"两侧一致；
3. build_digest() 输出可直接入库/存文件的完整 payload（JSON 可序列化）。

【同步约定】github_news_worker/digest_engine.py 是本文件的逐字副本（两侧部署
环境隔离，无法共享导入路径）。修改本文件后必须同步复制过去，保持行为一致。
"""

from __future__ import annotations

import hashlib
import re
import time
from typing import Any, Optional

# ============================================================
# 停用词表（中文虚词 + 新闻套话；命中即丢弃）
# ============================================================
STOPWORDS: set[str] = {
    # 虚词/介词/连词
    "的", "了", "在", "是", "和", "与", "及", "或", "而", "但", "并", "等",
    "被", "把", "对", "从", "到", "向", "为", "以", "于", "之", "则", "就",
    "也", "都", "还", "会", "将", "已", "很", "更", "最", "可以", "可能",
    "这个", "那个", "这些", "那些", "一个", "一种", "一些", "我们", "他们",
    "你们", "自己", "以及", "并且", "因为", "所以", "如果", "但是", "然而",
    # 新闻套话（标题里高频但无信息量）
    "记者", "报道", "消息", "表示", "指出", "认为", "介绍", "据悉", "据报",
    "近日", "目前", "日前", "昨日", "今天", "明天", "今年", "去年", "上午",
    "下午", "上午", "最新", "相关", "有关", "进行", "实现", "成为", "通过",
    "如何", "什么", "哪", "哪些", "怎么", "多少", "是否", "没有", "不是",
    "方面", "问题", "情况", "工作", "发展", "建设", "推进", "加强", "提升",
}
# 单字停用词：分词产生的二字词若由两个停用单字组成，同样丢弃
STOP_CHARS: set[str] = set("的了在和是与及或而但并被把对从到向为以于之则就也都会将已很更最着过吗呢吧啊哦嗯")

# 正则：连续中文 / 连续拉丁数字串
_CJK_RE = re.compile(r"[\u4e00-\u9fff]+")
_LATIN_RE = re.compile(r"[A-Za-z][A-Za-z0-9\-]{1,}")
# 摘要清洗：去标签、压缩空白、去首尾符号
_TAG_RE = re.compile(r"<[^>]+>")
_SPACE_RE = re.compile(r"[\s\u3000]+")
_TRIM_RE = re.compile(r"^[\s\-—·|:：，,。.、;；\"'“”‘’《》()（）\[\]【】]+")


# ============================================================
# 新闻稳定 id（两侧一致）
# ============================================================
def news_id(source: str, url: str) -> str:
    """
    source+url 的稳定短哈希（16 位十六进制）。
    Worker 写 news/YYYY-MM-DD.json 的 items[].id 与站点兜底生成时使用同一算法。
    """
    raw: str = f"{source}\x1f{url}".encode("utf-8")
    return hashlib.sha1(raw).hexdigest()[:16]


# ============================================================
# 分词（中文二字滑窗 + 拉丁词，零依赖）
# ============================================================
def tokenize(text: str) -> list[str]:
    """
    极简分词：连续中文串取相邻二字滑窗（长度 1 的串直接取该字）；
    拉丁/数字串取下整词。全部小写化后过滤停用词。
    返回去重前的 token 列表（词频统计需要重复计数）。
    """
    text = _TAG_RE.sub(" ", str(text or ""))
    out: list[str] = []
    for m in _LATIN_RE.finditer(text):
        w: str = m.group(0).lower()
        if len(w) >= 2 and w not in STOPWORDS:
            out.append(w)
    for m in _CJK_RE.finditer(text):
        seg: str = m.group(0)
        if len(seg) == 1:
            cands: list[str] = [seg]
        else:
            cands = [seg[i:i + 2] for i in range(len(seg) - 1)]
        for c in cands:
            if c in STOPWORDS:
                continue
            # 二字词若由两个停用单字组成（如"的和"），丢弃
            if len(c) == 2 and c[0] in STOP_CHARS and c[1] in STOP_CHARS:
                continue
            out.append(c)
    return out


def clean_text(text: str, limit: int = 120) -> str:
    """清洗摘要/正文：去 HTML 标签与多余符号、压缩空白、截断。"""
    s: str = _SPACE_RE.sub(" ", _TAG_RE.sub(" ", str(text or ""))).strip()
    s = _TRIM_RE.sub("", s)
    return s[:limit]


# ============================================================
# TF 关键词
# ============================================================
def compute_keywords(titles: list[str], top_n: int = 20) -> list[tuple[str, int]]:
    """
    对标题集合做词频统计（标题权重天然高于摘要），返回 [(词, 频次)] 降序。
    """
    freq: dict[str, int] = {}
    for t in titles:
        for tok in tokenize(t):
            freq[tok] = freq.get(tok, 0) + 1
    # 只保留出现 ≥2 次的词作为"主题词"；若样本太少则放宽
    words = [(w, c) for w, c in freq.items() if c >= 2]
    if len(words) < 5:
        words = list(freq.items())
    words.sort(key=lambda x: (-x[1], -len(x[0]), x[0]))
    return words[:top_n]


# ============================================================
# 关键词重叠聚类
# ============================================================
def cluster_items(items: list[dict[str, Any]],
                  keyword_set: set[str],
                  threshold: float = 0.34) -> list[dict[str, Any]]:
    """
    贪心聚类：按发布时间倒序遍历，与已有簇做关键词集合重叠度比较，
    重叠度 ≥ threshold 则并入，否则新开一簇。
    重叠度 = 交集大小 / min(两集合大小)，对短文更宽容。
    """
    clusters: list[dict[str, Any]] = []
    for it in items:
        toks: set[str] = set()
        for tok in tokenize(f"{it.get('title', '')} {clean_text(it.get('summary', ''), 80)}"):
            if tok in keyword_set:
                toks.add(tok)
        if not toks:
            toks = set(tokenize(it.get("title", "")))
        best: Optional[dict[str, Any]] = None
        best_score: float = 0.0
        for c in clusters:
            inter: int = len(toks & c["toks"])
            if inter == 0:
                continue
            score: float = inter / max(1, min(len(toks), len(c["toks"])))
            if score > best_score:
                best, best_score = c, score
        if best is not None and best_score >= threshold:
            best["items"].append(it)
            best["toks"] |= toks
        else:
            clusters.append({"toks": set(toks), "items": [it]})
    return clusters


# ============================================================
# 单条要点文案
# ============================================================
def item_sentence(item: dict[str, Any], limit: int = 72) -> str:
    """
    某条新闻 → 一句话要点：优先用清洗后的摘要首句，缺失时退回标题。
    """
    summary: str = clean_text(item.get("summary", ""), 300)
    for sep in ("。", "！", "？", ".", "!", "?", "；", ";"):
        if sep in summary:
            summary = summary.split(sep)[0]
            break
    text: str = summary if len(summary) >= 8 else ""
    if not text:
        text = clean_text(item.get("title", ""), limit)
    return text[:limit]


def _item_ref(item: dict[str, Any]) -> dict[str, Any]:
    """要点携带的新闻引用（供前端点击跳原文）。"""
    return {
        "id": str(item.get("id") or news_id(str(item.get("source", "")),
                                              str(item.get("url", "")))),
        "title": clean_text(item.get("title", ""), 100),
        "source": str(item.get("source", "")),
        "url": str(item.get("url", "")),
        "published_at": float(item.get("published_at") or 0.0),
    }


# ============================================================
# 主入口：生成完整 digest payload
# ============================================================
def build_digest(items: list[dict[str, Any]],
                 ntype: str,
                 ndate: str,
                 window_start: float,
                 window_end: float,
                 generator: str = "lazy",
                 keywords_top_n: int = 20,
                 min_points: int = 8,
                 max_points: int = 15,
                 ai_points: Optional[list[str]] = None) -> dict[str, Any]:
    """
    由新闻条目生成总结 payload。

    参数：
      items         新闻条目（含 title/source/url/summary/published_at，可选 id）
      ntype         today | yesterday
      ndate         所属北京日期 YYYY-MM-DD
      window_start/end  时间窗口（Unix 秒，UTC 数值）
      generator     action(GitHub 定时) | lazy(站点兜底) | manual(后台重生成)
      ai_points     可选：Worker 侧 AI 润色后的要点文案（有则替换规则文案，
                    站点侧恒为 None —— 站点禁止访问白名单外网络）
    返回：可直接 json.dumps 的 dict
    """
    # 按发布时间倒序，保证聚类与代表选取稳定可复现
    ordered: list[dict[str, Any]] = sorted(
        items, key=lambda x: -float(x.get("published_at") or 0.0))

    kw_pairs: list[tuple[str, int]] = compute_keywords(
        [str(it.get("title", "")) for it in ordered], top_n=keywords_top_n)
    keywords: list[str] = [w for w, _c in kw_pairs]
    keyword_set: set[str] = set(keywords)
    weight: dict[str, int] = {w: c for w, c in kw_pairs}

    clusters: list[dict[str, Any]] = cluster_items(ordered, keyword_set)

    # 簇评分：簇内命中关键词的权重和 × log(1+条数)，热度优先
    def cluster_score(c: dict[str, Any]) -> float:
        w: int = sum(weight.get(t, 1) for t in c["toks"] if t in keyword_set)
        import math
        return float(w) * math.log(1 + len(c["items"]))

    clusters.sort(key=cluster_score, reverse=True)

    points: list[dict[str, Any]] = []
    used_ids: set[str] = set()

    def push_point(rep: dict[str, Any], kw: str) -> None:
        ref = _item_ref(rep)
        if ref["id"] in used_ids or len(points) >= max_points:
            return
        used_ids.add(ref["id"])
        text: str = item_sentence(rep)
        if ai_points and len(ai_points) >= len(points) + 1:
            cand: str = clean_text(ai_points[len(points)], 90)
            if cand:
                text = cand
        points.append({
            "text": f"【{kw}】{text}" if kw else text,
            "keyword": kw,
            "news_ids": [ref["id"]],
            "refs": [ref],
        })

    for c in clusters:
        if len(points) >= max_points:
            break
        reps = sorted(c["items"],
                      key=lambda it: -sum(weight.get(t, 0) for t in
                                          set(tokenize(it.get("title", "")))))
        rep = reps[0]
        kws: list[str] = sorted([t for t in c["toks"] if t in keyword_set],
                                key=lambda t: -weight.get(t, 0))
        push_point(rep, kws[0] if kws else "")

    # 簇不足 min_points 时，用未使用的新闻补齐（保证 8~15 条的产出下限）
    if len(points) < min_points:
        for it in ordered:
            if len(points) >= min_points or len(points) >= max_points:
                break
            ref = _item_ref(it)
            if ref["id"] in used_ids:
                continue
            tit_toks = [t for t in tokenize(it.get("title", "")) if t in keyword_set]
            kw = tit_toks[0] if tit_toks else ""
            push_point(it, kw)

    return {
        "type": ntype,
        "date": ndate,
        "window": {
            "start_ts": float(window_start),
            "end_ts": float(window_end),
        },
        "generated_at": time.time(),
        "generator": generator,
        "news_count": len(ordered),
        "keywords": keywords[:10],
        "points": points,
        "ai_enhanced": bool(ai_points),
    }
