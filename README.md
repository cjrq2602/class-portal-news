# News Worker（新闻抓取 + 每日总结 · GitHub Actions 子项目）

本目录是一个**独立可部署**的子项目：把它整份推到你的**公开 GitHub 仓库**，
GitHub Actions 就会定时抓新闻、生成每日总结，并把产出 commit 回仓库。
网站端（PythonAnywhere）再从仓库读取这些文件 —— 这是绕开
「PythonAnywhere 免费版白名单抓不了国内新闻站」的关键中转设计。

---

## 一、目录结构

```
github_news_worker/
├── .github/workflows/fetch_news.yml   # 三个 cron 任务 + 手动触发
├── fetch_news.py                      # 多源抓取 → news/YYYY-MM-DD.json
├── make_digest.py                     # 每日总结 → digests/YYYY-MM-DD_{type}.json
├── digest_engine.py                   # 规则引擎（与网站端 mysite/digest_engine.py 逐字一致）
├── sources.json                       # 新闻源配置（含备用 RSSHub 实例）
├── README.md                          # 本文件
└── SCHEMA.md                          # JSON 契约说明（网站端按此消费）
```

依赖：**仅 Python 标准库**（urllib / xml.etree / json / email.utils），
无需 `pip install` 任何包，Actions 里也无需缓存步骤。

---

## 二、建仓与上传

```bash
# 1) 在 GitHub 新建一个【公开】仓库，例如 classmates-portal-news
# 2) 本地进入本目录并推送
cd github_news_worker
git init
git branch -M main
git remote add origin https://github.com/<你的用户名>/<仓库名>.git
git add .
git commit -m "init news worker"
git push -u origin main
```

> ⚠️ `.github/workflows/` 目录必须一起推上去，否则 Actions 不会有任务。
> 若 `git push` 后看不到 workflow，检查仓库 **Actions 页签**是否已启用；
> 新的 fork/私有仓库首次使用 Actions 需要点一次「I understand my workflows, go ahead and enable them」。

---

## 三、开启 Actions 与手动验证

1. 进仓库 **Actions** 页签 → 左侧选 `News Worker`。
2. 右侧 **Run workflow** → 选择任务：
   - `fetch` → 只抓新闻；
   - `today` / `yesterday` → 只生成对应总结。
3. 跑完点开日志，正常输出形如：

```
== 开始抓取：5 个源，超时 10s/源 ==
  [成功] 新华网    解析 42 条，采用 38 条
  [跳过] 央视新闻  网络失败：<urlopen error timed out>   ← 单源失败不影响整体
== 完成：成功源 4/5，本次新增 96 条 ==
```

4. 回仓库首页确认出现 `news/2026-10-17.json` 与 `digests/` 文件。

---

## 四、配置 SECRETS（AI 润色为**可选**）

仓库 **Settings → Secrets and variables → Actions → New repository secret**：

| Secret 名 | 必填 | 说明 |
|---|---|---|
| `AI_API_KEY` | 否 | 配了才启用 AI 润色；不配则走纯规则引擎 |
| `AI_BASE_URL` | 否 | OpenAI 兼容接口地址，默认 `https://api.openai.com/v1` |
| `AI_MODEL` | 否 | 默认 `gpt-4o-mini` |

**AI 是可选项，且失败自动降级**：没配 key、接口超时、返回格式异常、限流……
`make_digest.py` 都会捕获并回退到纯规则结果，**绝不会让任务失败或产出空总结**。
日志里会明确打印 `[AI] ...` 说明用了还是降级了。

---

## 五、cron 与北京时间的对应关系（重要）

GitHub Actions 的 `cron` 一律用 **UTC**，所以要和北京时间（UTC+8）对齐必须减 8 小时：

| 你想要的北京时间 | UTC cron | 本工作流用途 |
|---|---|---|
| 每 20 分钟 | `*/20 * * * *` | 抓新闻 |
| **次日 00:00** | `0 16 * * *` | 生成【昨日总结】（覆盖昨天 00:00~24:00） |
| **当天 09:00** | `0 1 * * *` | 生成【今日总结】（覆盖今天 00:00~09:00） |

两份总结写入**不同文件**（`_yesterday.json` / `_today.json`），互不覆盖。

两点提醒：
1. `schedule` 只在**默认分支**上生效（这里是 `main`）。
2. GitHub 的 cron 在高峰期**可能延迟几分钟**——这不影响正确性：网站端有
   **惰性兜底**，用户访问时若发现「已过应生成时刻却缺失」，会自己用同一套规则引擎补生成。

---

## 六、换/修新闻源（RSSHub 实例失效时）

编辑 `sources.json`：

- 把失效条目的 `url` 域名换成备用实例：
  `rsshub.app` / `rsshub.rssforever.com` / `rsshub.feeded.xyz` / `hub.slarker.me`
- 或给同一条新增一个 `{"name": "新华网", "url": "<另一个实例>", ...}`
  （同名多条会都抓，最终按 `source+url` 去重，等于自动冗余）。
- 临时停用某源：把 `"enabled": false`。
- 完全新增一家媒体：加一条 `{name, url, enabled, note}`，`name` 会成为网站端的来源筛选项。

> 建议至少保留 **3 个不同域名**的源，避免单一 RSSHub 实例挂掉导致全站无新闻。
> 若你有自建 RSSHub，把实例地址填进 `url` 最稳。

---

## 七、产出位置

| 路径 | 内容 |
|---|---|
| `news/YYYY-MM-DD.json` | 该北京日期抓到的新闻（合并式写入，去重、时间倒序、上限 400 条） |
| `news/index.json` | 可用日期文件清单（站点兜底枚举用） |
| `digests/YYYY-MM-DD_today.json` | 今日总结（00:00~09:00 窗口） |
| `digests/YYYY-MM-DD_yesterday.json` | 昨日总结（全天窗口） |
| `digests/index.json` | 总结文件清单 |

字段定义见 [SCHEMA.md](./SCHEMA.md)。

---

## 八、网站端如何消费（核对清单）

1. 把网站 `news_sync.py` 里的 `NEWS_REPO` 改成 `<你的用户名>/<仓库名>`
   （或设环境变量 `NEWS_REPO`）。
2. PythonAnywhere Bash 跑 `python3.10 connectivity_test.py` 验证连通性。
3. 站点只访问 `api.github.com` 与 `raw.githubusercontent.com`——都在免费版白名单内。
   匿名 API 限速 **60 次/小时**；本站用「列目录 + SHA 比对」把每次同步压到 2 次请求，
   且缓存 300 秒，正常完全够用。若你同步很频繁或想更保险，可配 `NEWS_GITHUB_TOKEN`
   （只读即可）把限速提到 5000 次/小时。

---

## 九、故障排查

| 现象 | 原因与处理 |
|---|---|
| Actions 里没有这个工作流 | `.github/workflows/` 没推上去，或仓库未启用 Actions |
| 三个 job 只有一个跑 | 正常：每次 schedule 只触发对应那个任务；想全跑用手动触发 |
| `[跳过] xxx 网络失败` | RSSHub 实例抖动/限流，换实例或等待；单源失败不影响整体 |
| 全部源失败、任务红灯 | 检查 `sources.json` 的 url 是否还有效；换实例后再手动触发 |
| `news/` 目录为空 | 手动触发一次 `fetch`；若仍为空，看日志里每个源的失败原因 |
| 总结要点不足 8 条 | 窗口内新闻太少（如凌晨只有几条），属正常；生成条数 = 实际可用新闻数 |
| 总结时间差 8 小时 | 时区理解错误：cron 是 UTC，`0 16` 才是北京次日 00:00；见第五节 |
| 提交步骤反复失败 | 并发冲突，工作流已内置 3 次 `pull --rebase` 重试；仍失败会在下次运行带上 |
