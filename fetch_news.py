"""
fetch_news.py —— 多源新闻抓取（运行于 GitHub Actions，部署到公开仓库）
====================================================================
职责：
1. 读 sources.json（多个冗余 RSS 源），逐个抓取并解析 RSS 2.0 / Atom；
2. 归一化发布时间为 UTC Unix 秒 + 北京日期（UTC+8）；
3. 按北京日期分组，去重（source+url 唯一，标题同源重复也跳过）后
   合并写入 news/YYYY-MM-DD.json（已存在则合并，不覆盖历史）；
4. 写一份 news/index.json 供站点做兜底列举。

平台约束：仅标准库（urllib / xml.etree / email.utils），无任何第三方依赖。
失败策略：单源超时(10s)或解析失败 → 跳过该源，其余源照常；
         全部源失败 → 退出码非 0（让 Actions 显示红灯），但已写入的文件保留。

用法：
    python3 fetch_news.py                       # 默认：读 ./sources.json，输出 ./news/
    python3 fetch_news.py --limit-per-source 30 --days 2 --verbose
    python3 fetch_news.py --sources ../sources.json --out-dir /tmp/news
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Optional

# 规则引擎与本文件同目录（部署时一起上传），提供稳定 news_id
import digest_engine

BEIJING_OFFSET = timedelta(hours=8)   # 北京时间 = UTC + 8
HTTP_TIMEOUT = 10                     # 单源超时（秒）
MAX_ITEMS_PER_DAY = 400               # 单日文件条目上限（防仓库无限增长）
_TAG_RE = re.compile(r"<[^>]+>")
_SPACE_RE = re.compile(r"[\s\u3000]+")


# ============================================================
# 时间工具（全部显式换算，不依赖服务器本地时区）
# ============================================================
def parse_pubdate(raw: str) -> float:
    """
    解析发布时间 → UTC Unix 秒。
    支持 RFC-822（RSS pubDate）与 ISO-8601（Atom updated/published）。
    无法解析时回退为"当前时间"。

    注意：naive datetime（源里没写时区）一律按 UTC 解释，
    用 timezone.utc 而非 mktime —— mktime 依赖本机时区，会导致 8 小时偏移。
    """
    raw = (raw or "").strip()
    if not raw:
        return time.time()
    # RFC-822：Tue, 17 Oct 2026 12:30:00 +0800
    try:
        dt = parsedate_to_datetime(raw)
        if dt is not None:
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.timestamp()
    except Exception:  # noqa: BLE001
        pass
    # ISO-8601：2026-10-17T12:30:00+08:00 / 2026-10-17T12:30:00Z
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except Exception:  # noqa: BLE001
        return time.time()


def beijing_date_of(ts: float) -> str:
    """Unix 秒 → 北京日期 YYYY-MM-DD（UTC+8 换算）。"""
    return (datetime.utcfromtimestamp(ts) + BEIJING_OFFSET).strftime("%Y-%m-%d")


def beijing_str_of(ts: float) -> str:
    """Unix 秒 → 北京时间字符串（文件里存一份可读值，便于人工核对）。"""
    return (datetime.utcfromtimestamp(ts) + BEIJING_OFFSET).strftime("%Y-%m-%d %H:%M")


def clean(text: str, limit: int = 200) -> str:
    """去 HTML 标签、压缩空白并截断（服务端只做文本清洗，不做图像处理）。"""
    s = _SPACE_RE.sub(" ", _TAG_RE.sub(" ", str(text or ""))).strip()
    return s[:limit]


# ============================================================
# 抓取与解析
# ============================================================
def http_get(url: str) -> bytes:
    """带 UA 的 GET（部分源会拒绝空 UA）；timeout=10s。"""
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (compatible; class-portal-news-worker/1.0)",
        "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml, */*",
    })
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
        return resp.read()


def parse_feed(xml_bytes: bytes) -> list[dict[str, str]]:
    """
    解析 RSS 2.0 (<item>) 或 Atom (<entry>)，返回 [{title, link, pub, desc}]。
    命名空间用本地名匹配，兼容各家 RSSHub 输出的差异。
    """
    items: list[dict[str, str]] = []
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError:
        return items

    def local(tag: str) -> str:
        return tag.rsplit("}", 1)[-1].lower()

    def find_text(node: ET.Element, names: tuple[str, ...]) -> str:
        """在直接子节点里找指定本地名（兼容带命名空间）。"""
        for child in node:
            if local(child.tag) in names:
                if child.text:
                    return child.text
                # Atom 的 <link href="..."/> 形式
                href = child.attrib.get("href")
                if href:
                    return href
        return ""

    # 找 item/entry 节点（任意深度，兼容包一层 <channel>）
    for node in root.iter():
        if local(node.tag) not in ("item", "entry"):
            continue
        title = clean(find_text(node, ("title",)), 200)
        link = find_text(node, ("link", "id", "guid")).strip()
        if not link.startswith("http"):
            # RSS 里 link 可能在 <guid isPermaLink="true">
            guid_node = find_text(node, ("guid",)).strip()
            link = guid_node if guid_node.startswith("http") else link
        pub = find_text(node, ("pubdate", "published", "updated", "date"))
        desc = find_text(node, ("description", "summary", "content"))
        if title and link.startswith("http"):
            items.append({"title": title, "link": link, "pub": pub,
                          "desc": clean(desc, 300)})
    return items


def load_sources(path: str) -> list[dict[str, Any]]:
    """读 sources.json；兼容顶层直接是数组或 {sources:[...]} 两种写法。"""
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    srcs = data.get("sources") if isinstance(data, dict) else data
    out: list[dict[str, Any]] = []
    for s in (srcs or []):
        if not isinstance(s, dict):
            continue
        if s.get("enabled", True) is False:
            continue
        name = str(s.get("name") or "").strip()
        url = str(s.get("url") or "").strip()
        if name and url.startswith("http"):
            out.append({"name": name, "url": url})
    return out


# ============================================================
# 文件读写（合并式，幂等）
# ============================================================
def load_day_file(path: str) -> dict[str, Any]:
    if not os.path.exists(path):
        return {"date": "", "generated_at": 0.0, "count": 0, "items": []}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict) or not isinstance(data.get("items"), list):
            return {"date": "", "generated_at": 0.0, "count": 0, "items": []}
        return data
    except Exception:  # noqa: BLE001 —— 坏文件当空处理，本次重写
        return {"date": "", "generated_at": 0.0, "count": 0, "items": []}


def merge_and_save(out_dir: str, bj_date: str,
                   new_items: list[dict[str, Any]]) -> tuple[int, int]:
    """
    合并写入 news/<bj_date>.json。
    返回 (最终条数, 本次新增条数)。
    """
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, f"{bj_date}.json")
    data = load_day_file(path)
    items: list[dict[str, Any]] = list(data.get("items") or [])

    seen: set[str] = {(str(i.get("source")), str(i.get("url"))) for i in items}
    seen_titles: set[str] = {(str(i.get("source")), str(i.get("title"))) for i in items}
    added = 0
    for it in new_items:
        key = (it["source"], it["url"])
        tkey = (it["source"], it["title"])
        if key in seen or tkey in seen_titles:
            continue
        seen.add(key)
        seen_titles.add(tkey)
        items.append(it)
        added += 1

    # 时间倒序 + 单日截断（保留最新的 MAX_ITEMS_PER_DAY 条）
    items.sort(key=lambda x: -float(x.get("published_at") or 0))
    if len(items) > MAX_ITEMS_PER_DAY:
        items = items[:MAX_ITEMS_PER_DAY]

    payload = {
        "date": bj_date,
        "generated_at": time.time(),
        "count": len(items),
        "items": items,
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)
    return len(items), added


def write_index(out_dir: str, max_days: int = 14) -> None:
    """写 news/index.json：日期倒序的文件清单，供站点兜底枚举。"""
    try:
        names = sorted([n for n in os.listdir(out_dir)
                        if len(n) == 15 and n.endswith(".json") and n[4] == "-"],
                       reverse=True)[:max_days]
    except OSError:
        names = []
    payload = {"generated_at": time.time(), "files": names}
    with open(os.path.join(out_dir, "index.json"), "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)


# ============================================================
# 主流程
# ============================================================
def main() -> int:
    ap = argparse.ArgumentParser(description="多源新闻抓取 → news/YYYY-MM-DD.json")
    ap.add_argument("--sources", default="sources.json", help="源配置路径")
    ap.add_argument("--out-dir", default="news", help="输出目录")
    ap.add_argument("--limit-per-source", type=int, default=60, help="单源最多取多少条")
    ap.add_argument("--days", type=int, default=3, help="只保留最近 N 个北京日期的文件")
    ap.add_argument("--verbose", action="store_true", help="打印每条抓取明细")
    args = ap.parse_args()

    srcs = load_sources(args.sources)
    if not srcs:
        print("!! sources.json 里没有可用源（检查 name/url/enabled）", file=sys.stderr)
        return 2

    cutoff_date: str = beijing_date_of(time.time() - args.days * 86400)
    per_day: dict[str, list[dict[str, Any]]] = {}
    ok_sources = 0

    print(f"== 开始抓取：{len(srcs)} 个源，超时 {HTTP_TIMEOUT}s/源 ==")
    for s in srcs:
        name, url = s["name"], s["url"]
        try:
            raw = http_get(url)
            entries = parse_feed(raw)
        except urllib.error.URLError as exc:
            print(f"  [跳过] {name:<8} 网络失败：{exc}", file=sys.stderr)
            continue
        except Exception as exc:  # noqa: BLE001
            print(f"  [跳过] {name:<8} 解析失败：{type(exc).__name__}: {exc}",
                  file=sys.stderr)
            continue
        if not entries:
            print(f"  [跳过] {name:<8} 未解析到条目（源可能改版或为空）", file=sys.stderr)
            continue
        ok_sources += 1
        kept = 0
        for e in entries[:args.limit_per_source]:
            ts = parse_pubdate(e["pub"])
            bj_date = beijing_date_of(ts)
            if bj_date < cutoff_date:
                continue          # 太旧，丢弃（避免仓库无限增长）
            item = {
                "id": digest_engine.news_id(name, e["link"]),
                "source": name,
                "title": e["title"],
                "url": e["link"],
                "summary": e["desc"],
                "published_at": ts,
                "published_bj": beijing_str_of(ts),
            }
            per_day.setdefault(bj_date, []).append(item)
            kept += 1
            if args.verbose:
                print(f"    + [{bj_date}] {name}: {e['title'][:40]}")
        print(f"  [成功] {name:<8} 解析 {len(entries)} 条，采用 {kept} 条")

    if ok_sources == 0:
        print("!! 全部源均失败（网络或源地址问题）——未写入任何文件", file=sys.stderr)
        return 1

    total_added = 0
    print("== 写入文件 ==")
    for bj_date in sorted(per_day.keys(), reverse=True):
        count, added = merge_and_save(args.out_dir, bj_date, per_day[bj_date])
        total_added += added
        print(f"  news/{bj_date}.json  共 {count} 条（本次新增 {added}）")
    write_index(args.out_dir)
    print(f"== 完成：成功源 {ok_sources}/{len(srcs)}，本次新增 {total_added} 条 ==")
    return 0


if __name__ == "__main__":
    sys.exit(main())
