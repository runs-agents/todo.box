# -*- coding: utf-8 -*-
"""待办箱扫描器 v4 —— 只读箱子，只写索引（原子写 + 锁保护）。

【v4 语义变更】扫 = **枚举文件**（一待办一文件），行解析退役。
    旧 v3：os.walk 找 `- [ ]` 行 → 一个账本 N 行 = N 笔
    新 v4：os.walk 按**文件名协议**枚举 → 一个文件 = 一笔
    文件名即协议：`OPEN_20260918_T00002e_盯办_标题.txt`
    首行 `tid:` 标记与文件名 tid **双验**（任一不符 → 待整理，不静默）

【设计依据】《方案_待办箱完全体搬迁_20260917》v1.1 §1.2/§三 + 工单单3

【铁律不变】
  · 绝不修改箱内任何现有文件
  · 索引每次全量重建，禁止手工修改
  · 原子写：.tmp → os.replace()，另存 last_good 兜底
  · 日期一律 Asia/Shanghai
  · **旧箱（LEGACY_BOX）不扫**——封存后它不存在于扫描范围
  · 不合协议的文件 → 「📥 待整理」分节（不静默，红线：坏掉必须响）
"""
import os, re, sys, json, time, hashlib, datetime, sqlite3
from zoneinfo import ZoneInfo

# ★ 修复 V2-3（外审 2026-09-29 终验）：裸 Windows Python 无 tzdata 包时
#   ZoneInfo("Asia/Shanghai") 崩在模块加载——开源新用户「零门槛冷启动」当场翻车
#   （实测复现：Python 3.12.10 裸装，import 即 ZoneInfoNotFoundError）。
#   Asia/Shanghai 无夏令时，UTC+8 固定偏移兜底语义等价（本工具只做自然日运算）。
try:
    SH = ZoneInfo("Asia/Shanghai")
except Exception:
    SH = datetime.timezone(datetime.timedelta(hours=8))   # tzdata 缺席兜底（UTC+8）
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import box_config as bc  # noqa: E402
HOME = bc.HOME               # 开源参数化（2026-9-29）：路径单源 box_config，hermes_config 依赖已切
SKILLS_DIR = bc.SKILLS_DIR   # 对账强信号①（技能库）；空=跳过
STATE_DB = bc.STATE_DB       # 对账强信号③（会话库，只读）；空=跳过
import write_safe  # noqa: E402  FIX-12：统一 read_text_strict 真源
import todo_id  # noqa: E402
# ★ v4：路径全部走 box_config 单源（新家）。旧箱只读，不进扫描范围。
BASE      = bc.BOX
OUT_MD    = bc.INDEX_MD
OUT_JSON  = os.path.join(HOME, "todo_box_index.json")
LAST_GOOD = os.path.join(HOME, "todo_box_index.last_good.json")
LOCK      = bc.LOCK_FILE   # B-1：统一锁名（三工具共用）
STATE     = os.path.join(HOME, "todo_box_state.json")   # 变化检测 + 异常去重
SCHEMA    = 2   # ★ v4：schema 升 2（items 由「行」改「文件」）
# ★ 2026-9-13：改为从 box_config 导入（单一真源）——
#   此前这里自硬编码一份 SKIP，与 box_config.SKIP_NAMES 漂移
#   （历史教训：_check_conservation.py 曾三处白名单不一致）。
from box_config import SKIP_NAMES as _SKIP_NAMES  # noqa: E402
from box_config import SKIP_SUFFIXES as _SKIP_SUFFIXES  # noqa: E402  FIX-18：接入扫描器
from box_config import (SETTLE_IN_TITLE_DONE, SETTLE_NEG_RE,  # noqa: E402
                        SETTLE_SHADOW_JSON, SETTLE_EVIDENCE_DAYS,
                        SETTLE_SPLIT_RE, SETTLE_FUTURE_RE,
                        settle_sentence_ok)
from box_config import TMP_DIR as _TMP_DIR  # noqa: E402  FIX-21：索引 MD 的 tmp 落同卷箱外
SKIP      = set(_SKIP_NAMES)

#: v4：可枚举的协议文件名（一文件一笔）
FN_PROTO  = bc.FN_RE
#: 旧协议残留（`YYYYMMDD_待办账.txt` 等）→ 一律进「待整理」，不再按行解析
LEGACY_FN = re.compile(r"^\d{8}_待办账\.txt$")

#: 编码全失败的文件清单（2026-9-12 修：原先静默跳过，现记入并在扫描结束时告警）
ENCODING_FAILURES: list = []
#: v4：未进扫描范围的文件（不合协议 / tid 双验失败）——不静默，进「待整理」
UNPARSED_FILES: list = []
WINPATH   = re.compile(r'[A-Za-z]:[\\/][^\s，。；、）)】\]]*')
#: 文件体内「上下文: <指针>」行（迁移时写入的原账本坐标）
CTX_LINE  = re.compile(r"^上下文[:：]\s*(.+?)\s*$", re.M)

def read_text(p):
    """按序尝试四种编码读出文本 —— ★ FIX-12（A包 A1）：薄包装，真源在 write_safe。

    2026-9-12 修（3.8-max 只读源码核对发现）：四种编码全失败时原先 `return ""`
    ——**整个文件被当作空文件静默跳过**，一本 15 条账的账本会全部从索引消失。
    改为：抛异常并把文件记入 `ENCODING_FAILURES`（不中断整次扫描，但会汇总告警）。

    FIX-12：本函数原为三份拷贝之一（与 box_add / box_tidy 各一份，且都缺 \ufeff 残留检查），
    现统一走 `write_safe.read_text_strict`（单一真源）。
    """
    txt, _enc, lossy = write_safe.read_text_strict(p)
    if lossy:
        ENCODING_FAILURES.append(os.path.relpath(p, BASE))
        raise UnicodeDecodeError("utf-8", b"\x00", 0, 1,
                                 "四种编码均无法解码（文件已记入编码失败清单）")
    return txt

# ══════ 灰带原则（演化存续，2026-9-13 诞生 → v4 协议改版后以新形态存活）══════
# 原则：「机器管客观形式，真相归人眼」——绝不自动删除，只标记/分带，交人终审。
# 行协议时代：三带判定（形态可疑 `1. **xxx**` → 标 suspected 交人眼，实测 6/6 零误报）。
# v4 文件名协议后：扫描面=协议文件名，闸门输入不复存在——原则演化为
# 「协议命中 / 待整理」分带（不合协议的文件出声进「待整理」，绝不静默丢弃）。
# 本文件保留 suspected 字段（schema 兼容 + 未来接线位），恒为 False。

LOCK_STALE_SECONDS = bc.LOCK_STALE_SECONDS   # ★ 修复 F4（外审 2026-09-28）：收归 box_config 单源——原此处自存一份 600，人工同步正是 box_config 存在要杀死的漂移模式。两处值已核实同为 600，行为不变。


_HELD_LOCK = None       # B1：持有中的 box_lock 对象（供释放）


def _acquire_lock() -> bool:
    """拿锁 —— B1（2026-9-12）改用共享 box_lock（锁协议收敛）。

    切换原因（方案铁律）：任何第二个持锁工具上线前锁协议必须收敛。
    box_add/box_tidy 用 box_lock 的完整状态机（pid 探活 + 心跳 + 释放校验）；
    扫描器若继续用纯时间戳判定，会在长任务（整理器跑 15 分钟）时误清其锁 → 双写。

    失败降级：box_lock 不可用时回退旧时间戳逻辑（保 cron 韧性）。
    """
    global _HELD_LOCK
    try:
        from box_lock import BoxLock, LockBusy
        try:
            lk = BoxLock("todo_box_scan", quiet=True)
            lk.acquire()
            _HELD_LOCK = lk
            return True
        except LockBusy:
            return False
    except Exception:
        pass                                   # 降级：旧逻辑

    if os.path.exists(LOCK):
        try:
            content = open(LOCK, "r", encoding="utf-8").read().strip()
            pid_s, _, ts_s = content.partition(":")
            age = time.time() - float(ts_s)
            if age <= LOCK_STALE_SECONDS:
                return False
        except Exception:
            pass
        try:
            os.remove(LOCK)
        except Exception:
            return False
    try:
        with open(LOCK, "w", encoding="utf-8") as f:
            f.write("%d:%f" % (os.getpid(), time.time()))
        return True
    except Exception:
        return False


def _release_lock():
    """释放锁 —— B1：走 box_lock 的释放校验（只删自己的锁）。"""
    global _HELD_LOCK
    if _HELD_LOCK is not None:
        try:
            _HELD_LOCK.release()
        except Exception:
            pass
        _HELD_LOCK = None
        return
    # 降级路径：旧逻辑（校验 pid 是自己的才删）
    try:
        content = open(LOCK, "r", encoding="utf-8").read().strip()
        pid_s, _, _ts = content.partition(":")
        if int(pid_s) == os.getpid():
            os.remove(LOCK)
    except Exception:
        pass


def _trust_chain_ok() -> bool:
    """B1 信任链校验：锁文件存在 + 持锁者探活为活 → 允许跳过抢锁。

    用途：整理器持锁期间调扫描器（子进程不能抢同一把锁——重入死锁）。
    安全语义：**不是绕过保护，是验证有持锁者**——无活锁则拒绝运行（fail-closed）。
    """
    try:
        from box_lock import _lock_state, LockState
        return _lock_state() == LockState.ALIVE
    except Exception:
        return False

def _index_looks_sane(d) -> bool:
    """索引健康度校验：防止把「合法 JSON 但内容错误」的坏索引备份成 last_good。"""
    st = (d or {}).get("stats") or {}
    return (st.get("todo", 0) + st.get("done", 0) + st.get("non_todo", 0)) > 0 and bool((d or {}).get("items"))


def _load_state():
    try:
        with open(STATE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_state(d):
    try:
        tmp = STATE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=1)
        os.replace(tmp, STATE)
    except Exception:
        pass


def _zone_of(rel: str) -> str:
    """相对路径 → 分区名（顶层目录名；根文件为「(根)」）。"""
    parts = str(rel).split(os.sep)
    return parts[0] if len(parts) > 1 else "(根)"


def scan():
    """v4：枚举新家五区 + 收件箱，按**文件名协议**建条目。一个文件 = 一笔。

    返回 (items, files_hash)。items schema（工单单3）：
      tid / zone / file / title / state(OPEN→todo,DONE→done) / 节奏 / first_seen
      / ref_paths / inbox / 加 kind（恒「明细」，settle/对账/todobox 消费面沿用）
    不合协议的文件**不进 items**，而是记入 UNPARSED_FILES → 「待整理」分节。
    """
    items, files_hash = [], hashlib.sha256()
    ENCODING_FAILURES.clear()
    UNPARSED_FILES.clear()
    #: v4：扫描范围 = 五区 + 收件箱（骨架目录走 SKIP，不进）
    roots = list(bc.ZONES.values()) + [bc.INBOX]
    seen = 0
    for root in roots:
        if not os.path.isdir(root):
            continue
        for fn in sorted(os.listdir(root)):
            p = os.path.join(root, fn)
            if os.path.isdir(p):
                continue
            if fn in SKIP or any(fn.endswith(_s) for _s in _SKIP_SUFFIXES):
                continue
            rel = os.path.relpath(p, BASE)
            zone = _zone_of(rel)
            # 文件哈希进指纹（变化检测用）——**含不合协议的文件**：
            # 它们没进索引，但改了也得让扫描器察觉（否则「待整理」分节会悄悄过期）
            try:
                raw = open(p, "rb").read()
                files_hash.update(rel.encode("utf-8")); files_hash.update(raw)
            except OSError:
                UNPARSED_FILES.append((rel, "读取失败（OSError）"))
                continue
            # ── 协议枚举：文件名必须过 FN_RE，且首行 tid 与文件名 tid 一致 ──
            pr = FN_PROTO.match(fn)
            if not pr:
                why = ("旧协议账本残留（行协议已退役）" if LEGACY_FN.match(fn)
                       else "文件名不合协议（非 OPEN_/DONE_ 五段）")
                UNPARSED_FILES.append((rel, why))
                continue
            seen += 1
            try:
                txt = read_text(p)
            except UnicodeDecodeError:
                continue                      # 已记入 ENCODING_FAILURES
            body_tid = ""
            for _ln in txt.splitlines():
                if _ln.strip():
                    body_tid = _ln.strip()
                    break
            if not body_tid.startswith("tid: ") or body_tid[5:].strip() != pr.group(3):
                UNPARSED_FILES.append(
                    (rel, f"首行 tid 标记缺失/不符（文件名 {pr.group(3)}，"
                          f"首行 {body_tid[:20] or '空'}）"))
                continue
            # 完整描述 = 首行 tid 之后的正文；标题 = 第一段非空正文行
            # ★ 单5 修：原实现把正文全折进 title（含 `原文:` 行与空行）——
            #   索引里出现「原文: ...」当标题渲染 + 同一串出现两次（B4 唯一出现断言抓出）。
            lines = txt.splitlines()
            desc = []
            for _ln in lines[1:]:
                s = _ln.strip()
                if s.startswith(("first_seen:", "节奏:", "上下文:", "原文:", "tid:")):
                    continue
                desc.append(_ln)
            body_full = "\n".join(desc).strip()
            title = ""
            for _ln in desc:
                if _ln.strip():
                    title = _ln.strip()
                    break
            title = title or pr.group(5)
            st = bc.FN_STATE_TO_STATE[pr.group(1)]
            ctx = CTX_LINE.search(txt)
            created = f"{pr.group(2)[:4]}-{pr.group(2)[4:6]}-{pr.group(2)[6:]}"
            fs = ""
            for _ln in lines:
                if _ln.strip().startswith("first_seen:"):
                    fs = _ln.split(":", 1)[1].strip()
                    break
            items.append(dict(
                tid=pr.group(3), zone=zone, file=rel, line=1, kind="明细",
                title=title[:130], body=body_full[:2000], state=st, cls=pr.group(4),
                created=created,
                ref_paths=WINPATH.findall(title) + ([ctx.group(1)] if ctx else []),
                first_seen=fs or created + " 00:00",
                private=("@private" in txt), suspected=False,
                _raw=f"{fn} | {body_tid}", inbox=(zone == "00_收件箱"),
            ))
    # ── 自断言（工单单3 断言3）：扫描数 == 目录枚举数 ──
    # 数的是「过协议的账」（items）+「没过协议的」（UNPARSED）——
    # 两者相加必须等于五区+收件箱里所有非 SKIP 文件数，一个都不能少（红线：不静默）
    enum_total = _count_scan_files(roots)
    assert len(items) + len(UNPARSED_FILES) == enum_total, (
        f"扫描数不符：items={len(items)} + unparsed={len(UNPARSED_FILES)} "
        f"!= 目录枚举={enum_total}")
    return items, files_hash.hexdigest()[:16]


def _count_scan_files(roots) -> int:
    """目录枚举数（scan 的自断言基准）：五区+收件箱里所有非 SKIP 的非目录项。"""
    n = 0
    for root in roots:
        if not os.path.isdir(root):
            continue
        for fn in os.listdir(root):
            if os.path.isdir(os.path.join(root, fn)):
                continue
            if fn in SKIP or any(fn.endswith(_s) for _s in _SKIP_SUFFIXES):
                continue
            n += 1
    return n

def _name_hit(name, title):
    """名字命中判定：纯 ASCII 名用**词边界**（防 VTuber 命中 uber 子串误报）；
    中文/混合名保持子串（中文无词边界，且长中文名子串安全）。"""
    if not name:
        return False
    if re.fullmatch(r"[A-Za-z0-9_.-]+", name):
        return bool(re.search(r"(?<![A-Za-z0-9])" + re.escape(name) + r"(?![A-Za-z0-9])", title))
    return name in title


def settle_selfreport(items):
    """销账哨·上岗层（2026-9-16）：条目自述完成语义 → 标记「疑似已干完待销账」。

    判据（box_config 单一真源）：标题/正文含完成词（已落地/建成/已定档…）
    且过**完整排除窗**（否定 + 将来时 + 讨论噪音 + 状态名词互斥，
    bc.settle_sentence_ok 句级判定）。
    铁律不变：只标记不删除，终审归人（agent 裁决销账、所有者拍板）。
    排除：等人（干没干完判定权在所有者）/永不催类（L3 铁律永不参与）。

    ★ v4：输入面已是文件名枚举，"kind" 恒为「明细」——守卫保留（防将来混入）。
    ★ v4.1（项1/2/5/7，glm Q2-2 + qwen C1/W1）：
      - 句级切分（bc.SETTLE_SPLIT_RE，中英标点+换行）后**逐句**判定；
        命中句必须过排除窗才算——原先只看标题整体，规则 8 与 5/6 矛盾。
      - 讨论噪音窗（「怎么搞定销账哨的误报问题」不再命中）。
      - 状态名词互斥窗（「…的落地进度」不再命中）。
      - 输入面仍**只取标题**（与修补前一致，不扩大出声面）；
        「正文也算自述」是否放开 → 见战报「待裁决」。
    """
    out = []
    for it in items:
        if it["state"] != "todo":
            continue
        if it.get("kind") != "明细":
            continue
        if it.get("cls") in (bc.R_AWAIT, bc.R_NEVER):
            continue
        title = it.get("title") or ""
        for sent in SETTLE_SPLIT_RE.split(title):
            sent = sent.strip()
            if not sent:
                continue
            if not SETTLE_IN_TITLE_DONE.search(sent):
                continue
            if not settle_sentence_ok(sent, self_ref=False):
                continue
            out.append((it, "条目自述完成语义（箱内信号·零外部依赖）"))
            break
    return out


def reconcile(items):
    """强信号对账：路径存在性 / 技能名精确匹配 / 会话标题包含。宁漏勿假。"""
    out = []
    sk_dir = SKILLS_DIR
    skill_names = set()
    if sk_dir and os.path.isdir(sk_dir):   # 开源参数化：源未配置=该路强信号跳过（宁漏勿假）
        for r, d, f in os.walk(sk_dir):
            for x in f:
                if x.endswith(".md"): skill_names.add(os.path.splitext(x)[0])
    titles = []
    if STATE_DB and os.path.exists(STATE_DB):
        try:
            con = sqlite3.connect(f"file:{STATE_DB}?mode=ro", uri=True)
            titles = [t for (t,) in con.execute("SELECT title FROM sessions WHERE title IS NOT NULL")]
            con.close()
        except Exception:
            pass
    for it in items:
        if it["state"] != "todo": continue
        # B5（2026-9-12）：对账**只处理「明细」**（分区账本）——
        # `_方案_`/`_审查_`/`_咨询_` 等文档（kind=制度/总账本）的正文不是待办；
        # 实测它们的英文句子与技能名撞车（signed artifacts→artifacts、
        # CRLF issues→issues），每日 cron 拿着假条目出声，违「宁漏勿假」。
        if it.get("kind") != "明细": continue
        # 非待办类别（参考/约定/前置/子步骤）不参与对账——它们本来就不是待办
        if it.get("cls") in ("参考", "约定", "前置", "子步骤"): continue
        hits = []
        for p in it["ref_paths"]:
            if os.path.exists(p): hits.append(f"路径已存在：{p}")
        for s in skill_names:
            if len(s) > 3 and _name_hit(s, it["title"]): hits.append(f"同名技能：{s}")
        for t in titles:
            if len(t) > 4 and _name_hit(t, it["title"]): hits.append(f"同名会话：{t}")
        if hits: out.append((it, hits))
    return out

def write_index(items, src_hash, hits, settle_hits=None):
    """v4：索引 schema v2 —— 一文件一笔；「待整理」= 收件箱条目 + 不合协议文件。"""
    # ── FIX-1（A包 C2）空箱守卫 ──
    # 箱子不可达时 os.walk 不抛异常、只返回空 → 空索引会覆盖现役索引，
    # 且 real=0 时 anomalies 也为空 → --auto 下零输出，全链静默归零。
    if not os.path.isdir(BASE):
        raise RuntimeError(f"箱子目录不可达，拒绝重建索引: {BASE}")
    now = datetime.datetime.now(SH).strftime("%Y-%m-%d %H:%M")
    detail = [x for x in items if x["kind"] == "明细"]
    # B4（2026-9-12）+ v4：收件箱内容单独分节（「待整理」）——不混进分类区
    inbox_todo = [x for x in detail if x.get("inbox") and x["state"] == "todo"]
    zoned = [x for x in detail if not x.get("inbox")]
    todo = [x for x in zoned if x["state"] == "todo"]
    done = [x for x in detail if x["state"] == "done"]
    # v4：真待办 = 五区 OPEN_（收件箱的算待整理，不算已归位的活账）
    real = list(todo)
    order = list(bc.RHYTHMS)
    L = [f"# 待办箱索引（自动生成 · 请勿手改）", "",
         f"> 生成：{now} ｜ schema v{SCHEMA} ｜ 源 hash：{src_hash}",
         f"> 真源仍是各分区文件（**一待办一文件**），本文件只是**派生索引**，每次扫描全量重建。", "",
         f"**统计**：真待办 **{len(real)}** 笔 ｜ 待整理 {len(inbox_todo) + len(UNPARSED_FILES)} 笔"
         f"（收件箱 {len(inbox_todo)} + 不合协议 {len(UNPARSED_FILES)}）"
         f" ｜ 已完成 {len(done)} 笔",
         # ★ FIX-20（A包 D9）：编码失败只进 stdout 时，cron 输出被 deliver=local 静默 → 告警只剩文件。
         #   现在索引头也写一行，让索引本身可见。
         ] + ([f"> 🔴 **{len(ENCODING_FAILURES)} 个文件未能解码**（内容完全未进入索引）："
               + "；".join(ENCODING_FAILURES[:5]) + ("…" if len(ENCODING_FAILURES) > 5 else "")]
              if ENCODING_FAILURES else []) + [""]
    # ── 📥 待整理：收件箱条目 + 不合协议文件（红线：坏掉必须响，不静默）──
    if inbox_todo or UNPARSED_FILES:
        L += [f"## 📥 待整理（{len(inbox_todo) + len(UNPARSED_FILES)} 笔 · 等待归位）", ""]
        for x in inbox_todo:
            L.append(f"- [ ] {x['title']}  `{x['file']}`")
        for rel, why in UNPARSED_FILES:
            L.append(f"- ⚠ **不合协议**：`{rel}` —— {why}（人工处置：改名入协议 或 移出箱外）")
        L.append("")
    for c in order:
        grp = [x for x in real if x["cls"] == c]
        if not grp:
            continue
        L += [f"### {c} （{len(grp)} 笔）"]
        for x in grp:
            L.append(f"- [ ] {x['title']}  `{x['file']}`  `{x['tid']}`")
        L.append("")
    if settle_hits:
        L += [f"## 🕵 销账哨·疑似已干完待销账（{len(settle_hits)} 笔 · 箱内自述信号 · 只标记不删，终审归人）", ""]
        for it, why in settle_hits:
            L.append(f"- {it['title']}  `{it['file']}` （{why}）")
        L.append("")
    if hits:
        L += ["## ⚠ 疑似已完成（强信号对账 · 只标记不删）", ""]
        for it, hs in hits:
            L.append(f"- {it['title']}  ← " + "；".join(hs))
        L.append("")
    if done:
        L += [f"## ✅ 已完成（{len(done)} 笔 · 在 _done\\ 按月归档）", ""]
        for x in sorted(done, key=lambda y: y["file"]):
            L.append(f"- {x['title']}  `{x['file']}`  `{x['tid']}`")
        L.append("")
    md = "\n".join(L)
    js = dict(schema_version=SCHEMA, generated=now, source_hash=src_hash,
              stats=dict(todo=len(real), inbox=len(inbox_todo),
                         unparsed=len(UNPARSED_FILES),
                         待整理_count=len(inbox_todo) + len(UNPARSED_FILES),
                         non_todo=0, done=len(done),
                         by_cls={c: len([x for x in real if x["cls"] == c]) for c in order}),
              unparsed=[{"file": r, "why": w} for r, w in UNPARSED_FILES],
              items=items)
    # ── FIX-1（A包 C2）：零结果是异常事件，不是正常态 ──
    # 本次扫出 0 条明细（真待办+收件箱+不合协议全空），而现役索引健全 → 拒绝覆盖
    # （好副本只剩 last_good，没人知道去拿）。返回 None 交由 main 判空跳过后续写，并出声一次。
    # ★ 开源改造修正（2026-9-29）：判据从 real==0 收紧为 detail==0——收件箱条目也是账，
    #   「只剩收件箱待整理件」是合法状态（新用户/纯收件箱工作流），不该被守卫拦下
    #   （实测：B4 收件箱测试件在空五区下被守卫误拦，索引永不更新）。
    if not detail and os.path.exists(OUT_JSON):
        try:
            old = json.load(open(OUT_JSON, encoding="utf-8"))
            if _index_looks_sane(old):
                print("⚠ 本次扫描 0 条而现役索引健全——拒绝覆盖，请人工核查箱子路径: " + BASE)
                return None, None, None
        except Exception:
            pass
    tmp = OUT_JSON + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(js, f, ensure_ascii=False, indent=1)
        f.flush(); os.fsync(f.fileno())      # FIX-22（A包 D1）：原子 ≠ 落盘，断电/蓝屏窗口防 0 字节
    if os.path.exists(OUT_JSON):
        try:
            old = json.load(open(OUT_JSON, "r", encoding="utf-8"))
            if _index_looks_sane(old):      # 只备份健康版本，坏的不传染 last_good
                os.replace(OUT_JSON, LAST_GOOD)
        except Exception:
            pass
    os.replace(tmp, OUT_JSON)
    # B-1（2026-9-12）：MD 索引也走原子写（此前直接 open("w")，写一半会留截断索引）
    # ★ FIX-21（A包 D10）：tmp 落 TMP_DIR —— 与 write_safe「tmp 避开 OneDrive」原则一致。
    md_tmp = os.path.join(_TMP_DIR, f".idxmd_{os.getpid()}_{int(time.time())}.tmp")
    with open(md_tmp, "w", encoding="utf-8", newline=chr(13) + chr(10)) as f:
        f.write(md)
        f.flush(); os.fsync(f.fileno())      # FIX-22（A包 D1）
    os.replace(md_tmp, OUT_MD)
    return real, done, md

def _bucket(n: int) -> str:
    """量级桶：同一异常在数量翻档时才再报一次，避免掩盖恶化。

    ★ FIX-17（A包 C6）：末档原为无上界的「100+」→ 100 与 1500 同档，
      恶化被 last_alert 当日去重永久掩盖。现分段到 1k-2.9k。
    """
    for hi, name in ((10, "1-9"), (30, "10-29"), (100, "30-99"),
                     (300, "100-299"), (1000, "300-999"), (3000, "1k-2.9k")):
        if n < hi:
            return name
    return "3k+"


def ensure_skeleton() -> list:
    """建新家骨架（幂等）：五区 + 收件箱 + _done + _registry + _snapshots。

    工单单3 断言1。只建目录，不写任何文件（骨架本身不进扫描范围——SKIP 已含）。
    """
    made = []
    for d in list(bc.ZONES.values()) + [bc.INBOX, bc.DONE_DIR, bc.REGISTRY_DIR,
                                        os.path.join(bc.BOX, "_snapshots")]:
        if not os.path.isdir(d):
            os.makedirs(d, exist_ok=True)
            made.append(d)
    return made


def main():
    args = sys.argv[1:]
    check = "--check" in args
    auto = "--auto" in args          # cron 用：变化驱动，无异常则完全静默
    trust = "--trust-lock" in args   # B1：信任链模式（持锁工具调用专用）
    skeleton = "--skeleton" in args  # v4：建骨架（单3）

    if skeleton:
        made = ensure_skeleton()
        print(f"✓ 骨架就位（新建 {len(made)} 个目录）")
        for d in made:
            print(f"   + {d}")
        if not made:
            print("   （已存在，无改动）")
        return

    if trust:
        if not _trust_chain_ok():
            print("✗ 信任链校验失败：未发现活锁持有者。--trust-lock 仅供持锁工具调用。")
            return 1
    else:
        if not _acquire_lock():
            if not auto:
                print("已有扫描在跑（锁有效），跳过本轮")
            return
    try:
        items, h = scan()
        hits = reconcile(items) if check else []
        settle_hits = settle_selfreport(items) if check else []   # 销账哨·上岗层

        # ★ v4（单3）：tid 已在 scan() 内从**文件名+首行双验**取得——
        #   不再调 todo_id.assign。原因：assign 是给「行协议」时代追同一笔账用的，
        #   它靠 file:line 坐标与相似度复用；新协议下 tid 就写在文件名里，是既有事实，
        #   再跑 assign 反而会把两笔标题相近的账并成一个号（污染注册表）。
        #   注册表的维护归写侧（box_add.mint_fresh / migrate_v2 沿用冻结 tid）。

        detail = [x for x in items if x["kind"] == "明细"]
        inbox_items = [x for x in detail if x.get("inbox") and x["state"] == "todo"]
        todo = [x for x in detail if x["state"] == "todo" and not x.get("inbox")]
        real = list(todo)
        nontodo = []
        done = [x for x in detail if x["state"] == "done"]
        待整理 = len(inbox_items) + len(UNPARSED_FILES)

        st_old = _load_state()
        changed = (h != st_old.get("last_hash"))

        # 索引**始终重建**：它含对账结果，而对账依赖外部状态（路径是否存在、会话标题），
        # 外部状态变了而箱子没变时，跳过重建会让索引里的对账段过期。
        # （「变化检测」只用于 state 记录与是否出声，不用来省这次写入。）
        wres = write_index(items, h, hits, settle_hits)
        if wres is None or wres[0] is None:
            # ── FIX-1（A包 C2）：空箱守卫触发 —— 索引未覆盖，跳过后续写，出声一次 ──
            print("🔴 空箱守卫：本次 0 条而索引健全，已拒绝覆盖索引；"
                  "本次不写 stats/anomalies，请人工核查箱子路径。")
            return

        # 异常汇总（key 用于当日去重）
        anomalies = []
        if hits:
            anomalies.append((f"reconcile:{_bucket(len(hits))}", f"对账命中「疑似已完成」{len(hits)} 条（强信号）"))
        if UNPARSED_FILES:
            # ★ v4 红线：不合协议的文件**必须出声**（人写协议：写错→标待整理，不静默）
            anomalies.append((f"unparsed:{_bucket(len(UNPARSED_FILES))}",
                              f"{len(UNPARSED_FILES)} 个文件不合文件名协议（已列「待整理」）："
                              + "；".join(f"{r}（{w}）" for r, w in UNPARSED_FILES[:3])))
        if settle_hits:
            anomalies.append((f"settle:{len(settle_hits)}", f"销账哨：{len(settle_hits)} 笔疑似已干完待销账（箱内自述信号）——真干完请销账，误报请忽略（影子层记录在案）"))
        if len(real) > 15:
            anomalies.append((f"backlog:{_bucket(len(real))}", f"待办积压 {len(real)} 笔（>15），建议清一轮"))
        if ENCODING_FAILURES:
            # ★ FIX-20（A包 D9）：编码失败原先只进 stdout——cron 输出被 deliver: local 静默时
            #   这条告警就只剩文件。现在并入 anomalies（配 FIX-17 的分档桶）。
            anomalies.append((f"encoding:{_bucket(len(ENCODING_FAILURES))}",
                              f"{len(ENCODING_FAILURES)} 个文件未能解码——内容**完全没进索引**，账可能已消失："
                              + "；".join(ENCODING_FAILURES[:5])))

        today = datetime.datetime.now(SH).strftime("%Y-%m-%d")
        last_alert = dict(st_old.get("last_alert") or {})
        fresh = [(k, t) for (k, t) in anomalies if last_alert.get(k) != today]
        for k, _ in fresh:
            last_alert[k] = today
        _save_state(dict(last_hash=h, last_run=datetime.datetime.now(SH).strftime("%Y-%m-%d %H:%M"),
                         stats=dict(todo=len(real), done=len(done), non_todo=len(nontodo),
                                    inbox=len(inbox_items), unparsed=len(UNPARSED_FILES)),
                         last_alert=last_alert))

        if auto:
            # 无事 → 完全静默（no_agent 下空输出＝不发任何消息）
            if not fresh:
                return
            print("【待办箱体检 · 有事需要瞄一眼】")
            for _, t in fresh:
                print(f"- {t}")
            for it, hs in hits:
                print(f"    · {it['title'][:60]} ← {hs[0]}")
            print("（完整清单：新家 _索引.md）")
            return

        # 手动模式：始终给完整报告
        tag = "（内容有变化，索引已重建）" if changed else "（内容无变化，索引沿用）"
        print(f"扫描完成 {tag} ｜ 源hash={h} ｜ 真待办 {len(real)} ／ 已完成 {len(done)} "
              f"／ 待整理 {待整理} ／ 疑似完成 {len(hits)}")
        for c in list(bc.RHYTHMS):
            g = [x for x in real if x["cls"] == c]
            if g:
                print(f"\n[{c}] {len(g)} 笔")
            for x in g:
                print(f"   - [{x['tid']}]", x["title"][:72])
        if UNPARSED_FILES:
            print("\n⚠ 不合协议（已列「待整理」，人写协议：写错必须响）：")
            for r, w in UNPARSED_FILES:
                print(f"   - {r} —— {w}")
        if hits:
            print("\n⚠ 疑似已完成：")
            for it, hs in hits:
                print("   -", it["title"][:60], "←", hs[0])
        if ENCODING_FAILURES:
            # 2026-9-12 修（3.8-max 源码核对发现）：编码全失败的文件原先静默消失，
            # 整本账可能以「零条目」身份蒸发。现在必须出声。
            print("\n🔴 编码失败（这些文件的内容**完全没能进入索引**，账可能已消失）：")
            for f in ENCODING_FAILURES:
                print(f"   - {f}")
            print("   处置：用 write_safe.read_text_strict 检查实际编码，人工转码后重扫。")
        if fresh:
            print("\n⚠ 异常（cron 会据此出声）：")
            for _, t in fresh:
                print("   -", t)
    finally:
        # B1：安全释放——信任链模式下不碰别人的锁；否则释放自己的
        if not trust:
            _release_lock()


if __name__ == "__main__":
    main()
