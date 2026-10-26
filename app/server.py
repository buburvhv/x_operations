from __future__ import annotations

import html
import json
import os
import random
import re
import sqlite3
import subprocess
import threading
import urllib.error
import urllib.parse
import urllib.request
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from flask import Flask, jsonify, request, send_from_directory

from . import prompts

ROOT = Path(__file__).resolve().parent.parent
APP_DIR = Path(__file__).resolve().parent
DATA = ROOT / "data"
DATA.mkdir(exist_ok=True)
DB_PATH = DATA / "poster.db"
AGENTX = ROOT / "bin" / ("agentx.exe" if os.name == "nt" else "agentx")
AGENT_HOME = DATA / "agentx-home"
TZ = ZoneInfo("Asia/Shanghai")
ACCOUNT = "main"
DAILY_LIMIT = 3
MIN_GAP = timedelta(hours=4)
DAY_START, DAY_END = 9, 23  # 只在自然活跃时段发布
MAX_LATE = timedelta(hours=6)  # 错过太久就作废，不补发
LOCK = threading.Lock()
REGIONS = {"": "全球", "united-states": "美国", "japan": "日本", "singapore": "新加坡", "united-kingdom": "英国"}
STATUS_TEXT = {"queued": "待发布", "posted": "已发布", "failed": "发布失败", "expired": "已过期"}

app = Flask(__name__, static_folder=None)


@app.before_request
def guard_origin():
    """只接受本站页面发起的写请求，挡住其他网页跨站操控发帖。"""
    if request.method in {"GET", "HEAD", "OPTIONS"}:
        return None
    origin = request.origin or request.headers.get("Referer", "")
    if origin and not origin.startswith(request.host_url.rstrip("/")):
        return jsonify({"ok": False, "error": "拒绝来自其他网站的请求"}), 403
    return None


def now() -> datetime:
    return datetime.now(TZ)


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db() -> None:
    with closing(connect()) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS drafts (
                id INTEGER PRIMARY KEY,
                text TEXT NOT NULL,
                scheduled_at TEXT,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                posted_at TEXT,
                tweet_url TEXT,
                tweet_id TEXT,
                error TEXT,
                metrics TEXT,
                image TEXT,
                thread INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        # 旧版本的“草稿”现在直接视为待发布
        conn.execute("UPDATE drafts SET status='queued' WHERE status='draft'")
        conn.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        for column, definition in (("tweet_id", "TEXT"), ("metrics", "TEXT"), ("image", "TEXT"), ("thread", "INTEGER NOT NULL DEFAULT 0")):
            if column not in {row["name"] for row in conn.execute("PRAGMA table_info(drafts)")}:
                conn.execute(f"ALTER TABLE drafts ADD COLUMN {column} {definition}")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS saved (
                id INTEGER PRIMARY KEY,
                text TEXT NOT NULL,
                note TEXT NOT NULL DEFAULT '',
                source_url TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL
            )
            """
        )
        conn.commit()


# 后台可调的配置；环境变量作为首次启动的默认值
SETTING_DEFAULTS = {
    "ai_base_url": lambda: os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1"),
    "ai_api_key": lambda: os.getenv("OPENAI_API_KEY", ""),
    "ai_model": lambda: os.getenv("OPENAI_MODEL", "gpt-4.1-mini"),
    "ai_editor_model": lambda: "",  # 选热点和编辑把关用的模型，留空则与写手相同
    "daily_limit": lambda: str(DAILY_LIMIT),
    "min_gap_hours": lambda: str(int(MIN_GAP.total_seconds() // 3600)),
    "persona": lambda: "",
    "samples": lambda: "",
    "best_hours": lambda: "",  # 由历史数据算出的最佳发布小时，逗号分隔
}
LIMITS = {"daily_limit": (1, 5), "min_gap_hours": (2, 12)}  # 上限故意设得保守


def get_settings() -> dict:
    values = {key: default() for key, default in SETTING_DEFAULTS.items()}
    with closing(connect()) as conn:
        values.update({row["key"]: row["value"] for row in conn.execute("SELECT key, value FROM settings")})
    return values


def save_settings(changes: dict) -> None:
    with closing(connect()) as conn:
        for key, value in changes.items():
            conn.execute("INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
        conn.commit()


def daily_limit() -> int:
    return int(get_settings()["daily_limit"])


def min_gap() -> timedelta:
    return timedelta(hours=int(get_settings()["min_gap_hours"]))


def run_agentx(args: list[str], timeout: int = 30, extra_env: dict | None = None) -> dict:
    env = os.environ.copy()
    env["AGENTX_HOME"] = str(AGENT_HOME)
    env.update(extra_env or {})
    try:
        result = subprocess.run(
            [str(AGENTX), *args],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
            encoding="utf-8",
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": {"code": "timeout", "message": "timeout"}}
    except OSError as exc:
        return {"ok": False, "error": {"code": "missing_binary", "message": str(exc)}}
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        message = (result.stderr or result.stdout or "empty response").strip()
        return {"ok": False, "error": {"code": "invalid_response", "message": message}}


def friendly_error(result: dict) -> str:
    """把 AgentX 的英文错误翻译成用户能照着做的中文提示。"""
    error = result.get("error") or {}
    if isinstance(error, str):
        return error
    code, message = error.get("code", ""), error.get("message", "")
    lower = message.lower()
    if "loginacid" in lower or "e-mail/phone verification" in lower:
        return "X 要求邮箱/手机验证码确认身份，这一步无法自动完成。请用下方「浏览器导入」登录。"
    if "2fa" in lower or ("two" in lower and "factor" in lower):
        return "账号开启了两步验证，无法用密码自动登录。请用下方「浏览器导入」登录。"
    if "denied" in lower or "deny" in lower:
        return "X 拒绝了这次登录（常见于新设备或异常网络）。请先在浏览器正常登录一次，再用「浏览器导入」。"
    if "password" in lower or "wrong" in lower or "incorrect" in lower:
        return "用户名或密码不正确。"
    if "no accounts configured" in lower or ("account" in lower and "not found" in lower):
        return "还没有登录 X 账号。"
    if code == "timeout":
        return "连接 X 超时，请检查网络或代理后重试。"
    if code == "network_error":
        return "无法连接 X，请检查网络或代理。"
    if code == "rate_limited":
        return "X 限流了，请稍后再试。"
    if code == "not_authenticated":
        return "登录状态无效或已过期，请重新登录。"
    if code == "missing_binary":
        return "缺少发布组件 bin/agentx，请先按 README 构建。"
    return f"操作失败：{message or code or '未知错误'}"


def account_state() -> dict:
    result = run_agentx(["account", "list"])
    data = result.get("data") if result.get("ok") else {}
    accounts = data.get("accounts", []) if isinstance(data, dict) else []
    current = next((item for item in accounts if item.get("name") == ACCOUNT), accounts[0] if accounts else None)
    return {
        "connected": current is not None,
        "screenName": (current or {}).get("screenName", ""),
        "binary": AGENTX.exists(),
    }


def verify_login() -> dict:
    """登录/导入后立刻调用 me 验证会话，并让 AgentX 记住 @用户名。"""
    result = run_agentx(["me", "--account", ACCOUNT], timeout=45)
    if result.get("ok"):
        return {"ok": True, "screenName": (result.get("data") or {}).get("screenName", "")}
    run_agentx(["account", "remove", ACCOUNT])
    return {"ok": False, "error": friendly_error(result)}


def rows(status: str | None = None) -> list[dict]:
    sql = "SELECT * FROM drafts"
    params: tuple = ()
    if status:
        sql += " WHERE status = ?"
        params = (status,)
    sql += " ORDER BY COALESCE(scheduled_at, created_at)"
    with closing(connect()) as conn:
        return [dict(row) for row in conn.execute(sql, params)]


def when_text(value: str | None) -> str:
    if not value:
        return ""
    moment = datetime.fromisoformat(value).astimezone(TZ)
    days = (moment.date() - now().date()).days
    prefix = {-1: "昨天", 0: "今天", 1: "明天", 2: "后天"}.get(days, f"{moment.month}月{moment.day}日")
    return f"{prefix} {moment:%H:%M}"


def present(item: dict) -> dict:
    when = item["posted_at"] if item["status"] == "posted" else item["scheduled_at"]
    metrics = None
    if item.get("metrics"):
        metrics = {key: value for key, value in json.loads(item["metrics"]).items() if key != "checked_at"}
    return {**item, "statusText": STATUS_TEXT.get(item["status"], item["status"]), "whenText": when_text(when),
            "metrics": metrics, "hasImage": bool(item.get("image"))}


def occupied_times(exclude: int | None = None) -> list[datetime]:
    values = []
    for item in rows():
        if item["id"] == exclude:
            continue
        if item["status"] == "posted" and item["posted_at"]:
            values.append(datetime.fromisoformat(item["posted_at"]))
        elif item["status"] == "queued" and item["scheduled_at"]:
            values.append(datetime.fromisoformat(item["scheduled_at"]))
    return values


def jitter() -> timedelta:
    return timedelta(minutes=random.randint(15, 40))


def next_morning(moment: datetime) -> datetime:
    day = moment.astimezone(TZ).date() + timedelta(days=1)
    return datetime.combine(day, datetime.min.time(), TZ) + timedelta(hours=DAY_START)


def suggest_time(candidate: datetime | None = None, exclude: int | None = None) -> datetime:
    """找到最近一个满足：活跃时段、每日上限、最小间隔的时间点。

    有历史数据时，优先落在互动最好的那几个小时（best_hours），否则在 9–23 点内顺延。
    """
    preferred = {int(h) for h in get_settings()["best_hours"].split(",") if h.isdigit()}
    preferred = {hour for hour in preferred if DAY_START <= hour < DAY_END}

    def align(slot: datetime) -> datetime:
        """不在最佳时段内时，挪到之后最近的一个最佳小时；今天没有就等明天。"""
        local = slot.astimezone(TZ)
        if not preferred or local.hour in preferred:
            return slot
        later = [hour for hour in sorted(preferred) if hour > local.hour]
        if later:
            return datetime.combine(local.date(), datetime.min.time(), TZ) + timedelta(hours=later[0]) + jitter()
        return next_morning(local) + timedelta(hours=min(preferred) - DAY_START) + jitter()

    slot = align(candidate or now() + timedelta(minutes=10) + jitter())
    if slot < now() + timedelta(minutes=2):
        slot = now() + timedelta(minutes=10)
    taken = occupied_times(exclude)
    for _ in range(1000):
        local = slot.astimezone(TZ)
        if local.hour < DAY_START:
            slot = datetime.combine(local.date(), datetime.min.time(), TZ) + timedelta(hours=DAY_START) + jitter()
            continue
        if local.hour >= DAY_END:
            slot = next_morning(local) + jitter()
            continue
        if preferred and local.hour not in preferred:
            slot = align(slot)
            continue
        if sum(1 for item in taken if item.astimezone(TZ).date() == local.date()) >= daily_limit():
            slot = next_morning(local) + jitter()
            continue
        gap = min_gap()
        clash = [item for item in taken if abs(slot - item) < gap]
        if clash:
            slot = max(clash) + gap + jitter()
            continue
        return slot
    raise RuntimeError("找不到可用的发布时间")


def tweet_url(data: dict) -> tuple[str, str]:
    tweet_id = str(data.get("id", ""))
    handle = (data.get("author") or {}).get("screenName", "") or "i"
    return (f"https://x.com/{handle}/status/{tweet_id}" if tweet_id else ""), tweet_id


def thread_parts(text: str) -> list[str]:
    """按空行拆成线程的各条，并校验每一条都不超过 X 的字数上限。"""
    parts = [part.strip() for part in re.split(r"\n\s*\n", text) if part.strip()]
    if len(parts) < 2:
        raise ValueError("线程至少要有 2 段，段与段之间空一行")
    if len(parts) > 10:
        raise ValueError("一条线程最多 10 段")
    for index, part in enumerate(parts, start=1):
        if x_length(part) > 280:
            raise ValueError(f"第 {index} 段超过 280 字符，请拆短")
    return parts


def post_one(item: dict) -> dict:
    """真正发出一条并落库。调用方负责持锁。每条只尝试一次。

    只有勾选了「作为线程发送」才按空行拆成多条，用回复串起来；否则整段原文发一条。
    """
    parts = thread_parts(item["text"]) if item.get("thread") else [item["text"]]
    image = item.get("image") or ""
    args = ["post", "--account", ACCOUNT, parts[0]]
    if image and Path(image).exists():
        args += ["--media", image]
    result = run_agentx(args, timeout=90)
    posted = now().isoformat()
    with closing(connect()) as conn:
        if not result.get("ok"):
            message = friendly_error(result)
            conn.execute("UPDATE drafts SET status='failed', error=?, updated_at=? WHERE id=?", (message, posted, item["id"]))
            conn.commit()
            return {"ok": False, "error": message}
        url, tweet_id = tweet_url(result.get("data") or {})
        sent = 1
        reply_to = tweet_id
        for part in parts[1:]:
            reply = run_agentx(["reply", "--account", ACCOUNT, reply_to, part], timeout=60)
            if not reply.get("ok"):
                break  # 线程发到一半中断：已发出的保留，不再重试
            sent += 1
            reply_to = str((reply.get("data") or {}).get("id") or reply_to)
        note = None
        if len(parts) > 1 and sent < len(parts):
            note = f"线程只发出了前 {sent} 段（共 {len(parts)} 段），其余未发，没有自动重试"
        conn.execute(
            "UPDATE drafts SET status='posted', posted_at=?, tweet_url=?, tweet_id=?, error=?, updated_at=? WHERE id=?",
            (posted, url, tweet_id, note, posted, item["id"]),
        )
        conn.commit()
    return {"ok": True, "url": url}


def publish_due() -> None:
    if not LOCK.acquire(blocking=False):
        return
    try:
        current = now()
        due = [item for item in rows("queued") if datetime.fromisoformat(item["scheduled_at"]) <= current]
        with closing(connect()) as conn:
            for item in due:
                if current - datetime.fromisoformat(item["scheduled_at"]) > MAX_LATE:
                    conn.execute(
                        "UPDATE drafts SET status='expired', error=?, updated_at=? WHERE id=?",
                        ("错过发布时间超过 6 小时（电脑关机或未登录），为避免集中补发已作废。", current.isoformat(), item["id"]),
                    )
            conn.commit()
        due = [item for item in due if current - datetime.fromisoformat(item["scheduled_at"]) <= MAX_LATE]
        if not due or not account_state()["connected"]:
            return
        # 每轮只发一条，失败不自动重试，避免被判定为异常行为
        post_one(due[0])
    finally:
        LOCK.release()


def scheduler_loop(stop: threading.Event) -> None:
    while not stop.wait(60):
        try:
            publish_due()
            refresh_metrics()
        except Exception as exc:  # 后台线程不能因单次异常退出
            print(f"[scheduler] {exc}", flush=True)


def parse_analytics(payload: dict) -> dict | None:
    """从 analytics 返回里抽出互动数。字段名和嵌套层级都不稳定，所以按名字全局找。"""
    names = {"likes": ("likes", "like_count", "favorite_count"),
             "retweets": ("retweets", "retweet_count"),
             "replies": ("replies", "reply_count"),
             "views": ("views", "view_count", "impressions")}
    found: dict[str, int] = {}

    def walk(node) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                for metric, aliases in names.items():
                    if metric not in found and key in aliases and isinstance(value, (int, float)) and not isinstance(value, bool):
                        found[metric] = int(value)
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(payload.get("data"))
    return found or None


def engagement(metrics: dict) -> int:
    """与热帖排序同一套权重：转发传播价值最高，浏览只作参考。"""
    return metrics.get("likes", 0) + metrics.get("retweets", 0) * 4 + metrics.get("replies", 0) + metrics.get("views", 0) // 200


def refresh_metrics() -> None:
    """每小时回查一次已发帖的互动数据，并据此更新最佳发布时段。"""
    current = now()
    pending = []
    for item in rows("posted"):
        if not item.get("tweet_id"):
            continue
        metrics = json.loads(item["metrics"]) if item.get("metrics") else {}
        checked = metrics.get("checked_at")
        if checked and current - datetime.fromisoformat(checked) < timedelta(hours=1):
            continue
        if item.get("posted_at") and current - datetime.fromisoformat(item["posted_at"]) > timedelta(days=14):
            continue  # 两周后数据基本不再变
        pending.append(item)
    with closing(connect()) as conn:
        for item in pending[:8]:  # 每轮限量，避免请求过密
            result = run_agentx(["analytics", item["tweet_id"]], timeout=30)
            parsed = parse_analytics(result) if result.get("ok") else None
            if not parsed:
                continue
            parsed["checked_at"] = current.isoformat()
            conn.execute("UPDATE drafts SET metrics=? WHERE id=?", (json.dumps(parsed), item["id"]))
        conn.commit()
    update_best_hours()


def update_best_hours() -> None:
    """统计每个小时发出的帖子平均互动，样本够了才启用，取最好的 4 个小时。"""
    buckets: dict[int, list[int]] = {}
    for item in rows("posted"):
        if not item.get("metrics") or not item.get("posted_at"):
            continue
        hour = datetime.fromisoformat(item["posted_at"]).astimezone(TZ).hour
        buckets.setdefault(hour, []).append(engagement(json.loads(item["metrics"])))
    reliable = {hour: sum(scores) / len(scores) for hour, scores in buckets.items() if len(scores) >= 2 and DAY_START <= hour < DAY_END}
    if len(reliable) < 3:
        return
    best = ",".join(str(hour) for hour in sorted(sorted(reliable, key=reliable.get, reverse=True)[:4]))
    if best != get_settings()["best_hours"]:
        save_settings({"best_hours": best})


TREND_CACHE: dict[str, tuple[datetime, list[dict]]] = {}
# 中文板块：用关键词 + X 高级搜索筛出近 24 小时中文高互动帖
BOARDS = {
    "ai": ("AI", "AI OR 大模型 OR ChatGPT OR OpenAI OR Claude OR DeepSeek OR 智能体 OR Agent OR Gemini"),
    "invest": ("投资", "美股 OR A股 OR 港股 OR 基金 OR 降息 OR 美联储 OR 财报 OR 黄金"),
    "crypto": ("加密", "比特币 OR BTC OR 以太坊 OR ETH OR 币圈 OR 空投 OR Solana"),
    "tech": ("科技", "英伟达 OR 苹果 OR 特斯拉 OR 马斯克 OR 芯片 OR 华为 OR 小米"),
    "indie": ("出海创业", "出海 OR 独立开发 OR 创业 OR SaaS OR 副业 OR 产品上线"),
}
HOT_CACHE: dict[str, tuple[datetime, list[dict]]] = {}


def hot_query(keywords: str, min_faves: int = 30) -> str:
    since = (now() - timedelta(days=1)).astimezone(ZoneInfo("UTC")).date().isoformat()
    return f"({keywords}) lang:zh min_faves:{min_faves} since:{since} -filter:replies -filter:retweets"


SPAM = re.compile(
    r"空投|抽奖|开奖|互关|互粉|回关|有关必回|蓝V|一键三连|转发置顶|评论区留下|留下.{0,6}地址|钱包地址|预售|促销码|邀请码|返佣|返利|专属链接|走我的链接"
    r"|0x[0-9a-fA-F]{20,}|色情|无码|约炮|裸聊|Giveaway|Airdrop",
    re.IGNORECASE,
)


def is_spam(item: dict) -> bool:
    """去掉抽奖空投、互关、带货返利和黄赌类帖子：这些热度蹭了只会伤号。"""
    if SPAM.search(item["text"]):
        return True
    # 评论远多于点赞，基本是“留言领奖”式的刷互动
    return item["replies"] > max(item["likes"], 20) * 1.5


def rank_hot(items: list[dict]) -> list[dict]:
    """转发比点赞更能代表“可蹭”的传播热度；评论容易被刷，权重最低。"""
    kept = [item for item in items if not is_spam(item)]
    for item in kept:
        item["score"] = item["likes"] + item["retweets"] * 4 + item["replies"]
        item["url"] = f"https://x.com/{item['author'] or 'i'}/status/{item['id']}"
    return sorted(kept, key=lambda item: item["score"], reverse=True)


def fetch_hot(keywords: str) -> dict:
    cached = HOT_CACHE.get(keywords)
    if cached and now() - cached[0] < timedelta(minutes=15):
        return {"ok": True, "items": cached[1]}
    result = run_agentx(["search", "--account", ACCOUNT, "--type", "Top", "-n", "30", hot_query(keywords)], timeout=45)
    if not result.get("ok"):
        return {"ok": False, "error": friendly_error(result)}
    items = rank_hot(topic_items(result))[:20]
    HOT_CACHE[keywords] = (now(), items)
    return {"ok": True, "items": items}


def parse_trends24(page: str) -> list[dict]:
    """trends24.in 首个列表就是当前小时的 X 热搜，免登录可读。"""
    parts = page.split("class=list-container", 2)
    block = parts[1] if len(parts) > 1 else page
    items = []
    pattern = r'<a href="?([^" >]+)"? class=trend-link>([^<]+)</a>\s*<span class=tweet-count data-count="?([^" >]*)'
    for link, name, count in re.findall(pattern, block):
        query = urllib.parse.parse_qs(urllib.parse.urlparse(html.unescape(link)).query).get("q", [""])[0]
        name = html.unescape(name).strip()
        items.append({"name": name, "query": query or name, "volume": int(count) if count.isdigit() else 0})
    return items[:30]


def fetch_trends(region: str) -> list[dict]:
    cached = TREND_CACHE.get(region)
    if cached and now() - cached[0] < timedelta(minutes=10):
        return cached[1]
    url = f"https://trends24.in/{region}/" if region else "https://trends24.in/"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
    with urllib.request.urlopen(req, timeout=15) as response:
        items = parse_trends24(response.read().decode("utf-8", "replace"))
    TREND_CACHE[region] = (now(), items)
    return items


def topic_items(payload: dict) -> list[dict]:
    values = payload.get("data") if isinstance(payload.get("data"), list) else []
    items = []
    for value in values:
        text = str(value.get("text", "")).strip()
        if not text:
            continue
        metrics = value.get("metrics") or {}
        author = value.get("author") or {}
        items.append({
            "id": value.get("id", ""),
            "text": text,
            "author": author.get("screenName", ""),
            "likes": metrics.get("likes", 0),
            "retweets": metrics.get("retweets", 0),
            "replies": metrics.get("replies", 0),
        })
    return items


def x_length(text: str) -> int:
    """X 的计数规则：拉丁字母等算 1，中日韩文字和 emoji 算 2，链接固定 23。"""
    total = 0
    for part in re.split(r"(https?://\S+)", text):
        if part.startswith(("http://", "https://")):
            total += 23
            continue
        for char in part:
            code = ord(char)
            light = code <= 4351 or 8192 <= code <= 8205 or 8208 <= code <= 8223 or 8242 <= code <= 8247
            total += 1 if light else 2
    return total


CJK = r"[　-〿一-鿿＀-￯]"


def tidy(text: str) -> str:
    """去掉模型常见的多余空白，但保留换行分段和英文单词间的空格。"""
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in str(text).replace("\r", "").split("\n")]
    joined = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
    return re.sub(rf"(?<={CJK}) (?={CJK})", "", joined)


def parse_json(completion: str) -> dict:
    """从模型输出里取出 JSON。

    有的模型会先输出 <think>…</think> 思考过程再给答案，思考里也可能有大括号，所以先去掉这段。
    答案本身有时包在 ```json 代码块里，取其中最大的一个对象，避免误拿到举例用的小片段。
    """
    raw = re.sub(r"<think>.*?</think>", "", str(completion), flags=re.S)
    found = []
    for match in re.finditer(r"\{", raw):
        try:
            value, end = json.JSONDecoder().raw_decode(raw, match.start())
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            found.append((end - match.start(), value))
    return max(found)[1] if found else {}


def copy_variants(text: str, angle: str, completion: str) -> list[str]:
    values = parse_json(completion).get("variants")
    if values is None:
        values = [line.strip(" -0123456789.") for line in completion.strip().splitlines()]
    variants = []
    for value in values:
        item = tidy(value.get("text", "") if isinstance(value, dict) else value)
        if item and x_length(item) <= 280 and item not in variants:
            variants.append(item)
    if variants:
        return variants[:3]
    stance = angle.strip() or "值得关注"
    return [f"{text[:60]} {stance}"[:130]]


AI_ERRORS = (RuntimeError, KeyError, IndexError, ValueError, urllib.error.URLError, TimeoutError, json.JSONDecodeError)


def editor_model(settings: dict) -> str:
    return settings.get("ai_editor_model", "").strip() or settings["ai_model"]


def chat(settings: dict, system: str, user: str, temperature: float, model: str | None = None) -> str:
    base = settings["ai_base_url"].rstrip("/")
    body = {
        "model": model or settings["ai_model"],
        "temperature": temperature,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
    }
    req = urllib.request.Request(
        f"{base}/chat/completions",
        data=json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {settings['ai_api_key']}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=90) as response:
            payload = json.loads(response.read().decode())
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:200]
        raise RuntimeError(f"模型接口返回 {exc.code}：{detail}") from exc
    return payload["choices"][0]["message"]["content"]


def generate_copy(topic: str, context: list[str], angle: str) -> list[dict]:
    """两步：写手出 5 条不同角度（高温）→ 编辑按清单打分、最小修改、留 3 条（低温）。"""
    settings = get_settings()
    if not settings["ai_api_key"]:
        return [{"text": text, "type": "模板", "score": None, "why": "未配置 AI，仅为占位模板"} for text in copy_variants(topic, angle, "")]
    material = "\n".join(f"- {item[:400]}" for item in context[:5]) or "（无，只有话题名）"
    user = f"热点：{topic}\n相关热帖素材：\n{material}\n账号主人的看法：{angle or '未提供，你来给出一个有依据的判断'}"
    draft = chat(settings, prompts.writer_prompt(settings["persona"], settings["samples"]), user, 0.9)
    candidates = [item for item in parse_json(draft).get("candidates", []) if isinstance(item, dict) and item.get("text")]
    if not candidates:
        return [{"text": text, "type": "", "score": None, "why": ""} for text in copy_variants(topic, angle, draft)]
    listing = "\n".join(f"{i + 1}. [{item.get('type', '')}] {item['text']}" for i, item in enumerate(candidates))
    try:
        reviewed = parse_json(chat(settings, prompts.editor_prompt(material, bool(settings["samples"].strip())), f"候选：\n{listing}", 0.2, editor_model(settings)))
        picks = [item for item in reviewed.get("variants", []) if isinstance(item, dict) and item.get("text")]
    except AI_ERRORS:
        picks = []  # 编辑这一步失败时退回写手的原稿
    results = []
    for item in picks or candidates:
        text = tidy(item["text"])
        if text and x_length(text) <= 280 and text not in {r["text"] for r in results}:
            results.append({"text": text, "type": str(item.get("type", "")), "score": item.get("score"), "why": str(item.get("why", ""))})
    return results[:3]


def daily_count(day) -> int:
    return sum(1 for item in occupied_times() if item.astimezone(TZ).date() == day)


# 中文社会热榜（均为免登录公开接口）。小红书热榜需要登录和签名，暂不接入。
CN_SOURCES = {"weibo": "微博热搜", "douyin": "抖音热榜", "zhihu": "知乎热榜"}
MOBILE_UA = "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 Mobile/15E148"
DESKTOP_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/130 Safari/537.36"
CN_CACHE: dict[str, tuple[datetime, list[dict]]] = {}


def get_json(url: str, ua: str, referer: str) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": ua, "Referer": referer})
    with urllib.request.urlopen(req, timeout=15) as response:
        return json.loads(response.read().decode("utf-8", "replace"))


def as_int(value) -> int:
    """热榜接口的热度字段类型不稳定，统一转成整数，转不出就当 0。"""
    if isinstance(value, bool):
        return 0
    if isinstance(value, (int, float)):
        return int(value)
    digits = re.sub(r"\D", "", str(value))
    return int(digits) if digits else 0


def parse_cn_hot(source: str, payload: dict) -> list[dict]:
    """统一成 {name, summary, heat, url}，并去掉广告位。"""
    data = payload.get("data") if isinstance(payload, dict) else None
    items = []
    if source == "weibo":
        entries = data.get("realtime") if isinstance(data, dict) else None
        for entry in entries or []:
            if not isinstance(entry, dict) or entry.get("is_ad") or not entry.get("word"):
                continue
            word = str(entry["word"])
            items.append({"name": word, "summary": str(entry.get("flag_desc") or ""), "heat": as_int(entry.get("num")),
                          "url": "https://s.weibo.com/weibo?q=" + urllib.parse.quote(f"#{word}#")})
    elif source == "douyin":
        entries = data.get("word_list") if isinstance(data, dict) else None
        for entry in entries or []:
            if not isinstance(entry, dict) or not entry.get("word"):
                continue
            word = str(entry["word"])
            items.append({"name": word, "summary": "", "heat": as_int(entry.get("hot_value")),
                          "url": "https://www.douyin.com/search/" + urllib.parse.quote(word)})
    elif source == "zhihu":
        for entry in data or []:
            if not isinstance(entry, dict):
                continue
            target = entry.get("target") if isinstance(entry.get("target"), dict) else {}
            if not target.get("title"):
                continue
            excerpt = target.get("excerpt")
            detail = str(entry.get("detail_text") or "")
            heat = as_int(detail)
            if "万" in detail:
                heat *= 10000
            items.append({"name": str(target["title"]), "summary": str(excerpt or "")[:300],
                          "heat": heat,
                          "url": f"https://www.zhihu.com/question/{target.get('id', '')}"})
    return items[:30]


def fetch_cn_hot(source: str) -> list[dict]:
    cached = CN_CACHE.get(source)
    if cached and now() - cached[0] < timedelta(minutes=10):
        return cached[1]
    if source == "weibo":
        payload = get_json("https://weibo.com/ajax/side/hotSearch", MOBILE_UA, "https://weibo.com/")
    elif source == "douyin":
        payload = get_json("https://www.douyin.com/aweme/v1/web/hot/search/list/?device_platform=webapp&aid=6383&channel=channel_pc_web&detail_list=1",
                           DESKTOP_UA, "https://www.douyin.com/")
    else:
        payload = get_json("https://api.zhihu.com/topstory/hot-lists/total?limit=30", DESKTOP_UA, "https://www.zhihu.com/")
    items = parse_cn_hot(source, payload)
    CN_CACHE[source] = (now(), items)
    return items


def quick_candidates(source: str, board: str) -> list[dict]:
    """一键模式的候选事件，统一为 {name, summary, heat, url, source, posts}。"""
    if source in CN_SOURCES:
        return [{**item, "source": CN_SOURCES[source], "posts": []} for item in fetch_cn_hot(source)]
    if source == "trends":
        return [{"name": t["name"], "summary": "", "heat": t["volume"], "url": f"https://x.com/search?q={urllib.parse.quote(t['query'])}",
                 "source": "X 全球热搜", "posts": []} for t in fetch_trends("")]
    if board not in BOARDS:
        raise ValueError("未知板块")
    result = fetch_hot(BOARDS[board][1])
    if not result["ok"]:
        raise RuntimeError(result["error"])
    return [{"name": item["text"].split("\n")[0][:60], "summary": item["text"][:400], "heat": item["score"], "url": item["url"],
             "source": f"X · {BOARDS[board][0]}板块", "posts": [item["text"]]} for item in result["items"]]


def event_material(event: dict) -> list[str]:
    """事实锚定用的素材：热榜摘要 + 已带的原帖 + 登录时 X 上的相关热帖。"""
    material = [f"热榜标题（{event.get('source', '')}）：{event['name']}"]
    if event.get("summary"):
        material.append(f"热榜摘要：{event['summary']}")
    material += [f"原帖：{text[:400]}" for text in event.get("posts") or []]
    if not event.get("posts") and account_state()["connected"]:
        result = run_agentx(["search", "--account", ACCOUNT, "--type", "Top", "-n", "8", f"{event['name'][:40]} -filter:replies"], timeout=45)
        if result.get("ok"):
            material += [f"X 热帖 @{item['author']}：{item['text'][:300]}" for item in rank_hot(topic_items(result))[:4]]
    return material


def numbered(material: list[str]) -> str:
    return "\n".join(f"[{i + 1}] {text}" for i, text in enumerate(material))


def pick_event(settings: dict, candidates: list[dict], skip: list[str]) -> dict:
    pool = [item for item in candidates if item["name"] not in skip and not SPAM.search(item["name"])][:12]
    if not pool:
        raise RuntimeError("这个榜单里能写的热点都换过了，换个来源试试")
    listing = "\n".join(f"{i + 1}. {item['name']}（热度 {item['heat']}）{(' — ' + item['summary'][:80]) if item['summary'] else ''}" for i, item in enumerate(pool))
    choice = parse_json(chat(settings, prompts.picker_prompt(settings["persona"]), f"候选热点：\n{listing}", 0.3, editor_model(settings)))
    index = choice.get("index")
    if not isinstance(index, int) or not 1 <= index <= len(pool):
        raise RuntimeError(f"当前榜单没有适合蹭的热点：{choice.get('why') or '模型没有给出选择'}")
    return {**pool[index - 1], "why": str(choice.get("why", "")), "emotion": str(choice.get("emotion", "")), "angle": str(choice.get("angle", ""))}


def quick_post(source: str, board: str, event: dict | None, skip: list[str]) -> dict:
    """一键：选事件 → 提炼事实并写 3 条 → 编辑只留 1 条。共 3 次模型调用（换写法时 2 次）。"""
    settings = get_settings()
    if not settings["ai_api_key"]:
        raise RuntimeError("一键生成需要 AI，请先到「后台设置」配置模型")
    if not event:
        event = pick_event(settings, quick_candidates(source, board), skip)
    material = event_material(event)
    writer = prompts.quick_prompt(settings["persona"], settings["samples"], event.get("emotion", ""), event.get("angle", ""))
    draft = parse_json(chat(settings, writer, f"热点：{event['name']}\n素材：\n{numbered(material)}", 0.9))
    facts = []
    for item in draft.get("facts") or []:
        if isinstance(item, dict) and item.get("fact"):
            index = item.get("source")
            facts.append({"fact": str(item["fact"]), "source": material[index - 1] if isinstance(index, int) and 1 <= index <= len(material) else ""})
    candidates = [item for item in draft.get("candidates") or [] if isinstance(item, dict) and item.get("text")]
    if not candidates:
        raise RuntimeError("模型没有返回可用的帖子，请再点一次")
    listing = "\n".join(f"{i + 1}. [{item.get('type', '')}] {item['text']}" for i, item in enumerate(candidates))
    try:
        reviewed = parse_json(chat(settings, prompts.editor_prompt(numbered(material), bool(settings["samples"].strip()), 1), f"候选：\n{listing}", 0.2, editor_model(settings)))
        picks = [item for item in reviewed.get("variants") or [] if isinstance(item, dict) and item.get("text")]
    except AI_ERRORS:
        picks = []
    for item in picks + candidates:
        text = tidy(item["text"])
        if text and x_length(text) <= 280:
            post = {"text": text, "type": str(item.get("type", "")), "score": item.get("score"), "why": str(item.get("why", ""))}
            break
    else:
        raise RuntimeError("生成的帖子都超过了 X 字数上限，请再点一次")
    public_event = {key: event.get(key, "") for key in ("name", "summary", "heat", "url", "source", "posts", "why", "emotion", "angle")}
    return {"post": post, "event": public_event, "facts": facts}


def revise_post(text: str, instruction: str, facts: list[str]) -> str:
    settings = get_settings()
    if not settings["ai_api_key"]:
        raise RuntimeError("AI 修改需要先在「后台设置」配置模型")
    listing = "\n".join(f"- {fact}" for fact in facts[:8])
    reply = chat(settings, prompts.revise_prompt(settings["persona"], listing), f"原帖：\n{text}\n\n修改要求：{instruction}", 0.7)
    revised = tidy(parse_json(reply).get("text") or reply)
    if not revised:
        raise RuntimeError("模型没有返回修改结果")
    return revised


def remix_post(text: str) -> str:
    """爆款复用：保留原帖的观点和事实，换一种说法，方便隔一段时间再发一次。"""
    return revise_post(text, "保留核心观点和所有事实，但换成完全不同的开头、叙事顺序和用词，让人感觉是一条新帖子而不是重复发。", [])


IMAGE_DIR = DATA / "images"
CARD_W, CARD_H = 1200, 675
CARD_COLORS = ((15, 20, 25), (255, 255, 255), (29, 155, 240))


def wrap_text(draw, text: str, font, width: int) -> list[str]:
    lines = []
    for paragraph in text.split("\n"):
        current = ""
        for char in paragraph:
            if draw.textlength(current + char, font=font) <= width:
                current += char
            else:
                lines.append(current)
                current = char
        lines.append(current)
    return lines or [""]


def card_font(size: int):
    """找一个能显示中文的字体。Windows 用系统字体，Linux 服务器上用随仓库附带的字体。"""
    from PIL import ImageFont

    candidates = [
        Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts" / "msyh.ttc",
        Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
        Path("/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc"),
        APP_DIR / "static" / "fonts" / "NotoSansSC-Regular.otf",
    ]
    for path in candidates:
        if path.exists():
            return ImageFont.truetype(str(path), size)
    return ImageFont.load_default()


def render_card(text: str) -> Path:
    """把金句渲染成深色图片，纯文字帖配图后互动通常更好。"""
    from PIL import Image, ImageDraw

    IMAGE_DIR.mkdir(exist_ok=True)
    image = Image.new("RGB", (CARD_W, CARD_H), CARD_COLORS[0])
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 0, 14, CARD_H), fill=CARD_COLORS[2])
    font = card_font(56)
    lines = wrap_text(draw, tidy(text)[:150], font, CARD_W - 200)[:8]
    total_h = len(lines) * 80
    y = max(80, (CARD_H - total_h) // 2)
    for line in lines:
        draw.text((90, y), line, font=font, fill=CARD_COLORS[1])
        y += 80
    path = IMAGE_DIR / f"card-{int(now().timestamp() * 1000)}.png"
    image.save(path)
    return path


def attach_image(draft_id: int, text: str) -> str:
    path = render_card(text)
    with closing(connect()) as conn:
        row = conn.execute("SELECT status FROM drafts WHERE id=?", (draft_id,)).fetchone()
        if row is None or row["status"] == "posted":
            raise RuntimeError("这条内容已发布或不存在，无法再配图")
        conn.execute("UPDATE drafts SET image=?, updated_at=? WHERE id=?", (str(path), now().isoformat(), draft_id))
        conn.commit()
    return str(path)


@app.get("/")
def index():
    return send_from_directory(APP_DIR / "static", "index.html")


@app.get("/settings")
def settings_page():
    return send_from_directory(APP_DIR / "static", "settings.html")


def masked(key: str) -> str:
    return f"{key[:3]}…{key[-4:]}" if len(key) > 10 else ("已设置" if key else "")


@app.get("/api/settings")
def read_settings():
    values = get_settings()
    values["ai_api_key"] = masked(values["ai_api_key"])  # 密钥不回传给页面
    return jsonify({"ok": True, "settings": values, "limits": LIMITS})


@app.post("/api/settings")
def write_settings():
    body = request.get_json(force=True)
    changes = {}
    for key in SETTING_DEFAULTS:
        if key not in body:
            continue
        value = str(body[key]).strip()
        if key == "ai_api_key" and not value:
            continue  # 留空表示不修改
        if key in LIMITS:
            low, high = LIMITS[key]
            if not value.isdigit() or not low <= int(value) <= high:
                return jsonify({"ok": False, "error": f"{key} 需在 {low}–{high} 之间"}), 400
        if key == "ai_base_url" and value and not value.startswith(("http://", "https://")):
            return jsonify({"ok": False, "error": "接口地址需以 http:// 或 https:// 开头"}), 400
        changes[key] = value
    save_settings(changes)
    return jsonify({"ok": True})


@app.post("/api/settings/test")
def test_model():
    settings = get_settings()
    if not settings["ai_api_key"]:
        return jsonify({"ok": False, "error": "请先填写并保存 API Key"}), 400
    try:
        reply = chat(settings, "只回复两个字：可以", "测试连通性", 0)
        if editor_model(settings) != settings["ai_model"]:
            chat(settings, "只回复两个字：可以", "测试连通性", 0, editor_model(settings))
            reply += "（写手、编辑模型均可用）"
    except AI_ERRORS as exc:
        return jsonify({"ok": False, "error": f"调用失败：{exc}"}), 502
    return jsonify({"ok": True, "reply": reply.strip()[:50]})


@app.get("/api/status")
def status():
    settings = get_settings()
    return jsonify({
        "account": account_state(),
        "aiEnabled": bool(settings["ai_api_key"]),
        "aiModel": settings["ai_model"],
        "dailyLimit": daily_limit(),
        "minGapHours": int(settings["min_gap_hours"]),
        "boards": {key: label for key, (label, _) in BOARDS.items()},
        "cnSources": CN_SOURCES,
        "todayCount": sum(1 for item in rows("posted") if item.get("posted_at") and datetime.fromisoformat(item["posted_at"]).astimezone(TZ).date() == now().date()),
        "nextSlot": when_text(suggest_time().isoformat()),
        "bestHours": get_settings()["best_hours"],
        "drafts": [present(item) for item in rows()],
    })


@app.post("/api/account")
def add_account():
    body = request.get_json(force=True)
    username = str(body.get("username", "")).strip().lstrip("@")
    password = str(body.get("password", ""))
    if username or password:
        if not username or not password:
            return jsonify({"ok": False, "error": "请同时填写用户名和密码"}), 400
        # 密码只经环境变量传给 AgentX，不进命令行参数、不落库
        result = run_agentx(
            ["account", "login", "--name", ACCOUNT, "--username", username],
            timeout=90,
            extra_env={"TWITTER_PASSWORD": password},
        )
    else:
        token = str(body.get("authToken", "")).strip()
        ct0 = str(body.get("ct0", "")).strip()
        if not token or not ct0:
            return jsonify({"ok": False, "error": "请填写 auth_token 和 ct0"}), 400
        result = run_agentx(["account", "add", "--name", ACCOUNT, "--auth-token", token, "--ct0", ct0])
    if not result.get("ok"):
        return jsonify({"ok": False, "error": friendly_error(result)}), 400
    checked = verify_login()
    return jsonify(checked), (200 if checked["ok"] else 400)


@app.post("/api/logout")
def logout():
    run_agentx(["account", "remove", ACCOUNT])
    return jsonify({"ok": True})


@app.get("/api/trends")
def trends():
    region = request.args.get("region", "")
    if region not in REGIONS:
        region = ""
    try:
        return jsonify({"ok": True, "items": fetch_trends(region), "regions": REGIONS})
    except (urllib.error.URLError, TimeoutError) as exc:
        return jsonify({"ok": False, "error": f"热搜榜获取失败，请检查网络：{exc}"}), 502


@app.get("/api/hot")
def hot():
    board = request.args.get("board", "")
    keywords = BOARDS[board][1] if board in BOARDS else " OR ".join(request.args.get("keywords", "").split())
    if not keywords:
        return jsonify({"ok": False, "error": "请填写关键词"}), 400
    if not account_state()["connected"]:
        return jsonify({"ok": False, "needLogin": True, "error": "中文板块热点要用你的 X 账号搜索，请先登录。登录前可以先看「全球热搜」。"}), 400
    result = fetch_hot(keywords)
    return jsonify(result), (200 if result["ok"] else 502)


@app.get("/api/topic")
def topic():
    query = request.args.get("q", "").strip()
    if not query:
        return jsonify({"ok": False, "error": "缺少话题"}), 400
    if not account_state()["connected"]:
        return jsonify({"ok": True, "items": [], "note": "登录后可查看这个话题下的热门帖子作为素材"})
    result = run_agentx(["search", "--account", ACCOUNT, "--type", "Top", "-n", "8", f"{query} -filter:replies"], timeout=45)
    if not result.get("ok"):
        return jsonify({"ok": True, "items": [], "note": friendly_error(result)})
    return jsonify({"ok": True, "items": topic_items(result)[:6]})


@app.post("/api/copy")
def copy():
    body = request.get_json(force=True)
    topic_name = str(body.get("topic", "")).strip()
    context = [str(item) for item in body.get("context") or []]
    angle = str(body.get("angle", "")).strip()
    if not topic_name:
        return jsonify({"ok": False, "error": "请先选择一个热点"}), 400
    try:
        return jsonify({"ok": True, "variants": generate_copy(topic_name, context, angle)})
    except AI_ERRORS as exc:
        return jsonify({"ok": False, "error": f"文案生成失败：{exc}"}), 502


@app.post("/api/quick")
def quick():
    body = request.get_json(force=True)
    source = str(body.get("source", ""))
    board = str(body.get("board", ""))
    if source not in {*CN_SOURCES, "trends", "board"}:
        return jsonify({"ok": False, "error": "未知的热点来源"}), 400
    if source == "board" and not account_state()["connected"]:
        return jsonify({"ok": False, "needLogin": True, "error": "X 板块热点需要先登录。可以先用微博/抖音/知乎热榜。"}), 400
    event = body.get("event")
    if event is not None:
        # 「换个写法」：沿用上次选中的事件，跳过选题这一步
        if not isinstance(event, dict) or not str(event.get("name", "")).strip():
            return jsonify({"ok": False, "error": "事件信息不完整"}), 400
        event = {key: event.get(key, "") for key in ("name", "summary", "heat", "url", "source", "why", "emotion", "angle")} | {
            "name": str(event["name"])[:200], "summary": str(event.get("summary", ""))[:400],
            "posts": [str(text)[:400] for text in event.get("posts") or []][:3]}
    skip = [str(name) for name in body.get("skip") or []][:50]
    try:
        return jsonify({"ok": True, **quick_post(source, board, event, skip)})
    except AI_ERRORS as exc:
        return jsonify({"ok": False, "error": f"生成失败：{exc}"}), 502


@app.post("/api/revise")
def revise():
    body = request.get_json(force=True)
    text = str(body.get("text", "")).strip()
    preset = str(body.get("preset", ""))
    instruction = prompts.REVISE_PRESETS.get(preset) or str(body.get("instruction", "")).strip()[:300]
    if not text or not instruction:
        return jsonify({"ok": False, "error": "需要原文和修改要求"}), 400
    facts = [str(item)[:300] for item in body.get("facts") or []]
    try:
        revised = revise_post(text, instruction, facts)
    except AI_ERRORS as exc:
        return jsonify({"ok": False, "error": f"修改失败：{exc}"}), 502
    return jsonify({"ok": True, "text": revised, "length": x_length(revised)})


@app.post("/api/remix")
def remix():
    body = request.get_json(force=True)
    text = str(body.get("text", "")).strip()
    if not text:
        return jsonify({"ok": False, "error": "缺少原文"}), 400
    try:
        return jsonify({"ok": True, "text": remix_post(text)})
    except AI_ERRORS as exc:
        return jsonify({"ok": False, "error": f"改写失败：{exc}"}), 502


@app.post("/api/drafts/<int:draft_id>/card")
def card(draft_id: int):
    with closing(connect()) as conn:
        row = conn.execute("SELECT text FROM drafts WHERE id=?", (draft_id,)).fetchone()
    if row is None:
        return jsonify({"ok": False, "error": "找不到这条内容"}), 404
    try:
        return jsonify({"ok": True, "image": attach_image(draft_id, row["text"])})
    except (RuntimeError, OSError) as exc:
        return jsonify({"ok": False, "error": f"生成配图失败：{exc}"}), 500


@app.get("/api/saved")
def list_saved():
    with closing(connect()) as conn:
        items = [dict(row) for row in conn.execute("SELECT * FROM saved ORDER BY id DESC LIMIT 100")]
    return jsonify({"ok": True, "items": items})


@app.post("/api/saved")
def create_saved():
    body = request.get_json(force=True)
    text = str(body.get("text", "")).strip()
    if not text:
        return jsonify({"ok": False, "error": "内容不能为空"}), 400
    with closing(connect()) as conn:
        conn.execute(
            "INSERT INTO saved (text, note, source_url, created_at) VALUES (?, ?, ?, ?)",
            (text[:2000], str(body.get("note", "")).strip()[:200], str(body.get("sourceUrl", "")).strip()[:300], now().isoformat()),
        )
        conn.commit()
    return jsonify({"ok": True})


@app.delete("/api/saved/<int:saved_id>")
def delete_saved(saved_id: int):
    with closing(connect()) as conn:
        conn.execute("DELETE FROM saved WHERE id=?", (saved_id,))
        conn.commit()
    return jsonify({"ok": True})


@app.post("/api/samples")
def add_sample():
    """把一条自己的帖子追加进写作样本，之后的 AI 写作会模仿它。"""
    text = str((request.get_json(force=True) or {}).get("text", "")).strip()
    if not text:
        return jsonify({"ok": False, "error": "内容不能为空"}), 400
    samples = get_settings()["samples"].strip()
    if text in samples:
        return jsonify({"ok": True, "note": "这条已经在样本里了"})
    save_settings({"samples": (samples + "\n\n" + text).strip()[:4000]})
    return jsonify({"ok": True})


def parse_time(raw: str) -> datetime | None:
    if not raw:
        return None
    value = datetime.fromisoformat(raw)
    return value if value.tzinfo else value.replace(tzinfo=TZ)


@app.post("/api/drafts")
def create_draft():
    body = request.get_json(force=True)
    text = str(body.get("text", "")).strip()
    as_thread = bool(body.get("thread"))
    if not text:
        return jsonify({"ok": False, "error": "内容不能为空"}), 400
    try:
        parts = thread_parts(text) if as_thread else [text]
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    if not as_thread and x_length(text) > 280:
        return jsonify({"ok": False, "error": "超过 X 的 280 字符上限（中文约 140 字），请精简，或勾选「作为线程发送」按空行分段"}), 400
    if text in {item["text"] for item in rows() if item["status"] in {"queued", "posted"}}:
        return jsonify({"ok": False, "error": "这条内容已经在队列里或发过了"}), 409
    try:
        scheduled = suggest_time(parse_time(str(body.get("scheduledAt", "")).strip()))
    except (RuntimeError, ValueError) as exc:
        return jsonify({"ok": False, "error": f"安排发布时间失败：{exc}"}), 409
    current = now().isoformat()
    with closing(connect()) as conn:
        cursor = conn.execute(
            "INSERT INTO drafts (text, scheduled_at, status, created_at, updated_at, thread) VALUES (?, ?, 'queued', ?, ?, ?)",
            (text, scheduled.isoformat(), current, current, 1 if as_thread else 0),
        )
        conn.commit()
    return jsonify({"ok": True, "id": cursor.lastrowid, "whenText": when_text(scheduled.isoformat()), "parts": len(parts)})


HARD_GAP = timedelta(minutes=60)  # 立即发送也要遵守的最小间隔


def reschedule_queue() -> None:
    """立即发送后，把和它挤在一起的待发帖顺延，保证仍满足每日上限和间隔。"""
    with closing(connect()) as conn:
        for item in rows("queued"):
            slot = suggest_time(datetime.fromisoformat(item["scheduled_at"]), exclude=item["id"])
            if slot.isoformat() != item["scheduled_at"]:
                conn.execute("UPDATE drafts SET scheduled_at=?, updated_at=? WHERE id=?", (slot.isoformat(), now().isoformat(), item["id"]))
                conn.commit()


@app.post("/api/drafts/<int:draft_id>/now")
def post_now(draft_id: int):
    with closing(connect()) as conn:
        row = conn.execute("SELECT * FROM drafts WHERE id=?", (draft_id,)).fetchone()
    if row is None or row["status"] not in {"queued", "failed", "expired"}:
        return jsonify({"ok": False, "error": "这条不能发送"}), 409
    if not account_state()["connected"]:
        return jsonify({"ok": False, "error": "还没登录 X"}), 400
    posted = [datetime.fromisoformat(item["posted_at"]) for item in rows("posted") if item["posted_at"]]
    if sum(1 for item in posted if item.astimezone(TZ).date() == now().date()) >= daily_limit():
        return jsonify({"ok": False, "error": f"今天已经发了 {daily_limit()} 条，达到上限。可以在后台调整，或让它排到明天。"}), 409
    recent = [item for item in posted if now() - item < HARD_GAP]
    if recent:
        wait = int((HARD_GAP - (now() - max(recent))).total_seconds() // 60) + 1
        return jsonify({"ok": False, "error": f"距上一条太近，为降低风控请 {wait} 分钟后再发"}), 409
    if not LOCK.acquire(timeout=5):
        return jsonify({"ok": False, "error": "后台正在发布另一条，请稍后再试"}), 409
    try:
        result = post_one(dict(row))
    finally:
        LOCK.release()
    if result["ok"]:
        reschedule_queue()
    return jsonify(result), (200 if result["ok"] else 502)


@app.post("/api/drafts/<int:draft_id>/retry")
def retry(draft_id: int):
    with closing(connect()) as conn:
        row = conn.execute("SELECT * FROM drafts WHERE id=?", (draft_id,)).fetchone()
        if row is None or row["status"] not in {"failed", "expired"}:
            return jsonify({"ok": False, "error": "这条不能重新排期"}), 409
        scheduled = suggest_time(exclude=draft_id)
        conn.execute(
            "UPDATE drafts SET status='queued', scheduled_at=?, updated_at=?, error=NULL WHERE id=?",
            (scheduled.isoformat(), now().isoformat(), draft_id),
        )
        conn.commit()
    return jsonify({"ok": True, "whenText": when_text(scheduled.isoformat())})


@app.delete("/api/drafts/<int:draft_id>")
def delete_draft(draft_id: int):
    with closing(connect()) as conn:
        conn.execute("DELETE FROM drafts WHERE id=? AND status!='posted'", (draft_id,))
        conn.commit()
    return jsonify({"ok": True})


def main() -> None:
    init_db()
    threading.Thread(target=scheduler_loop, args=(threading.Event(),), daemon=True).start()
    app.run(host="127.0.0.1", port=8787, debug=False)


if __name__ == "__main__":
    main()
