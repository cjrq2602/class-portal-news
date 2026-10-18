# JSON 契约（SCHEMA）

网站端 `news_sync.py` 与 `digest_service.py` **严格按本文件解析这些产出**。
改动字段名必须同步修改网站端，否则页面会读不到数据（但不会 500，会降级为空态）。

---

## 1. `news/YYYY-MM-DD.json` — 单日新闻

```json
{
  "date": "2026-10-17",
  "generated_at": 1792235108.12,
  "count": 42,
  "items": [
    {
      "id": "a1b2c3d4e5f60718",
      "source": "新华网",
      "title": "标题文本",
      "url": "https://example.com/article",
      "summary": "摘要文本（已去 HTML 标签，≤300 字）",
      "published_at": 1792230000.0,
      "published_bj": "2026-10-17 12:30"
    }
  ]
}
```

| 字段 | 类型 | 说明 |
|---|---|---|
| `date` | string | **北京日期** `YYYY-MM-DD`（该文件归档的日期，按发布时间归组） |
| `generated_at` | number | 生成时刻（Unix 秒，UTC 数值） |
| `count` | number | `items` 条数（与数组长度一致） |
| `items[].id` | string | `digest_engine.news_id(source, url)`：sha1 前 16 位十六进制，**两侧一致** |
| `items[].source` | string | 来源显示名（= `sources.json` 的 `name`，网站端用作筛选项） |
| `items[].title` | string | 标题 |
| `items[].url` | string | 原文链接（网站端以 `target="_blank" rel="noopener noreferrer"` 打开） |
| `items[].summary` | string | 摘要（纯文本） |
| `items[].published_at` | number | 发布时间（Unix 秒，UTC 数值）——**分页/窗口过滤的唯一依据** |
| `items[].published_bj` | string | 北京时间的可读副本（仅供人工核对，程序不依赖它） |

约束：`items` 按 `published_at` 倒序；单日上限 400 条；同 `(source,url)` 唯一。

---

## 2. `news/index.json` — 日期清单（站点兜底枚举）

```json
{ "generated_at": 1792235108.12, "files": ["2026-10-17.json", "2026-10-16.json"] }
```

---

## 3. `digests/YYYY-MM-DD_{today|yesterday}.json` — 每日总结

```json
{
  "schema": 1,
  "type": "today",
  "date": "2026-10-17",
  "window": { "start_ts": 1792204800.0, "end_ts": 1792237200.0 },
  "window_bj": { "start": "2026-10-17 00:00", "end": "2026-10-17 09:00" },
  "generated_at": 1792241100.0,
  "generator": "action",
  "news_count": 42,
  "keywords": ["经济", "交通", "天气"],
  "points": [
    {
      "text": "【经济】GDP 同比增速回升至 5.2%（新华网）",
      "keyword": "经济",
      "news_ids": ["a1b2c3d4e5f60718"],
      "refs": [
        {
          "id": "a1b2c3d4e5f60718",
          "title": "标题文本",
          "source": "新华网",
          "url": "https://example.com/article",
          "published_at": 1792230000.0
        }
      ]
    }
  ],
  "ai_enhanced": false
}
```

| 字段 | 类型 | 说明 |
|---|---|---|
| `schema` | number | 契约版本，当前 `1` |
| `type` | string | `today` \| `yesterday`（与网站端 `news_digests.ntype` 对应） |
| `date` | string | 所属**北京日期**（与 `ntype` 组成唯一键，重复生成只覆盖同一条） |
| `window.start_ts` / `end_ts` | number | 时间窗口（Unix 秒，左闭右开） |
| `window_bj.start` / `end` | string | 窗口的北京时间可读文案（页面直接展示） |
| `generated_at` | number | 生成时刻（Unix 秒） |
| `generator` | string | `action`（Actions 定时）｜ `lazy`（站点兜底）｜ `manual`（后台重生成） |
| `news_count` | number | 纳入窗口的新闻条数 |
| `keywords` | string[] | Top 关键词（最多 10 个，页面渲染为标签） |
| `points` | object[] | **8~15 条**要点（受窗口新闻数限制，新闻不足时可能更少） |
| `points[].text` | string | 单句要点（规则引擎产出；`ai_enhanced=true` 时为 AI 润色版） |
| `points[].keyword` | string | 该要点归属主题词（可为空串） |
| `points[].news_ids` | string[] | 关联新闻 `id` 列表（对应第 1 节） |
| `points[].refs` | object[] | 可点击的原文引用（id/title/source/url/published_at） |
| `ai_enhanced` | boolean | 是否使用了 AI 润色（缺 API key 或调用失败时为 `false`） |

网站端额外注入（**不在文件里，来自数据库列**）：
`edited`（管理员是否手改过）、`updated_at`、`exists`。
`edited=1` 的记录，网站端在下次同步时**不会被仓库文件覆盖**。

---

## 4. `digests/index.json` — 总结文件清单

```json
{ "generated_at": 1792241100.0, "files": ["2026-10-17_today.json", "2026-10-16_yesterday.json"] }
```

---

## 5. 空数据约定

窗口内没有新闻时**仍会写出合法文件**：

```json
{ "schema": 1, "type": "today", "date": "2026-10-17",
  "window": {"start_ts": 0, "end_ts": 0}, "window_bj": {"start": "", "end": ""},
  "generated_at": 1792241100.0, "generator": "action",
  "news_count": 0, "keywords": [], "points": [], "ai_enhanced": false }
```

这样网站端永远能取到文件，页面会显示「该时段没有抓到新闻，暂无要点」的空态，
而不是「文件缺失」的错误。
