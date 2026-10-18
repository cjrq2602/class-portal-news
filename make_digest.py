"""
make_digest.py —— 每日总结生成（运行于 GitHub Actions，部署到公开仓库）
======================================================================
职责：
1. 按北京时间计算目标日期与时间窗口：
     --type yesterday → date=北京昨天，窗口 = 昨天 00:00 ~ 次日 00:00
     --type today     → date=北京今天，窗口 = 今天 00:00 ~ 09:00
2. 读取 news/<date>.json，过滤出窗口内条目；
3. 调用 digest_engine.build_digest() 生成 8~15 条要点 + Top 关键词（纯规则）；
4. 可选 AI 润色：仅当环境变量 AI_API_KEY 存在且未加 --no-ai 时调用，
   任何失败都自动降级为纯规则结果，绝不阻断任务；
5. 写 digests/<date>_<type>.json（重复运行覆盖同一文件 → 幂等），
   并刷新 digests/index.json。

平台约束：仅标准库（urllib / json / argparse）。窗口内无新闻时也写出合法空结构，
          保证站点侧永远拿得到文件（不会出现"文件缺失"）。

用法：
    python3 make_digest.py --type today
    python3 make_digest.py --type yesterday --date 2026-10-16
    python3 make_digest.py --type today --no-ai          # 强制纯规则
"""

from __future__ import annotations

import argparse
import calendar
import json
import os
import sys
import time
import urllib.request
from datetime import datetime, timedelta
from typing import Any, Optional

import digest_engine

BEIJING_OFFSET = timedelta(hours=8)
AI_TIMEOUT = 30


# ============================================================
# 北京时间的日期与窗口（必须与服务端 common.py 的算法完全一致）
# 注意：绝不能用 datetime.timestamp() —— 它按【本机时区】解释 naive datetime，
#       在 UTC+8 的本机跑会偏移 8 小时；GitHub runner 恰好是 UTC 才侥幸正确。
#       统一用 calendar.timegm()（按 UTC 解释），保证任何机器结果一致。
# ============================================================
def beijing_now() -> datetime:
    """当前北京时间（runner 时钟为 UTC，显式 +8）。"""
    return datetime.utcnow() + BEIJING_OFFSET


def bj_day_start_ts(date_str: str) -> float:
    """
    北京日期 → 该日 00:00（北京）对应 Unix 秒。
    北京日 D 的 00:00 == UTC D 00:00 − 8 小时。
    """
    d = datetime.strptime(date_str, "%Y-%m-%d")
    return float(calendar.timegm(d.timetuple()) - 8 * 3600)


def bj_str(ts: float) -> str:
    return (datetime.utcfromtimestamp(ts) + BEIJING_OFFSET).strftime("%Y-%m-%d %H:%M")


def resolve(ntype: str, date_arg: str) -> tuple[str, float, float]:
    """返回 (所属北京日期, 窗口起, 窗口止)。"""
    today = beijing_now().strftime("%Y-%m-%d")
    if date_arg:
        if ntype == "today":
            start = bj_day_start_ts(date_arg)
            return date_arg, start, start + 9 * 3600.0
        start = bj_day_start_ts(date_arg)
        return date_arg, start, start + 86400.0
    if ntype == "yesterday":
        y = (beijing_now() - timedelta(days=1)).strftime("%Y-%m-%d")
        start = bj_day_start_ts(y)
        return y, start, start + 86400.0
    start = bj_day_start_ts(today)
    return today, start, start + 9 * 3600.0


# ============================================================
# 读取新闻
# ============================================================
def load_items(news_dir: str, start: float, end: float) -> list[dict[str, Any]]:
    """读取窗口内新闻（扫描目录下所有日期文件，按 published_at 精确过滤）。"""
    out: list[dict[str, Any]] = []
    if not os.path.isdir(news_dir):
        return out
    for name in sorted(os.listdir(news_dir)):
        if not (len(name) == 15 and name.endswith(".json") and name[4] == "-"):
            continue
        try:
            with open(os.path.join(news_dir, name), "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:  # noqa: BLE001
            continue
        for it in (data.get("items") or []):
            try:
                ts = float(it.get("published_at") or 0)
            except (TypeError, ValueError):
                continue
            if start <= ts < end and it.get("title") and it.get("url"):
                out.append({
                    "id": str(it.get("id") or digest_engine.news_id(
                        str(it.get("source") or ""), str(it.get("url") or ""))),
                    "title": str(it.get("title") or ""),
                    "source": str(it.get("source") or ""),
                    "url": str(it.get("url") or ""),
                    "summary": str(it.get("summary") or ""),
                    "published_at": ts,
                })
    out.sort(key=lambda x: -x["published_at"])
    return out


# ============================================================
# 可选 AI 润色（有 key 才用；失败自动降级）
# ============================================================
def ai_points(items: list[dict[str, Any]], ntype: str, date_str: str) -> list[str]:
    """
    让 AI 把窗口内新闻改写成 8~15 条中文单句要点。
    任何异常（无 key / 网络 / 限流 / 返回格式不对）都返回空列表 → 上层走纯规则。
    """
    key = os.environ.get("AI_API_KEY", "").strip()
    base = os.environ.get("AI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
    model = os.environ.get("AI_MODEL", "gpt-4o-mini")
    if not key:
        print("  [AI] 未配置 AI_API_KEY，使用纯规则引擎")
        return []
    digest_engine.compute_keywords([i["title"] for i in items], top_n=15)
    titles = "\n".join(f"- ({i['source']}) {i['title']}" for i in items[:60])
    prompt = (
        f"下面是{date_str}（{ntype}）的新闻标题列表。请提炼成 8~15 条中文要点，"
        "每条一句话、不超过 45 字、客观中立、不要编号、不要解释。"
        "只输出要点本身，每行一条。\n\n" + titles
    )
    body = json.dumps({
        "model": model,
        "messages": [
            {"role": "system", "content": "你是严谨的新闻编辑，只做事实性归纳。"},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.3,
    }).encode("utf-8")
    req = urllib.request.Request(
        f"{base}/chat/completions", data=body,
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {key}"})
    try:
        with urllib.request.urlopen(req, timeout=AI_TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
        content = (((data.get("choices") or [{}])[0].get("message") or {})
                   .get("content") or "")
        lines: list[str] = []
        for ln in str(content).splitlines():
            s = digest_engine.clean_text(ln, 60)
            s = s.lstrip("-•*·0123456789.、) ").strip()
            if len(s) >= 6:
                lines.append(s)
        if lines:
            print(f"  [AI] 使用 {model} 润色，得到 {len(lines)} 条要点")
            return lines[:15]
        print("  [AI] 返回内容为空，降级为纯规则")
    except Exception as exc:  # noqa: BLE001 —— AI 失败绝不影响任务
        print(f"  [AI] 调用失败（{type(exc).__name__}: {exc}），降级为纯规则")
    return []


# ============================================================
# 输出
# ============================================================
def write_digest(out_dir: str, ntype: str, date_str: str,
                 payload: dict[str, Any]) -> str:
    """写 digests/<date>_<type>.json（覆盖同名 → 幂等）。返回文件路径。"""
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, f"{date_str}_{ntype}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)
    return path


def write_index(out_dir: str, max_files: int = 30) -> None:
    """写 digests/index.json（总结文件清单，日期倒序），供站点兜底枚举。"""
    try:
        names = sorted([n for n in os.listdir(out_dir)
                        if n.endswith("_today.json") or n.endswith("_yesterday.json")],
                       reverse=True)[:max_files]
    except OSError:
        names = []
    with open(os.path.join(out_dir, "index.json"), "w", encoding="utf-8") as f:
        json.dump({"generated_at": time.time(), "files": names}, f,
                  ensure_ascii=False, indent=1)


def main() -> int:
    ap = argparse.ArgumentParser(description="生成每日总结（规则引擎 + 可选 AI 润色）")
    ap.add_argument("--type", required=True, choices=["today", "yesterday"],
                    help="today=今日总结(00:00~09:00)；yesterday=昨日总结(全天)")
    ap.add_argument("--date", default="", help="北京日期 YYYY-MM-DD（默认按当前北京时间推算）")
    ap.add_argument("--news-dir", default="news", help="新闻目录")
    ap.add_argument("--out-dir", default="digests", help="总结输出目录")
    ap.add_argument("--no-ai", action="store_true", help="强制纯规则，不调用 AI")
    args = ap.parse_args()

    date_str, start, end = resolve(args.type, args.date)
    items = load_items(args.news_dir, start, end)
    ai: list[str] = [] if args.no_ai else ai_points(items, args.type, date_str)

    payload: dict[str, Any] = digest_engine.build_digest(
        items, args.type, date_str, start, end,
        generator="action", ai_points=ai or None)
    payload["schema"] = 1
    payload["window_bj"] = {"start": bj_str(start), "end": bj_str(end)}

    path = write_digest(args.out_dir, args.type, date_str, payload)
    write_index(args.out_dir)

    print(f"== 生成完成：{path} ==")
    print(f"   北京日期：{date_str}（{args.type}）")
    print(f"   窗口：{payload['window_bj']['start']} ~ {payload['window_bj']['end']}")
    print(f"   新闻：{payload['news_count']} 条 ｜ 要点：{len(payload['points'])} 条"
          f" ｜ 关键词：{len(payload['keywords'])} 个")
    print(f"   生成器：{payload['generator']} ｜ AI 增强：{payload['ai_enhanced']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
