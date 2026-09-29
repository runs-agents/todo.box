# -*- coding: utf-8 -*-
"""
box_config.py — 待办箱工具族配置单源（B0 · 2026-9-12）

【本文件职责】待办箱写入器/整理器/扫描器的**唯一配置来源**。
任何路径、白名单、阈值、正则只在这里定义一次——防止两个脚本各写一份而漂移。

【修改纪律】改动走「维护者出方案 → 所有者拍板」。新增配置项用 append，不重排已有行。

【设计依据】《待办箱写入器与整理器方案 v4》§5.1（box_config 规格）
"""
from __future__ import annotations

import os
import re
import sys

# ★ 修复 V2-4（外审 2026-09-29 终验）：管道/重定向输出（cron、CI、> file）在中文
#   Windows 走 GBK 编码，emoji（✅/❌）直接 UnicodeEncodeError 崩溃（实测复现：
#   PowerShell 捕获输出即崩在第一个 check）。交互控制台（isatty）不受影响——
#   Python 3.6+ 的 Windows 控制台本就走 UTF-8 通道。所有工具都先 import 本模块，
#   在此收口一次。（顺带摘除 sys_path_note 死变量——外审汇总风格杂音清单项）
for _s in (sys.stdout, sys.stderr):
    try:
        if not _s.isatty() and _s.encoding and _s.encoding.lower().replace("-", "") != "utf8":
            _s.reconfigure(encoding="utf-8")
    except Exception:
        pass

# ══════════════════════════════════════════════════════
# 路径
# ══════════════════════════════════════════════════════
HOME = os.environ.get("TODOBOX_HOME") or os.path.dirname(os.path.abspath(__file__))

# ══ 双路径 schema ════
# BOX=真源（箱子本体）；HOME=工具与派生件住址。均可用环境变量覆盖：
# TODOBOX_HOME / TODOBOX_BOX / TODOBOX_LEGACY_BOX。
BOX = os.environ.get("TODOBOX_BOX") or os.path.join(HOME, "box")
# 旧箱（可选，只读封存）：迁移工具只准读它。任何写入 = 违规。
LEGACY_BOX = os.environ.get("TODOBOX_LEGACY_BOX") or ""

#: 五个数据分区（正式待办的家）——缺省中性命名；可用 TODOBOX_ZONES 覆盖
#: （逗号分隔，恰好 5 项，按序对应 01-05）。分区名是纯标签，语义由你赋予。
ZONES = dict(
    (k, os.path.join(BOX, v)) for k, v in zip(
        ("01", "02", "03", "04", "05"),
        (os.environ.get("TODOBOX_ZONES")
         or "01_工作,02_学习,03_生活,04_参考,05_杂项").split(",")))
#: 旧箱五区（迁移映射用；只在 migrate_v2 的读侧出现）
LEGACY_ZONES = {
    "01": os.path.join(LEGACY_BOX, "01_语音区"),
    "02": os.path.join(LEGACY_BOX, "02_表情包区"),
    "03": os.path.join(LEGACY_BOX, "03_文档区"),
    "04": os.path.join(LEGACY_BOX, "04_数据区"),
    "05": os.path.join(LEGACY_BOX, "05_杂项区"),
}
#: 旧区名 → 新分区号（一对一，零翻译）
LEGACY_ZONE_TO_NEW = {
    "01_语音区": "01", "02_表情包区": "02", "03_文档区": "03",
    "04_数据区": "04", "05_杂项区": "05", "00_收件箱": "00",
}

#: 收件箱（不确定放哪的先扔这里）
INBOX = os.path.join(BOX, "00_收件箱")
INBOX_DONE = os.path.join(INBOX, "_已处理")          # 收件箱内的已处理（旧协议遗留，整理器沿用）

#: 完成账按月归档（DONE_ 文件的家）
DONE_DIR = os.path.join(BOX, "_done")
#: tid 注册表（随箱子走）
REGISTRY_DIR = os.path.join(BOX, "_registry")
REGISTRY_JSON = os.path.join(REGISTRY_DIR, "todo_id_registry.json")
#: 搬迁对账报告
MIGRATION_REPORT_JSON = os.path.join(REGISTRY_DIR, "migration_report.json")

#: 派生/日志/中间态
INDEX_MD = os.path.join(BOX, "_索引.md")
LEDGER_MD = os.path.join(BOX, "_总账本.md")
CHANGELOG_MD = os.path.join(BOX, "_变更日志.md")
PENDING_FIX_MD = os.path.join(BOX, "_待确认修复.md")
PENDING_WRITE_MD = os.path.join(BOX, "_待写清单.md")
RULES_TXT = os.path.join(BOX, "_待办箱规矩.txt")
#: 旧箱同名（只读封存期参照；工具不写）
LEGACY_INDEX_MD = os.path.join(LEGACY_BOX, "_索引.md")
LEGACY_PENDING_WRITE_MD = os.path.join(LEGACY_BOX, "_待写清单.md")

#: 快照（**永不进同步盘**——方案 🟡-7 写死）
#: ★ 单5（2026-9-17）：快照域切新家箱内 `_snapshots\`（随箱子走，独立区不进扫描范围）。
SNAPSHOT_DIR = os.path.join(BOX, "_snapshots")
#: 旧快照域（只读参照；新家快照从零开始，旧箱快照随旧箱封存）
LEGACY_SNAPSHOT_DIR = os.path.join(HOME, "todo_snapshots")

#: 锁（三工具共用——方案 🔴-1）
LOCK_FILE = os.path.join(HOME, "todo_box.lock")
LEGACY_LOCK_FILE = os.path.join(HOME, "todo_box_index.lock")

#: tmp 目录（同卷保证 os.replace 原子性；不进同步盘）
TMP_DIR = HOME

# ══════════════════════════════════════════════════════
# 文件名协议（2026-9-17 完全体搬迁 · 单1）
# ══════════════════════════════════════════════════════
#: 文件名 = 状态_日期_tid_节奏_标题.txt
#:   OPEN_20260917_T00002e_盯办_销账哨v1.1重审修补.txt
#: 解析规则（v1.1 §1.2）：从左取前四段，余下**全部**归标题（标题含 `_` 不丢字）
#: ⚠ 用具名分组——技能文档与断言都按「状态/日期/tid/节奏/标题」这套段名核对，
#:   改成正则里的 group index 一挪就错（具名组让段序自证，防漂移）。
FN_RE = re.compile(
    r"^(?P<state>OPEN|DONE)_(?P<date>\d{8})_(?P<tid>T[0-9a-f]{6})_"
    r"(?P<rhythm>[^_]+)_(?P<title>.+)\.txt$")
FN_STATES = ("OPEN", "DONE")
#: 状态 → 索引 state
FN_STATE_TO_STATE = {"OPEN": "todo", "DONE": "done"}

#: 节奏枚举（文件名字段 + 索引侧状态）——可用 TODOBOX_RHYTHMS 覆盖（逗号分隔，恰好 5 项）。
#: 语义：第 3 项=等人拍板（干没干完判定权在所有者），第 5 项=永不催类。
RHYTHMS = tuple(
    (os.environ.get("TODOBOX_RHYTHMS") or "盯办,现在就做,等人,长线,永不催").split(","))
R_WATCH, R_DO_NOW, R_AWAIT, R_LONG, R_NEVER = RHYTHMS
#: 默认节奏跟枚举走（盲审 P1-1：硬编码中文会让非中文枚举下「不带 --cls」
#: 的最常用写入路径静默降级收件箱——默认值必须是枚举成员）
RHYTHM_DEFAULT = R_DO_NOW
RHYTHM_INBOX = "待整理"        # 收件箱降级时的节奏字段
#: 永不催的节奏（L3 铁律）
RHYTHM_NEVER = (R_NEVER,)

#: 标题上限（**截断优先**；FN_MAX_CHARS 仅作清洗兜底，不与标题上限打架）
TITLE_MAX = 30

#: Windows 非法字符 → 全角替换（保留可读性：肉眼仍看得出原意）
FULLWIDTH_REPLACEMENT = {
    "\\": "＼", "/": "／", ":": "：", "*": "＊",
    "?": "？", '"': "”", "<": "＜", ">": "＞", "|": "｜",
}
#: Windows 保留设备名（大小写无关；含扩展名形态也要拦，故比对主名）
WINDOWS_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}
#: 保留前缀（标题清洗后不得以这些开头——否则文件名被误解析/被扫描器跳掉）
RESERVED_TITLE_PREFIXES = ("OPEN_", "DONE_", "_done", "_registry", "_snapshots", "00_收件箱")


def clean_title(s: str) -> str:
    """标题清洗（写侧唯一入口）：非法字符→全角、截断≤30、保留名/保留前缀加 x_。

    ⚠ 顺序有意为之：先清字符 → 再截断 → 最后判保留名/前缀。
    反过来的话，截断会切掉 x_ 前缀或把 x_CON 截成 x_CO。
    """
    s = " ".join(str(s).split()).strip()
    s = s.strip(".")                       # Windows 不许结尾点
    for ch, rep in FULLWIDTH_REPLACEMENT.items():
        s = s.replace(ch, rep)
    s = s[:TITLE_MAX]                      # 截断优先（总长 60 只作兜底）
    s = s.strip().strip(".")
    if not s:
        return "未命名"
    head = s.split(".")[0].upper()
    if head in WINDOWS_RESERVED:
        s = "x_" + s
    elif s.startswith(RESERVED_TITLE_PREFIXES):
        s = "x_" + s
    return s


def build_filename(state: str, date_str: str, tid: str, rhythm: str, title: str) -> str:
    """按协议造文件名（标题必须已过 clean_title）。"""
    if state not in FN_STATES:
        raise ValueError(f"未知状态前缀: {state}（可选 {FN_STATES}）")
    if not re.fullmatch(r"T[0-9a-f]{6}", tid or ""):
        raise ValueError(f"tid 形态非法（须 T+6位十六进制）: {tid!r}")
    if rhythm not in RHYTHMS + (RHYTHM_INBOX,):
        raise ValueError(f"未知节奏: {rhythm!r}")
    fn = f"{state}_{date_str}_{tid}_{rhythm}_{clean_title(title)}.txt"
    if len(fn) > FN_MAX_CHARS:             # 兜底：只可能因日期/tid/节奏超长而触发
        fn = f"{state}_{date_str}_{tid}_{rhythm}_{clean_title(title)[:TITLE_MAX]}.txt"
    return fn


def parse_filename(fn: str) -> dict | None:
    """解析协议文件名 → dict；不合协议返回 None（调用方须标「待整理」，不静默）。"""
    m = FN_RE.match(str(fn))
    if not m:
        return None
    state, date_str, tid, rhythm, title = m.groups()
    return {
        "state": FN_STATE_TO_STATE[state],
        "prefix": state,
        "date": date_str,
        "tid": tid,
        "rhythm": rhythm,
        "title": title,
        "file": fn,
    }


# ══════════════════════════════════════════════════════
# 扫描器 SKIP（哪些不进索引）
# ══════════════════════════════════════════════════════
SKIP_NAMES = {
    "_索引.md", "_已完成归档",
    "_变更日志.md",
    # ★ 2026-9-17 完全体搬迁（单1）：新家骨架目录全部进 SKIP——
    #   它们是箱子结构，不是账。漏掉任何一个都会被扫描器当账枚举。
    "_registry", "_snapshots", "_done", "_看板.html",
    # ★ FIX-5（A包 C1 最小版）：`_待确认修复.md` / `_待写清单.md` 移出 SKIP —
    #   它们被自己的扫描器屏蔽 + 全系统无消费者 = 死信箱（文件在箱子，活儿对系统不可见）。
    #   本轮不做消费方案（等所有者拍板①②），只做**最小露出**：让它们在索引里被人眼看到。
    #   它们是下划线开头的清单文本（kind=制度），天然不会进真待办。
    # ⚠ 目录名维度（os.walk 比对用）——_已处理 是全树唯一的，直接写目录名
    "_已处理", "_快照", "todo_snapshots",
    # ★ 2026-9-13 丙方案：资料与账物理分离——_资料\ 住非账本文档（方案/研究/报告），
    #   扫描器不再读它们的内容（这是「文档正文被当账本读」的根治）。
    "_资料",
}
SKIP_SUFFIXES = (".tmp",)

# ══════════════════════════════════════════════════════
# 格式规范
# ══════════════════════════════════════════════════════
#: 待办条目格式：勾选框在前、分类标记在后
#:   - [ ] [现在就做] 内容
#:   - [x] 内容            （完成后去掉分类标记也可）
TODO_PREFIX = "- [ ] "
DONE_MARKERS = ("[x]", "[X]", "✅")

#: 分类白名单（写入器用它校验；非白名单 → 落收件箱 + 日志）
CLASSES = RHYTHMS + ("参考", "约定", "前置", "子步骤", "完成")

#: 写入器「降级」的目标分类（方案审查 W5 修正：写规范化值，不走别名）
DEFAULT_CLASS = "现在就做"

#: 收件箱前缀（整理器按此分流）
INBOX_PREFIXES = ("[待办]", "[完成]", "[事件]", "[笔记]")

# ══════════════════════════════════════════════════════
# 提醒节流（2026-9-13 · 所有者设计 + 千问评估修正）
# ══════════════════════════════════════════════════════
#: 各分类的**提醒节奏**（天）——距上次提醒满这么多天才再提。
#:
#: 来源：所有者 2026-9-12 设计（盯办每天/待办三天/长线十天）。
#: ⚠ 千问评估指出「轴挂错了」：盯办里实际装着「卡在外部、所有者无能为力」的事
#:   （如「等外部额度续费」），每天提 = 每天提醒一件所有者做不了的事。
#:   但**节奏表本身是可调的**——先按所有者原设计上线，用观察期数据再校准。
#:   改这里即全链生效（单一真源）。
NOTIFY_CADENCE = {
    R_WATCH: 1,
    R_DO_NOW: 3,
    R_AWAIT: 3,      # 卡在所有者身上 → 其实该高频；先按 3 天观察
    R_LONG: 10,
    R_NEVER: 30,      # 永不催类（30 天只是兜底，实际不推送）
}
NOTIFY_CADENCE_DEFAULT = 3

#: 「停滞」阈值（天）——超过这个天数没动过的账，单独列一节。
#: 千问建议的最小 cadence：「仅在摘要里加一条 N 笔超过 X 天未动」。
STALE_DAYS = 14

#: 提醒时间戳状态（**独立文件、箱外、看板脚本独占**）
#: 为什么不用现成的 todo_box_state.json：
#:   扫描器写它是**整体替换**（`_save_state(dict(...))`）→ 会把别人的键抹掉。
#:   独立文件零耦合；丢了最坏＝提醒重来一轮（可接受降级，不伤真源）。
NOTIFY_STATE = os.path.join(HOME, "todo_notify_state.json")
#: 开源参数化新增键（2026-9-29）：外部对账/日志源，空=该路强信号跳过
INDEX_JSON = os.path.join(HOME, "todo_box_index.json")
SKILLS_DIR = os.environ.get("TODOBOX_SKILLS_DIR") or ""   # 对账强信号①（技能库）
STATE_DB = os.environ.get("TODOBOX_STATE_DB") or ""       # 对账强信号③（会话库，sqlite 只读）
BACKUP_LOG = os.environ.get("TODOBOX_BACKUP_LOG") or ""   # 周报消费的备份日志

#: 看板（人读视图，可放同步盘随手机查看）
DASHBOARD_HTML = os.path.join(BOX, "_看板.html")
DASHBOARD_SIG = os.path.join(HOME, "todo_box_dashboard.sig")

# ══════════════════════════════════════════════════════
# 格式巡检白名单（方案 🟡-3：**显式枚举**，不用 glob、不用「含标记」动态口子）
# ══════════════════════════════════════════════════════
#: 整理器允许自动修格式的文件（相对 BOX 的路径）。
#: 开源版缺省空：原白名单指向行协议时代的旧账本（v4 文件名协议后这些
#: 文件已不存在，format_patrol 空转——盲审 P2-4）。要启用自动修格式，
#: 在这里显式列出你自己的账本文件。
TIDY_WHITELIST = ()
#: 注：新增文件入白名单需所有者确认（方案 🟡-3）

# ══════════════════════════════════════════════════════
# 阈值
# ══════════════════════════════════════════════════════
#: 锁僵尸超时（秒）——与扫描器一致
LOCK_STALE_SECONDS = 600
#: 锁心跳间隔（秒）——长任务每 N 秒 touch 刷新 mtime
LOCK_HEARTBEAT_SECONDS = 60
#: 抢锁重试
LOCK_RETRY_TIMES = 3
LOCK_RETRY_INTERVAL = 2.0

#: 状态修复熔断阈值（方案 🟡-3）
#: 依据：取日自然增量上限的 5–10 倍，表征「正常日积攒不出这个量」
FIX_FUSE_THRESHOLD = 50

#: 写后回读重试（方案 🔴-3）
READBACK_RETRY_TIMES = 3
READBACK_RETRY_INTERVAL = 0.5
#: 写前备份重试（方案 🟡-4——对齐回读策略）
BACKUP_RETRY_TIMES = 3

#: 整树快照保留份数 / 超此体积降级为 manifest 模式
TREE_SNAPSHOT_KEEP = 9
TREE_SNAPSHOT_MAX_MB = 500

#: 变更日志归档阈值（行）
CHANGELOG_ARCHIVE_LINES = 500

# ══════════════════════════════════════════════════════
# 快照范围（方案 🟡-2：仅文本扩展名——工具事故半径本就只有文本）
# ══════════════════════════════════════════════════════
TEXT_EXTS = (".txt", ".md")

#: 可执行扩展名（巡检报警用——方案新增，响应所有者「手滑点到」关切）
EXECUTABLE_EXTS = (".bat", ".cmd", ".exe", ".ps1", ".vbs", ".lnk", ".js", ".jar")

# ══════════════════════════════════════════════════════
# 文件名清洗
# ══════════════════════════════════════════════════════
#: Windows 非法字符 → 替换为 _
ILLEGAL_FN_CHARS = '<>:"/\\|?*'
FN_REPLACEMENT = "_"
FN_MAX_CHARS = 60          # 收件箱自动文件名的最长字符数（按字符，不按字节）

# ══════════════════════════════════════════════════════
# OneDrive 冲突副本检测模式（方案 🟡-10）
# ══════════════════════════════════════════════════════
#: OneDrive 冲突副本的典型命名（**宁窄勿宽**——2026-9-12 实测
#: `_审查_kimi-k3_raw_reasoning.md` 被模式0误报，因 `-k3` 匹配了「-设备名」形态）
CONFLICT_PATTERNS = (
    r"\(.*冲突副本.*\)",                        # 中文冲突副本（最可靠）
    r"\(.*的副本.*\)",                          # 英文版：xxx (my laptop's conflicted copy)
    r"-conflict-[A-Za-z0-9-]+\.(txt|md)$",      # -conflict-XXX 后缀
    r" \([0-9]+\)\.(txt|md)$",                  # 同名带序号（OneDrive 常见）
)

# ══════════════════════════════════════════════════════
# 整理器运行状态与周报（B2 · 2026-9-12）
# ══════════════════════════════════════════════════════
#: 整理器状态文件（心跳时间戳 last_run + 近期运行记录；周报数据源）
TIDY_STATE_JSON = os.path.join(HOME, "box_tidy_state.json")
#: 整理器运行日志（append-only：一行一次运行；供审计）
TIDY_RUNLOG = os.path.join(HOME, "box_tidy_run.log")
#: 周报落盘固定文件（≤5 行；周一由 cron 投递）
WEEKLY_REPORT_MD = os.path.join(HOME, "todo_tidy_weekly.md")
#: 变更日志归档目录（箱外；超行数时归档）
CHANGELOG_ARCHIVE_DIR = os.path.join(HOME, "todo_changelog_archive")
#: 心跳消费数据源：Hermes cron 执行库（周报「各 cron 执行情况」用）
# TODOBOX_CRON_DB（缺省空=跳过该统计——宁漏勿假，与 SKILLS_DIR/STATE_DB 同款守卫；
# 原硬编码 ~/AppData/Local/hermes 路径是家用残留，开源缺省不再指向它）
CRON_DB = os.environ.get("TODOBOX_CRON_DB") or ""
HERMES_CRON_JOBS = os.environ.get("TODOBOX_CRON_JOBS") or ""

if __name__ == "__main__":
    # 自检：路径存在性
    missing = []
    for name in ("HOME", "BOX", "INBOX", "SNAPSHOT_DIR"):
        pass
    checks = [
        ("HOME", HOME), ("BOX", BOX),
        ("_总账本.md", LEDGER_MD), ("_索引.md", INDEX_MD),
    ]
    print("=== box_config 自检 ===")
    for label, p in checks:
        exists = os.path.exists(p)
        print(f"  [{'OK ' if exists else 'MISS'}] {label:16} {p}")
        if not exists:
            missing.append(label)
    # 待建目录（不存在是正常的，B1/B2 会创建）
    for label, p in (("INBOX", INBOX), ("SNAPSHOT_DIR", SNAPSHOT_DIR)):
        print(f"  [{'OK ' if os.path.exists(p) else '待建'}] {label:16} {p}")
    print(f"\n分区数: {len(ZONES)} | 分类白名单: {len(CLASSES)} | 巡检白名单: {len(TIDY_WHITELIST)}")
    if missing:
        raise SystemExit(f"缺失关键路径: {missing}")
    print("✅ 配置自检通过")


# ══════════════════════════════════════════════════════
# 销账哨（2026-9-16 所有者拍板装 · 两层架构）
# ══════════════════════════════════════════════════════
# 架构：①上岗层=箱内自述正则（进索引+晨报出声）
#       ②影子层=会话证据判据（settle_shadow.py 每日跑，
#         只写 todo_settle_shadow.json 不出声，攒留出集数据，
#         消融证明有增量后才转正——deepseek 重审消融结论）
# 三家审查：_审查_销账哨_v1_*.md / _重审_销账哨_v1.1_*.md

#: 上岗层——条目标题/正文自述完成语义（deepseek W5：7/28 笔正文自称完成，
#: 零外部依赖零误报成本；须过否定窗，回测实证"已就绪未接"必须拦住）
import re as _re  # noqa: E402
SETTLE_IN_TITLE_DONE = _re.compile(
    r"已落地|已初判|已入|建成|已修|已销账|已上线|已完成|全绿|已定稿|已就绪|已定档|\[事件\]")

#: 否定窗（回测三轮迭代定型；"已定档，待XX后接入"必须拦）
SETTLE_NEG_RE = _re.compile(
    r"(没|未|还没|不能|无法|何时|什么时候|吗|呢|？|\?|明天|下周|计划|待做|之后|准备|待[^，。；]*后|等[^，。；]{0,6}再)")

#: 将来时窗（glm 重审 Q2-2 项1：与否定窗**分开**，两层都必须过两个窗）
#: 依据：规则 8（箱内自述）与规则 5/6（会话证据）必须同一套排除窗，否则
#: 「已定档，待夜间批处理稳定后接入」会从箱内自述层原样漏进输出。
#: 只含「事情还没发生」的标志词，不含纯否定词。
#: ★ 二修项B（qwen3.7 复核雷#2/#3/#4）：裸词收窄——
#:   「拟」→「拟定|拟稿|拟于|拟在|拟再」（原裸「拟」误杀「模拟器已完成」6/6）；
#:   「后续」→「后续将|后续再|后续待|后续需」（原裸「后续」误杀「后续工作已完成」）；
#:   「准备」从本窗**删除**（否定窗已含，两窗词表边界划清，雷#4 一并解决）。
SETTLE_FUTURE_RE = _re.compile(
    r"(明天|下周|待做|之后|即将|将来|打算|排期|拟定|拟稿|拟于|拟在|拟再"
    r"|后续将|后续再|后续待|后续需|待[^，。；]*后|等[^，。；]{0,6}再)")

#: 路径剥离（2026-9-20 三修·雷①）：证据句里的文件路径（D:/docs/...、
#: file:///D:/...、D:\docs\...）**不是话题内容**——目录名撞标题关键词（实测：
#: 路径里的 "hermes" 撞上三笔含 "Hermes" 的标题 → 影子层 1 条证据误挂 3 笔账）。
#: 两层共用：judge() 匹配前整段剥掉，剥完再提词。与 todo_box_scan.WINPATH 同族
#: （那个是「从标题提 ref_paths」，这个是「从证据句删路径」——方向相反，各自单源）。
#: 参与剥离的目录名（TODOBOX_PATH_STRIP_DIRS 可配，逗号分隔）——
#: 证据句里的 /dir/... 路径不是话题内容，目录名会撞标题关键词。
PATH_STRIP_DIRS = tuple(
    x for x in (os.environ.get("TODOBOX_PATH_STRIP_DIRS")
                or "docs,knowledge-base,notes").split(",") if x)
SETTLE_PATH_STRIP_RE = _re.compile(
    r"(?:file:///)?[A-Za-z]:[\\/][^\s，。；、）)】\]」』]*"
    r"|(?<![A-Za-z0-9_.-])/(?:" + "|".join(_re.escape(d) for d in PATH_STRIP_DIRS)
    + r")/[^\s，。；、）)】\]」』]*")

#: 引号动词剥离（三修雷③ 2026-9-20）：「已修」/『销账』这类**带引号的完成动词**
#: 是在「谈论这个词」（mention），不是在「陈述完成」（use）——实测：诊断句
#: 「…关键词，『已修』又是强动词——三笔全误报」被当成三笔账的完成证据。
#: 只在 judge() 的证据句路径剥（上岗层输入是标题，引号动词罕见；且闸门级
#: 断言『销账哨误报问题搞定了』不带引号，不受影响）。
SETTLE_QUOTED_VERB_RE = _re.compile(
    r"[「『](?:销账|通过验收|已解决|已交付|已上线|已修|搞定|落地|收官|完成)[」』]")

#: 句切分真源（glm 重审 Q4-1 项5）：中英标点 + 分号 + 换行。
#: 原先只有「。！？；」——多行无标点消息成整块（否定/将来词任意出现即整
#: 块排除，偏漏报）；英文句点不切分，中英混排粒度失效。两层共用此常量。
SETTLE_SPLIT_RE = _re.compile(r"[。！？；.!?;]+|\n+")

#: 讨论噪音窗（qwen 重审 C1 项2）：疑问代词/探讨类动词 + 完成动词，或
#: 探讨类动词 + 方案类名词 → 纯属「聊到没干完」，命中即跳过该句（两层共用）。
#: ★ 二修项A（qwen3.7 复核雷#1）：原第三支 `(搞定|落地|完成|解决).{0,10}(方案|计划|
#:   思路|可行性)` 把完成动词放在前件——「已完成方案评审」「已确认交付完成」这类
#:   **真完成陈述**被当讨论噪音误杀（上岗层漏报、影子层丢证据）。现改为：
#:   第三支前件 = 探讨类动词（讨论/研究/评估/分析/商量），完成动词在前的支删除；
#:   同时给第二支加「前置无『已』」负向断言（`(?<!已)`）——「已确认交付完成」放行，
#:   「确认落地方案」仍拦。
SETTLE_DISCUSS_RE = _re.compile(
    r"(怎么|如何|怎样|啥|什么).{0,10}(搞定|落地|完成|解决)"
    r"|(?<!已)(讨论|研究|评估|确认|分析|商量).{0,15}(搞定|落地|完成|解决)"
    r"|(?<!已)(讨论|研究|评估|分析|商量).{0,15}(方案|计划|思路|可行性)")

#: 状态名词互斥窗（qwen 重审 W1 项7）：弱动词后 0-4 字内跟状态名词 →
#: 是「动作对象」不是「动作完成」，该句降级为无效证据。
SETTLE_PROGRESS_NOUN_RE = _re.compile(r"(进度|情况|状态|问题)")


#: 影子层专用——完成动词分级（deepseek W7：销账39次 vs 完成2096次，区分度差两个数量级）
SETTLE_VERBS_STRONG = ("销账", "通过验收", "已解决", "已交付", "已上线", "已修")
SETTLE_VERBS_WEAK = ("搞定", "落地", "收官", "完成")

#: 自指排除窗（哨兵讨论自身/审查自身文档的句子不是任何账的证据；
#: 消融实证：关掉它 FP 0→4。做成可维护清单不硬编码——deepseek 重审#5）
SETTLE_SELF_REF_RE = _re.compile(r"(销账哨|审查_销账哨|settle_probe|settle_shadow|回测报告|本方案|该方案)")

#: 影子层证据时间窗（上界显式定义——glm 重审 Q2-3：证据必须落在
#: [first_seen, first_seen+30天] 内，超窗证据视陈年盲区不采）
SETTLE_EVIDENCE_DAYS = 30
#: 证据窗文字定义（项3 glm Q2-3「30天窗」自相矛盾：规则表无证据时间上界，
#: 而 §四 覆盖区/陈年盲区都依赖它）。两层与方案文档引用此串，避免再漂移。
SETTLE_EVIDENCE_WINDOW = (
    "证据时间窗：下界=条目 first_seen，上界=当天，跨度≤30天（SETTLE_EVIDENCE_DAYS）；"
    "滑出窗的证据不采信（陈年账盲区=诚实边界，见方案§四）。"
    "first_seen 缺失时降级条目 created 并在证据上标「粗」。")

#: ── 规则12 熔断（项4 glm Q2-4：原「连续3天 scanned≥5 且 hit=0」在 24 笔日扫
#:    下 scanned≥5 恒真 → 退化成「3天无命中就喊判据过严」，平稳期周期性误报，
#:    哨兵自己变噪音源。改挂**采纳率/忽略率**：只看 hit 连续零命中 + 存量嫌疑）──
#: 二修项C/D（qwen3.7 复核雷#5/#6）：
#:   C —— backlog 不再取「历史任意一天 n_suspects 的最大值」（那是「7 天前曾有过
#:        嫌疑」，报「存量嫌疑 2 笔无人处理」时那 2 笔早已不在清单里，语义与事实
#:        相反）。改挂**上一轮 payload 的 suspects 中 ignored=False 的实际笔数**
#:        （读 todo_settle_shadow.json 上一条记录，由调用方以 prev_pending 传入）。
#:   D —— 补对称报警：连续 N 天 hit>0 且遗留嫌疑只增不减（天天命中天天没人处置）
#:        也必须出声；原先 hit>0 反复打断 streak ⇒ 真积压永不出声。
#: silent_days：连续天数阈值（零命中侧与命中侧共用，原 3 天 → 7 天）
SETTLE_FUSE_SILENT_DAYS = 7
#: 熔断条件 2：存量嫌疑（**上一轮 payload** 中 ignored=False 的 suspects）非空才出声
SETTLE_FUSE_NEEDS_BACKLOG = True
#: scanned 语义（glm Q2-4：原未定义）——过预筛的候选账数（明细·state=todo·
#: 排除等人/永不催），如现行 24；**不是**消息数、不是全库笔数。
SETTLE_SCANNED_DEF = (
    "scanned = 过预筛候选账数（kind=明细 且 state=todo 且 cls 不在 等人/永不催/"
    "参考/约定/前置/子步骤），不是消息数；hit = 当日判据产出的 suspects 笔数。")


def settle_fuse_check(history, silent_days=None, prev_pending=0):
    """规则12 熔断判定（项4 + 二修项C/D）——纯函数，可断言。

    history 形如 [{"date":"2026-09-18","scanned":24,"hit":0,"n_suspects":0}, …]，
    按 date 升序（尾部=最新）。返回 dict(alert, reason, streak)。

    prev_pending（二修项C）：**上一轮 payload** 的 suspects 中 ignored=False 的
    实际笔数（调用方从 todo_settle_shadow.json 上一条记录读出后传入）。
    绝不再用 history 的 max(n_suspects)——那是「历史上曾有过嫌疑」，不是「现存积压」。

    出声条件（三条，①/② 为主路径，③ 为二修项D 补的对称报警）：
      ① 尾部连续 hit==0 达 silent_days 天（默认 7）——判据可能过严/数据源断裂
         **且** 存在存量嫌疑（prev_pending > 0）——有人该处理没人处理
      ② 纯平稳期（连续零命中 + 无存量）一律静默：哨兵不做周期性噪音源
      ③ 对称报警（项D）：尾部连续 hit>0 达 silent_days 天**且**遗留嫌疑只增不减
         （prev_pending ≥ 最近一次命中日的 suspects 笔数）——天天命中天天没人
         处置的真积压。原先 hit>0 反复打断 streak ⇒ 真积压永不出声（反向盲区）。
    """
    sd = SETTLE_FUSE_SILENT_DAYS if silent_days is None else silent_days
    streak = 0
    for e in reversed(list(history or [])):
        if int(e.get("hit") or 0) == 0:
            streak += 1
        else:
            break
    hit_streak, last_hit_n = 0, 0
    for e in reversed(list(history or [])):
        if int(e.get("hit") or 0) > 0:
            hit_streak += 1
            if last_hit_n == 0:
                last_hit_n = int(e.get("n_suspects") or 0)
        else:
            break
    pending = int(prev_pending or 0)
    if streak >= sd and pending > 0:
        return dict(alert=True, streak=streak, kind="silent_streak", pending=pending,
                    reason=f"连续 {streak} 天零命中，且上轮遗留嫌疑 {pending} 笔未处置"
                           f"——判据过严或数据源异常，请示人")
    if streak >= sd:
        return dict(alert=False, streak=streak, kind="calm", pending=0,
                    reason=f"连续 {streak} 天零命中且上轮无遗留嫌疑——平稳期，静默"
                           f"（原「scanned≥5 恒真」误报已废除）")
    if hit_streak >= sd and pending > 0 and pending >= last_hit_n:
        return dict(alert=True, streak=hit_streak, kind="hit_streak", pending=pending,
                    reason=f"连续 {hit_streak} 天有命中，且遗留嫌疑 {pending} 笔只增不减"
                           f"——天天命中天天无人处置，真积压，请示人")
    return dict(alert=False, streak=streak, kind="below_threshold", pending=pending,
                reason=f"零命中连续 {streak} 天 < {sd} 天阈值；命中连续 {hit_streak} 天")


#: 影子层产物（独立文件！绝不写 todo_notify_summary.json——C4 教训：
#: 该文件被 05:35 看板整体 json.dump 覆盖，新键必蒸发）
SETTLE_SHADOW_JSON = os.path.join(HOME, "todo_settle_shadow.json")
#: 熔断历史保留天数（payload.history 上限；同日重跑覆盖当日条目不重复计）
SETTLE_HISTORY_DAYS = 30
#: 人工忽略登记（规则14 + glm 重审#6 指纹逐条化）：{tid: evidence_hash}。
#: **按条目**忽略——证据集变化（hash 变）则重新出声，不因集合变化重报旧命中。
#: 只读文件：由人/看板写，影子层绝不写它。
SETTLE_IGNORED_JSON = os.path.join(HOME, "todo_settle_ignored.json")

#: 状态名词互斥窗（项7）：弱动词 + 0-4 字内状态名词 = 动作对象非完成。
#: 定义在动词表之后（依赖 SETTLE_VERBS_WEAK）。
#: ★ 间隔**不含标点**（，。！？；、,.!?;）——「落地进度」是名词化（同句紧邻，
#:   动作对象），而「搞定了，进度回头补记」是完成陈述后另起小句，不误杀。
SETTLE_PROGRESS_MUTEX_RE = _re.compile(
    r"(?:" + "|".join(SETTLE_VERBS_WEAK) + r")[^，。！？；、,.!?;\n]{0,4}"
    r"(?:" + SETTLE_PROGRESS_NOUN_RE.pattern + r")")


def settle_sentence_ok(sent: str, self_ref: bool = True) -> bool:
    """句级判定闸门——**两层共用·单一真源**（项1/2/7）。

    该句是否可作为销账证据。依次过排除窗，任一命中即毙：
      ① 自指窗（哨兵讨论自身不是账的证据）——仅 self_ref=True 时生效
      ② 讨论噪音窗（「怎么搞定X」「X的落地方案」＝聊到没干完）
      ③ 否定窗（没/未/无法…）
      ④ 将来时窗（明天/待X后/等X再/计划/准备…）
      ⑤ 状态名词互斥窗（弱动词后 0-4 字跟进度/情况/状态/问题 = 动作对象非完成）
    上岗层（todo_box_scan.settle_selfreport）与影子层（settle_shadow.judge）
    都调此函数——避免两份排除窗逻辑漂移（本仓「双份脚本陷阱」历史教训）。

    ★ self_ref 默认 True（影子层：证据句谈哨兵自身不算别的账的证据）。
      上岗层传 False——该层输入是「条目自己的标题」，条目本身是「修销账哨X」
      这类元任务时标题必然含自指词；照毙就整类漏报，且修补前的上岗层本无
      自指窗（保持行为不变）。「上岗层是否也该吞掉元任务」→ 战报待裁决。
    """
    if not sent:
        return False
    if self_ref and SETTLE_SELF_REF_RE.search(sent):
        return False
    if SETTLE_DISCUSS_RE.search(sent):
        return False
    if SETTLE_NEG_RE.search(sent) or SETTLE_FUTURE_RE.search(sent):
        return False
    # ⑤ 仅对「弱动词且无强动词」的句子生效：强动词句（销账/通过验收…）的
    #    状态名词不改判（如「销账进度」仍是销账的事实陈述）。
    if not any(v in sent for v in SETTLE_VERBS_STRONG) and SETTLE_PROGRESS_MUTEX_RE.search(sent):
        return False
    return True
