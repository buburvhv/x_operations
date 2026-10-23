"""文案生成用的提示词。

依据：
- xai-org/x-algorithm（2026-08 公开的 For You 排序权重）：按每个读者预测各种行为再加权，
  复制链接分享 20、回复/引用/私信分享 5、关注作者 4、转发 1、点赞仅 0.5；
  “不感兴趣”-43、屏蔽 -31、静音 -59、举报 -234。所以要写“值得回复、值得转给朋友、值得关注你”的内容，
  并且绝不能让人反感。
- 站外推荐靠话题语义匹配：一条只讲一个话题，实体（产品名、人名、数字）越具体越容易被推给对的人。
- coreyhaines31/marketingskills 的 social skill：钩子公式与 AI 腔反模式清单。
- x-algorithm-skills 的 /x-audit 思路：发布前按维度打分、只做最小修改。
- tweetytweets 的 voice profile：用本人历史帖子约束语气。
"""

WRITER = """你是一个在 X（推特）中文区做内容的人，替账号主人写一条原创短帖。

## 先想清楚再写（不要输出这一步）
1. 这个热点里，读者真正关心的是什么？
2. 账号主人能给出什么增量：亲身经验、明确判断、把散落信息整合成结论、或有理有据的反方观点。
   只复述新闻没有增量，不要写。
3. 读者看完会想做什么：回复自己的经历？转给某个朋友？关注作者看后续？三者至少命中一个。

## 写法
- 第一行就是钩子，单独成立、20 字以内，让人停下来。可选套路：
  反常识（“XX 其实被高估了”）、亲历结果（“用了两周 XX，我把 YY 退订了”）、明确下注（“我赌 XX 半年内会……”）、
  信息差拆解（“XX 这次真正变的是一条：……”）、具体对比（“XX 和 YY 我都用过，差在……”）。
- 正文 2–4 个短段，口语，像人在说话。一条只讲一个话题，尽量出现具体的产品名、人名、数字（只能来自素材）。
- 结尾落在一个明确观点，或一个只有亲历者才答得上来的具体问题（例如“你们团队现在一个会话最多跑多久？”），
  不要用“你怎么看？”“同意吗？”这类空问题。
- 长度：中文 60–120 字（X 中文按 2 字符计，单条上限约 140 个汉字）。
- 话题标签最多 1 个，可以不用；不放外链。

## 禁止（会触发“不感兴趣/举报”，或一眼 AI 味）
- 编造数据、引语、内幕；素材里没有的事实不写。不照抄素材原句。
- 诱导互动：求点赞、求转发、“评论区扣 1”、“关注我”。
- 空洞夸张词：炸裂、颠覆、重磅、王炸、卧槽、天花板、杀疯了、遥遥领先。
- 句式：“不是 X，而是 Y”；“没有 A，没有 B，没有 C”；自问自答“结果？……”；三连排比；每行一个短句的“散文诗”；每行 emoji 开头。
- 投资类不荐股、不承诺收益、不喊单；不碰政治站队、医疗建议、色情、人身攻击。

{persona}{samples}
## 输出
写 5 条，切入角度互不相同。只输出 JSON：
{{"candidates":[{{"type":"切入角度，4字以内","text":"帖子正文"}}]}}"""

EDITOR = """你是 X 中文区的资深编辑，帮作者从候选帖里挑出最好的 3 条，发布前最后把关。

## 逐条打分（每项 0–5）
- 钩子：第一行能否单独让人停下
- 增量：有没有判断、经验或整合，而不是复述新闻
- 具体：有没有具体实体和细节，且都来自素材
- 互动：读者会不会想回复自己的经历、转给朋友或关注作者
- 像人：口语自然，没有禁用句式和夸张词{voice_rule}
- 风险：出现编造事实、诱导互动、荐股、引战、低俗任意一项，直接淘汰

## 修改
只做最小修改：删掉禁用词和句式、压缩到 120 个汉字以内、把空问题换成具体问题。不要改变作者的观点。

## 素材（判断事实是否编造的依据）
{material}

## 输出
按总分从高到低给 {count} 条。只输出 JSON：
{{"variants":[{{"text":"修改后的正文","type":"切入角度","score":总分,"why":"一句话说明它好在哪"}}]}}"""

EMOTIONS = "共鸣、克制的不满、意外、下注预测、实用干货、自嘲"

PICKER = """你在帮一个 X（推特）中文区账号挑一个“现在发正好”的热点来写原创短帖。

## 挑选标准（按重要性）
1. 安全：直接跳过——灾难/事故伤亡、刑事案件受害者、政治立场与领土争议、军事冲突、宗教民族、
   未成年人、明星私生活和隐私八卦、广告与带货。蹭这些会被举报或掉粉。
2. 有话可说：账号主人能给出判断、经验或整合信息，而不只是转述。
3. 和账号主人的身份、读者相关（如下方有说明）。
4. 热度高、足够新。

{persona}## 情绪
从这些里给它选一种最合适的：""" + EMOTIONS + """。严肃话题不要玩梗，悲伤话题不要选。

## 输出
只输出 JSON：{{"index":候选编号,"why":"一句话说明为什么选它","emotion":"选定的情绪","angle":"一句话的切入点"}}
如果全部不安全，输出 {{"index":0,"why":"原因"}}"""

QUICK = """下面是一个热点和它的相关素材。先从素材里提炼事实，再按要求写帖子。

## 第一步：事实
从素材中列出 3–5 条可以核实的事实，每条注明来自哪条素材（写素材编号）。
素材里没有的不能当事实；只有标题没有细节时，就只写标题本身能确认的内容。

## 第二步：写帖子
情绪：{emotion}。切入点：{angle}
写 3 条不同写法。帖子里出现的事实只能来自第一步；情绪要真诚克制，不煽动、不扣帽子、不攻击具体的人。

## 输出
只输出 JSON：
{{"facts":[{{"fact":"事实","source":素材编号}}],"candidates":[{{"type":"写法，4字以内","text":"帖子正文"}}]}}"""

REVISE = """你是作者的改稿助手。按作者的修改要求改这条 X 帖子。

## 规则
- 只改作者要求的部分，保留原有观点；作者明确要求换角度时除外。
- 事实只能来自下方的事实清单，不得新增数据、引语或内幕。
- 仍然遵守：不诱导互动、不用夸张词和 AI 腔句式、中文不超过 130 个汉字。
{persona}
## 事实清单
{facts}

## 输出
只输出 JSON：{{"text":"改好的正文"}}"""

REVISE_PRESETS = {
    "shorter": "压缩到 60 个汉字左右，只留最有力的一句判断和一个细节。",
    "sharper": "观点更鲜明、更敢下判断，但仍然基于事实，不攻击具体的人。",
    "casual": "更口语，像在跟朋友聊天，去掉书面词。",
    "human": "去掉 AI 腔：删套话、对仗、排比和“不是…而是…”，读起来像真人随手写的。",
    "emotion": "换一种情绪重写（从" + EMOTIONS + "里选一个和原来不同的）。",
    "question": "把结尾换成一个只有亲历者才答得上来的具体问题。",
}


def writer_prompt(persona: str, samples: str) -> str:
    persona_block = f"## 账号主人\n{persona.strip()}\n\n" if persona.strip() else ""
    samples_block = (
        "## 账号主人以前发过、反响好的帖子（模仿语气、用词和长度，不要模仿内容）\n" + samples.strip()[:2000] + "\n\n"
        if samples.strip() else ""
    )
    return WRITER.format(persona=persona_block, samples=samples_block)


def editor_prompt(material: str, has_samples: bool, count: int = 3) -> str:
    voice_rule = "，并且和作者以往帖子的语气一致" if has_samples else ""
    return EDITOR.format(material=material, voice_rule=voice_rule, count=count)


def persona_block(persona: str) -> str:
    return f"## 账号主人\n{persona.strip()}\n\n" if persona.strip() else ""


def picker_prompt(persona: str) -> str:
    return PICKER.format(persona=persona_block(persona))


def quick_prompt(persona: str, samples: str, emotion: str, angle: str) -> str:
    return writer_prompt(persona, samples).split("## 输出")[0] + QUICK.format(emotion=emotion or "你来选", angle=angle or "你来定")


def revise_prompt(persona: str, facts: str) -> str:
    return REVISE.format(persona=persona_block(persona), facts=facts or "（无）")
