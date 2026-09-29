# -*- coding: utf-8 -*-
"""提醒节流（notify throttle）—— 2026-9-13

回答一个问题：**哪些账"现在值得被提一次"？**

设计（所有者 2026-9-12 设计 + 2026-9-13 架构修正 + agent 落地）：

  ★ 架构修正（所有者 2026-9-13 09:42 拍板）：
    「把提醒整合进 UI —— 每天形成看板时计算时间、在看板上进行提醒」
    好处：① 一天开多次新对话也不会多次提醒（看板每天只生成一次，天然去重）
          ② 机械计算比人手动算更精确稳定

  ① 每条账有一个「节奏」（几天提一次），来自 box_config.NOTIFY_CADENCE
  ② 算「距上次提醒多少天」→ 够了才提，不够就折叠
  ③ 提过的更新时间戳（`last_notified`），进入下一轮计算
  ④ **首次提醒**：新账（从未提过）立即可提——否则可能十天不出声
  ⑤ 「没提」必须可见：输出要说「今天该提 N 笔，另有 M 笔未到提醒期」

时间戳存哪：**独立的 `todo_notify_state.json`**（箱外，看板脚本独占）。
  为什么不用 todo_box_state.json：扫描器写它是整体替换，会抹掉别人的键（已核实）。
  独立文件零耦合；丢了最坏 = 提醒重来一轮（可接受降级，不伤真源）。
  键 = 待办 ID（tid）——这正是稳定 ID 的用途。
"""
import datetime
import io
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from box_config import NOTIFY_STATE  # noqa: E402

STATE = NOTIFY_STATE


def _load_state() -> dict:
    try:
        with io.open(STATE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"schema": 1, "last_notified": {}}


def _save_state(d: dict):
    tmp = STATE + ".tmp"
    with io.open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=1)
    os.replace(tmp, STATE)


def _age_days(tid: str, last: dict):
    """距上次提醒的**自然日**数。无记录 → None（=从未提醒过）。

    ★ 为什么按"日期"不按"时刻"（2026-9-13 修，实测发现的坑）：
      若存精确时刻（如 10:00）而 cron 在 05:35 跑，
      「每天提」的账会变成每两天才提一次（19.5h < 24h → 判为未到期）。
      按自然日比较：今天提过=0 → 明天=1 → 节奏 1 天则明天该提 ✅
      这也天然防止"同一天跑两次看板重复提醒"。
    """
    ts = last.get(tid)
    if not ts:
        return None
    try:
        d = datetime.datetime.strptime(str(ts)[:10], "%Y-%m-%d").date()
        return (datetime.date.today() - d).days
    except Exception:
        return None


# 账本文件名里的日期：20260906_待办账.txt → 2026-09-06
_FN_DATE = __import__("re").compile(r"^(\d{4})(\d{2})(\d{2})_")


def _init_stamp(it: dict, box_root: str):
    """首次见到这笔账时，「上次提醒」的初值。

    ★ 为什么不用「无记录 → 立即提」（初版做法，实测有问题）：
      29 笔存量账会一次性全部冒出来 = 跟没节流一样刷屏。
      正确的语义是：**这笔账从什么时候起就躺在箱里没被系统提醒过**：
        · 账本文件名带日期 → 用那个日期（如 20260906 → 9-6 创建）
        · 否则 → 用文件 mtime
      这样老账会立刻该提（它确实积压了），新账不会。
    """
    f = str(it.get("file", "")).replace("\\", os.sep)
    m = _FN_DATE.match(os.path.basename(f))
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)} 09:00"
    try:
        p = os.path.join(box_root, f)
        return datetime.datetime.fromtimestamp(
            os.stat(p).st_mtime).strftime("%Y-%m-%d %H:%M")
    except OSError:
        return None


def due_items(real_items: list, cadence: dict, default_cadence: int,
              box_root: str = None):
    """算出「今天该提」的账。

    参数 real_items: 真待办列表（含 tid / cls / file / line / title）
    返回 (due, waiting, stats)

    ★ 单6（2026-9-17）：box_root 默认值改走 box_config 单源（原硬编码旧箱路径——
    硬编码的那份会随现实改变变成假话，且不报错，只是静默出错）。
    """
    if box_root is None:
        import box_config as _bc
        box_root = _bc.BOX
    st = _load_state()
    last = dict(st.get("last_notified") or {})

    due, waiting = [], []
    # ★ 修复 F3（外审 2026-09-28）：删除 _newly_init 死代码——该字典从未被
    #   填充（首次提醒在上方 ts is None 分支已直接处理），原 143 行的「首次」
    #   分支永不可达，原 149-157 落盘块永不执行，stats["newly_init"] 恒为 0。
    #   全库 grep 确认无外部消费者，整块摘除。
    for it in real_items:
        tid = it.get("tid")
        if not tid:
            continue                      # 无 ID 的条目不参与（降级安全）
        cls = str(it.get("cls", ""))
        cad = cadence.get(cls, default_cadence)
        title = str(it.get("title", ""))[:70]

        ts = last.get(tid)
        # 语义分层：last_notified = "上次被**展示**提醒的时间"
        #   · 有记录 → 正常算
        #   · 无记录 → **首次**：不写时间戳、直接该提一次（提完由 mark_notified 记账）
        days = _age_days(tid, last)
        # ── FIX-6（B包 C2）：脏时间戳防崩 ──
        # `_age_days` 在「有记录但解析失败」时返回 None，而外层判空用的是原始值 ts。
        # state 里只要有一条空串/坏格式时间戳（人工编辑、某次写坏），ts 非 None →
        # 走到 `cad - None` → TypeError → 看板脚本崩，且崩在 cron 里每天重复。
        # 修法：有记录但解析失败（days is None 且 ts 非空）→ 按「从未提醒」处理。
        if ts is None:
            due.append((tid, cls, title, "首次（新账，立即提一次）"))
            continue

        if days is None:
            due.append((tid, cls, title, "时间戳异常（按从未提醒处理）"))
            continue

        rest = cad - days
        if rest <= 0:
            note = f"距上次 {days:.0f} 天 / 节奏 {cad} 天"
            due.append((tid, cls, title, note))
        else:
            waiting.append((tid, cls, title, rest))

    return due, waiting, {"due": len(due), "waiting": len(waiting), "cadence": cadence}


def init_from_ledger_date(real_items: list, box_root: str, dry=False) -> dict:
    """**一次性**初始化：把存量账的「上次提醒」设为其**账本日期**。

    ★ 为什么需要（2026-9-13 实测发现）：
      系统首次上线时，29 笔存量账全都"从未提过"→ 第一次看板会把 29 笔
      全部标成"今天已提"。语义上不对——它们不是今天才出现的。
      正确做法：用账本文件名里的日期（如 20260906 → 9-6）当初始值。
      这样：老账 → 早就该提（确实积压了）；新账 → 不该提。
      跑一次即可，之后由 due_items / mark_notified 正常运转。
    """
    d = _load_state()
    last = dict(d.get("last_notified") or {})
    changed = 0
    for it in real_items:
        tid = it.get("tid")
        if not tid or tid in last:
            continue
        init = _init_stamp(it, box_root)
        if init:
            last[tid] = init
            changed += 1
    if changed and not dry:
        d["last_notified"] = last
        d["initialized_at"] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
        _save_state(d)
    return {"initialized": changed, "total": len(last)}


def mark_notified(tids: list):
    """把「已提」的 ID 打上时间戳（进入下一轮计算）。

    ★ 顺序纪律：**先渲染、后记账**——万一中途挂，宁可下次多看一遍，
      绝不"提醒被静默吞掉"。（所有者 2026-9-13 定）
    """
    if not tids:
        return
    d = _load_state()
    last = dict(d.get("last_notified") or {})
    stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    for t in tids:
        last[t] = stamp
    d["last_notified"] = last
    d["last_run"] = stamp
    _save_state(d)


def stale_items(real_items: list, box_root: str, stale_days: int) -> list:
    """停滞检测：超 N 天没动的账（按文件 mtime 算）。

    千问建议的「最小 cadence」——不加推送引擎，只在视图里提示。
    """
    out = []
    now = datetime.datetime.now().timestamp()
    for it in real_items:
        f = str(it.get("file", "")).replace("/", os.sep)
        p = os.path.join(box_root, f)
        try:
            days = (now - os.stat(p).st_mtime) / 86400.0
        except OSError:
            continue
        if days >= stale_days:
            out.append((it.get("tid"), str(it.get("cls", "")),
                        str(it.get("title", ""))[:60], days))
    out.sort(key=lambda x: -x[3])
    return out


if __name__ == "__main__":
    print("提醒节流模块 — 自检")
    d = _load_state()
    print(f"  状态文件: {STATE}")
    print(f"  last_notified 条目: {len(d.get('last_notified') or {})}")
    print(f"  last_run: {d.get('last_run', '（从未运行）')}")
