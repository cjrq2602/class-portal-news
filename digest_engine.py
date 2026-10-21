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


_CJK_CHAR_RE = re.compile(r"[\u4e00-\u9fff]")


def has_chinese(text: str, least: int = 4) -> bool:
    """判断文本是否含足够中文（用于优先选择中文新闻，保证总结是中文的）。"""
    return len(_CJK_CHAR_RE.findall(str(text or ""))) >= least


def trim_sentence(text: str, limit: int = 80) -> str:
    """
    截断到 limit 字，并去掉尾部不完整的半句（以逗号/顿号等结尾的碎片），
    避免出现"……have ，另有 2 条"这类断句。
    """
    t: str = str(text or "").strip()
    if len(t) > limit:
        t = t[:limit]
    # 无论是否发生截断，末尾不是终止标点时都回退到最后一个完整分句
    if t and t[-1] not in "。！？!?…":
        cut: int = -1
        for sep in ("，", "、", ",", "；", ";", "："):
            idx: int = t.rfind(sep)
            if idx > len(t) * 0.45:
                cut = max(cut, idx)
        if cut > 0:
            t = t[:cut]
        else:
            t = t.rstrip("，、,；;： ") + "…"
    return t.rstrip("，、,；;： ")


def short_phrase(title: str, limit: int = 16) -> str:
    """标题兜底成短主题：取第一个标点前的完整短语，避免生硬截字。"""
    t: str = clean_text(title, 60)
    for sep in ("——", "：", "，", "、", "；", "|", "-"):
        if sep in t:
            head: str = t.split(sep)[0].strip()
            if len(head) >= 4:
                t = head
                break
    return t[:limit].rstrip("，、,；;： ")




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
# 术语表（新词解释）：文中出现即自动附一句人话解释
# ============================================================
GLOSSARY: dict[str, str] = {
    "AGI": "通用人工智能，指能在各类任务上都接近人类水平、而不是只擅长单一任务的 AI",
    "AI": "人工智能，让机器完成识别、生成、判断等原本需要人做的任务",
    "AIGC": "由 AI 生成内容，包括文字、图片、音频和视频",
    "Agent": "智能体，能自己拆解任务、调用工具并连续执行的 AI 程序",
    "API": "应用程序接口，让不同软件按约定好的方式互相调用",
    "AR": "增强现实，把虚拟图像叠加到真实世界画面上",
    "ETF": "交易型开放式指数基金，像股票一样在交易所买卖的一篮子资产",
    "GDPR": "欧盟通用数据保护条例，对个人数据的收集与使用设定了严格要求",
    "GPU": "图形处理器，因为擅长大规模并行计算，成了 AI 训练的主力硬件",
    "HBM": "高带宽内存，把多层内存堆叠起来给 AI 芯片喂数据，缓解带宽瓶颈",
    "IPO": "首次公开募股，公司第一次把股票卖给公众并上市",
    "K8s": "Kubernetes 的简称，用来管理大量容器的编排系统",
    "LLM": "大语言模型，主打文字理解与生成，是「大模型」的同义词",
    "NFT": "非同质化代币，用区块链标记某个数字物品的唯一所有权",
    "RAG": "检索增强生成，先查资料再让模型作答，用来减少胡编",
    "Robotaxi": "无人驾驶出租车，车里没有司机，靠自动驾驶系统接单运营",
    "SaaS": "软件即服务，把软件放在云端按订阅方式使用",
    "Transformer": "2017 年提出的神经网络结构，是当今绝大多数大模型的基础",
    "VR": "虚拟现实，戴上头显进入一个全虚拟的画面",
    "Web3": "以区块链和用户自持资产为核心的下一代互联网构想",
    "XR": "扩展现实，VR、AR、MR 的统称",
    "云计算": "把服务器、存储和软件放到远端按需租用，不用自己买机器",
    "供应链": "从原材料、零部件到成品交付的整条协作链条",
    "光刻机": "把电路图案刻到硅片上的核心设备，制程越先进要求越高",
    "元宇宙": "试图把虚拟世界与现实身份、经济打通的沉浸式互联网形态",
    "光伏": "把太阳光直接转成电能的技术，也就是太阳能发电",
    "开源": "源代码公开，允许他人查看、修改并再分发",
    "微调": "在通用大模型基础上用专门数据再训练，让它更懂某个领域",
    "数据中心": "集中放置服务器、供电与散热设施的大型机房",
    "数字孪生": "给现实中的设备或系统建一个实时同步的虚拟副本",
    "无人机": "不需要机上飞行员、靠远程或自主控制的飞行器",
    "新能源": "相对煤炭石油而言的清洁能源，常见的是光伏、风电和储能",
    "晶圆": "制造芯片用的硅片，一块晶圆上会切出大量芯片",
    "机器学习": "让程序从数据里自己总结规律，而不是把规则一条条写死",
    "激光雷达": "用激光测量距离的传感器，是自动驾驶感知周围环境的重要部件",
    "碳中和": "排放的二氧化碳与吸收的量相抵，净排放为零",
    "算力": "计算能力，通常用 GPU 数量和浮点性能衡量，是训练大模型的门槛",
    "半导体": "制造芯片的材料产业，处在整个电子工业的上游",
    "区块链": "把数据按块链接、由多方共同维护的账本技术",
    "反垄断": "防止企业滥用市场支配地位、限制竞争",
    "多模态": "同一个模型同时处理文字、图片、音频、视频等多种信息",
    "大模型": "参数量巨大的神经网络，靠海量文本训练出语言理解与生成能力",
    "实体清单": "美国商务部的一份出口管制名单，进入名单的企业在采购美国技术时会受限",
    "推理": "模型根据输入算出输出的过程；日常语境里也指模型的逻辑思考能力",
    "开源许可证": "规定开源代码能被怎样使用、修改和再分发的法律文本，常见的有 MIT、Apache、GPL",
    "数据中心液冷": "用液体代替空气给服务器散热，适合高密度算力场景",
    "智能驾驶": "车辆辅助甚至替代人完成驾驶操作，按自动化程度分为 L0 到 L5",
    "流片": "把设计好的芯片图纸送去工厂真正生产出样品，费用高、周期长",
    "隐私": "个人信息的收集、使用与传播边界",
    "零信任": "不默认信任任何内部网络，每次访问都要重新验证",
    "零日漏洞": "厂商还不知道、因而暂时没有补丁的安全缺陷",
    # ---- 时政词汇（第八期新增；两侧 digest_engine.py 须保持逐字一致） ----
    "一带一路": "“丝绸之路经济带”和“21 世纪海上丝绸之路”国际合作倡议",
    "两会": "全国人民代表大会和政协年度全体会议的合称，一般在每年 3 月召开",
    "新质生产力": "以科技创新为主导、高科技高效能高质量的先进生产力",
    "高质量发展": "从“有没有”转向“好不好”的发展要求，强调创新驱动与可持续性",
    "中国式现代化": "中国共产党领导的社会主义现代化，含五大特征",
    "全过程人民民主": "人民在选举、决策、管理、监督各环节持续参与的民主形式",
    "人类命运共同体": "倡导各国共同建设持久和平、共同繁荣世界的理念",
    "共同富裕": "全体人民共享发展成果、逐步缩小贫富差距的目标",
    "双碳": "碳达峰与碳中和：2030 年前碳排放达峰、2060 年前实现净零排放",
    "乡村振兴": "以产业兴旺、生态宜居等为总要求的“三农”工作战略",
    "新发展格局": "以国内大循环为主体、国内国际双循环相互促进的格局",
    "供给侧结构性改革": "从提高供给质量出发推进结构调整的经济改革主线",
    "全国统一大市场": "打破地方保护与市场分割、让商品要素全国畅通流动的市场体系",
    "宏观调控": "政府运用经济和法律手段对国民经济总量进行调节",
    "营商环境": "企业开办经营所处的制度环境，含审批效率、法治保障等维度",
    "专精特新": "专业化、精细化、特色化、新颖化中小企业的培育方向",
    "实体经济": "物质产品和服务生产流通的经济活动，与虚拟经济相对",
    "放管服": "简政放权、放管结合、优化服务的行政体制改革",
    "多边主义": "通过联合国等多边机制协商处理国际事务的原则",
    "金砖国家": "巴西、俄罗斯、印度、中国、南非等组成的新兴市场合作机制",
    "上海合作组织": "2001 年成立、以安全合作起家的区域性国际组织",
    "RCEP": "《区域全面经济伙伴关系协定》，由 15 国签署的全球最大自贸区",
    "全球治理": "各国通过国际规则共同管理气候、卫生、金融等全球事务",
    "京津冀协同发展": "疏解北京非首都功能、建设雄安新区的国家战略",
    "长三角一体化": "推动沪苏浙皖协同发展的国家战略",
    "粤港澳大湾区": "香港、澳门与珠三角九市共建的世界级城市群战略",
    "长江经济带": "沿江 11 省市“共抓大保护、不搞大开发”的发展战略",
    "两个确立": "确立党中央的核心地位与指导思想地位的政治表述",
    "两个维护": "维护党中央核心地位与党中央权威、集中统一领导",
    "四个意识": "政治意识、大局意识、核心意识、看齐意识",
    "四个自信": "道路自信、理论自信、制度自信、文化自信",
    "中央八项规定": "2012 年通过的改进工作作风、密切联系群众的规定",
}
# 英文术语必须按单词边界匹配，避免 "AI" 命中 "said" 这类误伤
_GLOSSARY_LATIN = re.compile(
    r"(?<![A-Za-z])(" + "|".join(
        sorted([k for k in GLOSSARY if k.isascii()], key=len, reverse=True)
    ) + r")(?![A-Za-z])")


# ============================================================
# 主题词词典：只有命中这里的词才能当段落主题（避免二字滑窗碎片）
# ============================================================
TOPIC_WORDS: set[str] = {
    # 技术
    "大模型", "模型", "算法", "算力", "芯片", "半导体", "光刻", "晶圆", "流片",
    "架构", "平台", "系统", "数据", "网络", "量子", "通信", "卫星", "航天",
    "机器人", "无人机", "驾驶", "雷达", "传感器", "电池", "储能", "光伏",
    "风电", "核电", "氢能", "充电", "续航", "能源", "电力", "电网",
    "服务器", "存储", "云端", "边缘", "开源", "社区", "开发", "开发者",
    "代码", "编程", "软件", "硬件", "更新", "升级", "版本", "发布", "推出",
    "上线", "内测", "公测", "首发", "预售", "评测", "测试", "体验",
    "功能", "性能", "效率", "精度", "安全", "漏洞", "攻击", "黑客", "勒索",
    "病毒", "诈骗", "泄露", "入侵", "风险", "防护", "加密", "认证", "隐私",
    # 消费电子
    "手机", "电脑", "平板", "耳机", "眼镜", "手表", "家电", "屏幕", "显示",
    "摄像", "影像", "游戏", "主机", "显卡", "处理器", "内存", "硬盘",
    # 商业
    "融资", "投资", "估值", "上市", "股价", "市值", "财报", "营收", "利润",
    "亏损", "裁员", "招聘", "收购", "合并", "拆分", "重组", "破产",
    "市场", "行业", "产业", "企业", "公司", "集团", "品牌", "渠道", "零售",
    "电商", "直播", "物流", "供应链", "制造", "工厂", "产能", "订单", "交付",
    # 政策
    "监管", "政策", "法规", "法案", "条例", "规定", "标准", "合规", "审查",
    "处罚", "罚款", "诉讼", "判决", "专利", "版权", "商标", "反垄断",
    # 生活
    "交通", "出行", "地图", "导航", "外卖", "快递", "餐饮", "旅游", "酒店",
    "教育", "学校", "学生", "考试", "就业", "医疗", "医院", "医生", "患者",
    "药物", "疫苗", "健康", "养老", "保险", "金融", "银行", "支付",
    "利率", "汇率", "通胀", "央行", "货币", "财政", "税收", "补贴", "关税",
    "贸易", "出口", "进口", "出海", "全球", "创新", "研发", "突破", "技术",
    "研究", "成果", "论文", "内容", "视频", "音频", "图片", "音乐",
    "电影", "社交", "广告", "营销", "流量", "增长", "用户", "消费者",
}


def _display_term(tok: str) -> str:
    """把分词小写结果还原成更适合展示的写法（命中术语表时用原大小写）。"""
    if not tok:
        return ""
    for term in GLOSSARY:
        if term.lower() == tok.lower():
            return term
    return tok


# 泛词：能当关键词但信息量低，选"段落主题"时尽量跳过
GENERIC_WORDS: set[str] = {
    "发布", "推出", "上线", "更新", "升级", "版本", "公司", "企业", "集团",
    "市场", "行业", "产业", "用户", "消费者", "内容", "视频", "音频", "图片",
    "音乐", "功能", "性能", "体验", "系统", "平台", "数据", "网络", "技术",
    "研究", "成果", "全球", "增长", "服务",
}


def topic_from_title(title: str, weight: dict[str, int]) -> str:
    """
    从一条标题里挑最像"主题"的词：
    1) 术语表命中（长词优先，英文按词边界）；
    2) 词典内的具体名词；
    3) 词典内的泛词；
    4) 返回空串（调用方回退）。
    这样能保证段落主题与该段事实句来自同一条新闻，不会出现"主题说内容、事实说液冷"。
    """
    text: str = str(title or "")
    # 1) 术语表（子串 / 英文词边界）
    for term in sorted(GLOSSARY, key=len, reverse=True):
        if term.isascii():
            if re.search(r"(?<![A-Za-z])" + re.escape(term) + r"(?![A-Za-z])",
                         text, re.IGNORECASE):
                return term
        elif term in text:
            return term
    toks: list[str] = sorted(set(tokenize(text)),
                             key=lambda t: (-weight.get(t, 0), -len(t)))
    # 2) 具体名词
    for t in toks:
        if t in TOPIC_WORDS and t not in GENERIC_WORDS:
            return t
    # 3) 泛词
    for t in toks:
        if t in TOPIC_WORDS:
            return t
    return ""



# ============================================================
# 观点模板（按主题类别，规则引擎也能给出"看法"）
# ============================================================
CATEGORY_HINTS: dict[str, tuple[str, ...]] = {
    "tech": ("发布", "推出", "上线", "升级", "模型", "算法", "技术", "芯片", "算力",
             "开源", "突破", "研发", "专利", "架构", "系统", "平台"),
    "money": ("融资", "收购", "上市", "IPO", "股价", "估值", "营收", "利润",
              "投资", "市值", "财报", "裁员", "亏损", "增长"),
    "policy": ("监管", "法案", "规定", "政策", "处罚", "合规", "反垄断",
               "草案", "条例", "标准", "禁令", "审查"),
    "security": ("漏洞", "攻击", "泄露", "诈骗", "黑客", "安全", "勒索",
                 "病毒", "入侵", "风险"),
    "consumer": ("手机", "售价", "评测", "耳机", "汽车", "新品", "体验",
                 "用户", "订阅", "上市"),
}

VIEW_TEMPLATES: dict[str, tuple[str, ...]] = {
    "tech": (
        "这类进展更像是把已有能力往工程化落地推了一步，真正的分水岭在于成本和可靠性能不能同时压下来。",
        "技术路线的胜负很少由一次发布会决定，接下来要看的是它能不能被更多团队低成本复用。",
        "从节奏上看，行业已经从「能不能做」转向「做得便不便宜」，这通常意味着竞争会更务实。",
    ),
    "money": (
        "资本动作往往领先产品一年，值得盯的不是金额，而是钱最后花向了算力、场景还是补贴。",
        "融资和估值只能说明市场情绪，能不能兑现，还要看下一两个季度的收入和留存。",
        "这类交易背后通常是行业在提前整合，中小玩家的选择空间会被进一步压缩。",
    ),
    "policy": (
        "规则一旦落地，短期会抬高合规成本，中期则会把不合规的玩家清出场，头部反而受益。",
        "监管的看点在于执行尺度：条款写得严不严是一回事，真正查不查是另一回事。",
        "对普通用户来说，政策带来的变化通常滞后半年到一年，但方向一旦确定就很难逆转。",
    ),
    "security": (
        "安全事件频繁出现说明攻击面还在扩大，普通用户能做的仍然是把补丁和二次验证当成日常。",
        "这类问题很少是单点失误，更常见的是权限设计和应急流程一起出了漏洞。",
        "对企业而言，安全投入平时看不见产出，出事时却是唯一能兜底的东西。",
    ),
    "consumer": (
        "消费电子的看点不在参数堆叠，而在这些参数能不能变成日常可感知的体验差异。",
        "价格和生态往往比硬件规格更能决定一款产品能不能留下来。",
        "真正影响口碑的是使用一两周之后的细节，而不是发布会上的演示。",
    ),
    "general": (
        "这条消息短期内的实际影响可能有限，但它指向的方向值得放进观察名单。",
        "单看一条新闻容易高估影响，放到最近几周的脉络里，它更像是一个渐进变化中的节点。",
        "对读者来说，值得留意的不是结论本身，而是它背后哪些前提正在被改变。",
    ),
}


def _classify(text: str) -> str:
    """按命中提示词的数量给一条新闻归类（用于选择观点模板）。"""
    best, best_hits = "general", 0
    for cat, hints in CATEGORY_HINTS.items():
        hits = sum(1 for h in hints if h in text)
        if hits > best_hits:
            best, best_hits = cat, hits
    return best


def _pick_view(cat: str, seed_text: str) -> str:
    """稳定地从模板池选一条观点（同一主题每次生成结果一致）。"""
    pool = VIEW_TEMPLATES.get(cat) or VIEW_TEMPLATES["general"]
    idx = int(hashlib.sha1(seed_text.encode("utf-8")).hexdigest(), 16) % len(pool)
    return pool[idx]


def _glossary_hits(texts: list[str], limit: int = 6) -> list[dict[str, str]]:
    """
    从标题里挑出命中的术语，返回 [{term, desc}]。
    英文按单词边界匹配，中文按子串匹配。
    """
    blob: str = " ".join(texts)
    found: list[dict[str, str]] = []
    seen: set[str] = set()
    # 先匹配英文术语（边界精确）
    for m in _GLOSSARY_LATIN.finditer(blob):
        term = m.group(1)
        if term in seen:
            continue
        seen.add(term)
        found.append({"term": term, "desc": GLOSSARY[term]})
        if len(found) >= limit:
            return found
    # 再匹配中文术语（长词优先，避免"大模型"被"模型"截断的问题）
    for term in sorted([k for k in GLOSSARY if not k.isascii()],
                       key=len, reverse=True):
        if term in seen:
            continue
        # 已被更长的同族术语覆盖（如已收"数据中心液冷"就不再收"数据中心"）
        if any(term in s for s in seen if not s.isascii()):
            continue
        if term in blob:
            seen.add(term)
            found.append({"term": term, "desc": GLOSSARY[term]})
            if len(found) >= limit:
                break
    return found


def compose_article(clusters: list[dict[str, Any]],
                    keywords: list[str],
                    weight: dict[str, int],
                    items: list[dict[str, Any]],
                    ntype: str,
                    ndate: str,
                    max_sections: int = 4) -> dict[str, Any]:
    """
    把聚类结果写成一篇连贯的中文短评（纯规则，无 AI）。

    结构：导语 → 3~4 个主题段（事实 + 看法）→ 术语解释 → 结语。
    每段都带 refs，前端可以点击跳转原文。
    """
    total: int = len(items)
    sources: set[str] = {str(it.get("source", "")) for it in items if it.get("source")}
    date_label: str = "今天" if ntype == "today" else "过去 24 小时"

    # ---- 导语：交代规模与主线（只采用词典内的真实主题词，过滤滑窗碎片）----
    def _kw_ok(k: str, allow_generic: bool = True) -> bool:
        if k not in TOPIC_WORDS and not any(k.lower() == g.lower() for g in GLOSSARY):
            return False
        return allow_generic or k not in GENERIC_WORDS

    clean_kws: list[str] = [_display_term(k) for k in keywords if _kw_ok(k, False)]
    # 具体词不足两条时，补上泛词，避免导语太空
    if len(clean_kws) < 2:
        clean_kws += [_display_term(k) for k in keywords if _kw_ok(k, True)]
    # 去重保序
    seen_kw: set[str] = set()
    top_kws: list[str] = []
    for k in clean_kws:
        if k and k not in seen_kw:
            seen_kw.add(k)
            top_kws.append(k)
        if len(top_kws) >= 3:
            break

    if top_kws:
        kw_phrase: str = "、".join(top_kws)
        lead: str = (f"{date_label}共收录 {total} 条新闻，来自 {len(sources)} 家媒体。"
                     f"把标题放在一起看，讨论主要集中在 {kw_phrase} 这几个方向上，"
                     f"其余消息更像是零散补充。")
    else:
        lead: str = (f"{date_label}共收录 {total} 条新闻，来自 {len(sources)} 家媒体。"
                     f"整体没有压倒性的单一主线，更像是几条线索并行推进。")

    paragraphs: list[dict[str, Any]] = [{"kind": "lead", "text": lead}]

    # ---- 主题段：每簇一段 ----
    connectors: tuple[str, ...] = (
        "先看", "与此同时", "另一个值得注意的方向是", "相比之下", "再往后",
    )
    section_count: int = 0
    used_ids: set[str] = set()
    used_topics: set[str] = set()

    def _w_of(it: dict[str, Any]) -> int:
        return sum(weight.get(t, 0) for t in set(tokenize(it.get("title", ""))))

    for c in clusters:
        if section_count >= max_sections:
            break
        reps = sorted(c["items"], key=lambda it: -_w_of(it))
        if not reps:
            continue
        # 中文优先：总结必须是中文的，纯英文簇直接跳过
        zh_reps = [it for it in reps if has_chinese(str(it.get("title", "")), 4)]
        if not zh_reps:
            continue
        rep = zh_reps[0]

        # ---- 定主题词：用代表条目自身标题；同段主题不重复 ----
        def _pick_topic(item: dict[str, Any]) -> str:
            title: str = str(item.get("title", ""))
            first: str = topic_from_title(title, weight)
            if first and first not in used_topics:
                return first
            # 第一个词已被前面的段落用过 → 用标题里的下一个合格词
            cands: list[str] = sorted(set(tokenize(title)),
                                      key=lambda t: (-weight.get(t, 0), -len(t)))
            for t in cands:
                if not (t in TOPIC_WORDS or any(t.lower() == g.lower() for g in GLOSSARY)):
                    continue
                shown: str = _display_term(t)
                if shown not in used_topics:
                    return shown
            return first or ""

        picked: str = _pick_topic(rep)
        # 代表条目选不出合适主题 → 在同簇里找一条能选出新主题的中文新闻
        if not picked or picked in used_topics:
            for alt in zh_reps[1:]:
                alt_topic: str = _pick_topic(alt)
                if alt_topic and alt_topic not in used_topics:
                    rep, picked = alt, alt_topic
                    break

        # 仍无主题词 → 用标题里第一个完整短语兜底
        if not picked or picked in used_topics:
            picked = short_phrase(str(rep.get("title", "")), 16) or "新动态"
        used_topics.add(picked)

        # 代表条目最终确定后再生成引用，并做去重检查（防止同一条重复成段）
        ref = _item_ref(rep)
        if ref["id"] in used_ids:
            continue
        used_ids.add(ref["id"])

        # 事实句：优先摘要首句，退回标题；截断时去掉不完整的半句
        fact: str = trim_sentence(item_sentence(rep, limit=90), 90)
        if not fact or not has_chinese(fact, 4):
            continue
        # 补一句"另有 N 条同向消息"，体现热度
        extra: int = len(c["items"]) - 1
        extra_phrase: str = f"，另有 {extra} 条消息指向同一话题" if extra > 0 else ""

        cat: str = _classify(f"{rep.get('title', '')} {fact}")
        view: str = _pick_view(cat, picked)

        conn: str = connectors[section_count % len(connectors)]
        text: str = f"{conn}「{picked}」。{fact}{extra_phrase}。{view}"
        paragraphs.append({
            "kind": "section",
            "heading": picked,
            "text": text,
            "news_ids": [ref["id"]],
            "refs": [ref],
        })
        section_count += 1

    # ---- 术语解释：扫全部标题 + 已写好的段落正文，给新词加注 ----
    all_titles: list[str] = [str(it.get("title", "")) for it in items]
    all_titles += [str(p.get("text", "")) for p in paragraphs
                   if p.get("kind") == "section"]
    terms: list[dict[str, str]] = _glossary_hits(all_titles, limit=6)
    if terms:
        paragraphs.append({"kind": "glossary", "items": terms})

    # ---- 结语：给一个整体判断 ----
    if len(top_kws) >= 2:
        outlook: str = (
            f"把这几条放在一起看，最值得跟踪的是 {top_kws[0]} 和 {top_kws[1]} 两条线："
            f"一条决定技术能走多远，一条决定它多快能落到普通人的日常里。"
            f"至于其他消息，可以先放进观察名单，等后续有更实的进展再判断。")
    else:
        outlook = ("整体看，这个时段的新闻还处在「消息多、结论少」的阶段，"
                   "与其急着下判断，不如盯住接下来一两周有没有可验证的后续动作。")
    paragraphs.append({"kind": "outlook", "text": outlook})

    return {
        "title": f"{ndate} 新闻总结",
        "paragraphs": paragraphs,
        "word_count": sum(len(p.get("text", "")) for p in paragraphs),
    }


# ============================================================
# 兼容旧数据：由要点列表合成一篇连贯短评
# ============================================================
def compose_article_from_points(payload: dict[str, Any]) -> dict[str, Any]:
    """
    给"没有 article 字段的历史总结"现场合成一篇短评（不写库）。
    用于升级展示：老数据也能显示导语/段落/名词解释/结语。
    """
    pts: list[dict[str, Any]] = payload.get("points") or []
    kws: list[str] = [str(k) for k in (payload.get("keywords") or []) if k]
    ntype: str = str(payload.get("type") or "today")
    ndate: str = str(payload.get("date") or "")
    total: int = int(payload.get("news_count") or len(pts))
    date_label: str = "今天" if ntype == "today" else "过去 24 小时"

    top_kws: list[str] = [_display_term(k) for k in kws[:3]]
    if top_kws:
        lead = (f"{date_label}共收录 {total} 条新闻。从标题看，讨论主要集中在 "
                f"{'、'.join(top_kws)} 这几个方向上。")
    else:
        lead = f"{date_label}共收录 {total} 条新闻，整体没有单一主线。"

    paragraphs: list[dict[str, Any]] = [{"kind": "lead", "text": lead}]
    connectors: tuple[str, ...] = ("先看", "与此同时", "另一个方向是", "相比之下")

    for i, p in enumerate(pts[:4]):
        raw: str = str(p.get("text") or "").strip()
        if not raw:
            continue
        # 旧要点形如 "【关键词】正文"，拆出主题词
        heading: str = ""
        if raw.startswith("【") and "】" in raw:
            heading, raw = raw[1:].split("】", 1)
        if not heading:
            heading = _display_term(str(p.get("keyword") or "")) or "要点"
        cat: str = _classify(raw)
        view: str = _pick_view(cat, heading + str(i))
        text: str = f"{connectors[i % len(connectors)]}「{heading}」。{raw.rstrip('。')}。{view}"
        paragraphs.append({
            "kind": "section",
            "heading": heading,
            "text": text,
            "news_ids": p.get("news_ids") or [],
            "refs": p.get("refs") or [],
        })

    # 术语解释：从要点文本里扫
    blob: str = " ".join(str(p.get("text") or "") for p in pts)
    terms: list[dict[str, str]] = _glossary_hits([blob], limit=5)
    if terms:
        paragraphs.append({"kind": "glossary", "items": terms})

    if len(top_kws) >= 2:
        outlook = (f"综合来看，{top_kws[0]} 和 {top_kws[1]} 是接下来值得继续跟踪的两条线，"
                   f"其余消息还需要更多后续进展才能判断方向。")
    else:
        outlook = "信息还在陆续出现，建议过一两天再回看，结论会更清晰。"
    paragraphs.append({"kind": "outlook", "text": outlook})

    return {
        "title": f"{ndate} 新闻总结",
        "paragraphs": paragraphs,
        "word_count": sum(len(p.get("text", "")) for p in paragraphs),
        "legacy": True,
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
        # 连贯中文短评（导语 + 主题段 + 术语解释 + 结语），前端优先渲染它
        "article": compose_article(clusters, keywords, weight, ordered, ntype, ndate),
        "ai_enhanced": bool(ai_points),
    }
