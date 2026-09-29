# -*- coding: utf-8 -*-
"""待办箱 · 稳定 ID 注册表（2026-9-13）

目标：给每一笔账一个**跨会话、跨改名、跨重写都不变**的编号。

为什么需要它（来自《总工作流》§3.5 / 千问评估报告 §3.2）：
  - 提醒节流的 last_reminded 需要键（否则"什么时候提过"没法记）
  - 账龄追踪需要跨次识别"这是同一笔账"（否则每天都是新的）
  - 文件名会变（改标题/移动分区），行号会变（插行删行），标题会变（措辞微调）

设计原则（避免重蹈"假真源"覆辙）：
  1. **注册表住箱外**（工具目录 HOME 下），不污染箱内
  2. **纯派生**——可以从箱内容重建，丢了不影响真源
  3. **匹配用多重信号**（不是单靠标题哈希）：
       a. 显式 ID 行（`<!-- tid:T7f3a9c -->`，若写了就用它——最强信号）
       b. 标题归一化哈希（去标点/空格/分类标记后比对）
       c. 文件+行号的当前快照（用于首次登记）
  4. **只增不改**：已分配的 ID 永不回收、永不复用

ID 形态：`T` + 6 位十六进制（如 T7f3a9c），与文件名方案兼容。
"""
import difflib
import hashlib
import io
import json
import os
import re
import shutil
import sys
import time

# 2026-09-18 修正：注册表位置走 box_config 单一真源——原硬编码指旧位置，
# 导致新铸 tid 落旧注册表，_registry 与守恒对不上。
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from box_config import REGISTRY_JSON as _REG_PATH, BOX as _BOX  # noqa: E402

REG = _REG_PATH
BOX = _BOX

# 相似度阈值：**覆盖率**（短串中被匹配块覆盖的比例）
# 为什么不用 SequenceMatcher.ratio()（对称比值）：
#   ratito 对"插入"惩罚过重——实测 `QC两闸升四闸情绪标签闸MOS闸` 加了
#   「（SenseVoice）」后 ratio 只有 0.76（低于阈值→误判为新条目），
#   但它显然还是同一笔账。覆盖率口径下=1.00（全串都被匹配块覆盖）。
# 加最小长度守卫：太短的标题（<8 字）不参与相似匹配，只用精确哈希——
#   否则「睡觉」/「睡觉了」这类会被误并。
SIM_COVERAGE = 0.85
SIM_MIN_LEN = 8

# 显式 ID 标记（写在条目行尾，若有人愿意标就用它——最强信号）
TID_INLINE = re.compile(r"<!--\s*tid:(T[0-9a-f]{6})\s*-->")


def _norm(title: str) -> str:
    """归一化标题用于哈希比对：去分类标记、去标点空白、统一大小写。

    比对标的是"同一笔账换了措辞"——所以只保留实义字符。
    """
    t = str(title)
    t = re.sub(r"^\s*\[[^\]]{1,6}\]\s*", "", t)      # 去行首分类标记
    t = re.sub(r"[，。！？；：、,.!?;:（）()\[\]【】<>《》\-—~`*\\/\"'“”‘’\s]", "", t)
    return t.lower()


def _hash(norm_title: str) -> str:
    return hashlib.sha256(norm_title.encode("utf-8")).hexdigest()[:12]


class RegistryCorruptError(RuntimeError):
    """注册表损坏——拒读拒写，绝不静默归零（公理四：坏掉必须响）。"""


def load() -> dict:
    """加载注册表。文件不存在 = 正常新建；存在但读不了 = 损坏 → 备份 + 抛错。

    ★ 修复 F2（外审 2026-09-28）：原实现对**任何**异常都返回空注册表，
      随后的 save() 会拿空表 os.replace 覆盖原件——坏文件连备份都没有，
      next_seq 归零、tid 从头重铸、撞上存量文件（坑 30 的灾难从侧门进来）。
      全系统唯一没被 write_safe 纪律保护的文件就是注册表自己，这里补上。
      抛错后由调用方降级（box_add 会落待写清单），原件原样保留。
    """
    if not os.path.exists(REG):
        return {"schema": 1, "next_seq": 0, "items": {}}
    try:
        with io.open(REG, encoding="utf-8") as f:
            d = json.load(f)
        # 结构最低校验（防「能解析但不是注册表」的半损坏件）
        if not isinstance(d, dict) or not isinstance(d.get("items", {}), dict):
            raise ValueError(f"结构异常: {type(d).__name__}")
        return d
    except Exception as e:
        backup = REG + ".corrupt." + time.strftime("%Y%m%d_%H%M%S")
        try:
            shutil.copy2(REG, backup)
        except Exception:
            backup = "(备份失败)"
        raise RegistryCorruptError(
            f"注册表损坏，拒绝加载（坏件已备份: {backup}）：{e!r}")


def save(d: dict):
    tmp = REG + ".tmp"
    with io.open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=1)
    os.replace(tmp, REG)


def _mint(d: dict) -> str:
    """铸造新 ID：递增序号 → 6 位十六进制。只增不回收。"""
    d["next_seq"] = int(d.get("next_seq", 0)) + 1
    return f"T{d['next_seq']:06x}"


def assign(items: list) -> dict:
    """给条目列表分配/复用 ID。返回 {条目key: tID}。

    条目 key = f"{file}:{line}"（当前快照的位置——会变，只是个临时坐标）。
    匹配顺序：
      ① 行内有显式 <!-- tid:... --> → 用它
      ② 归一化标题哈希命中已有记录 → 复用那个 ID
      ③ 都没命中 → 铸造新 ID
    """
    d = load()
    items_map = d.setdefault("items", {})
    # 建三个索引：按 h（标题哈希）、按 loc（文件:行）、以及归一化文本（相似度匹配）
    by_h = {}
    by_loc = {}
    by_norm = {}
    for tid, rec in items_map.items():
        if rec.get("h"):
            by_h.setdefault(rec["h"], []).append(tid)
        if rec.get("loc"):
            by_loc[rec["loc"]] = tid
        nt = rec.get("norm")
        if nt:
            by_norm.setdefault(nt, []).append(tid)

    result = {}
    minted = reused = 0

    for it in items:
        loc = f"{it.get('file')}:{it.get('line')}"
        title = str(it.get("title", ""))
        norm = _norm(title)

        # ① 显式标记
        m = TID_INLINE.search(str(it.get("_raw", "")) + " " + title)
        tid = m.group(1) if m else None
        how = "显式" if tid else ""

        # ② 归一化标题哈希命中（取最新的那个）
        if not tid:
            h = _hash(norm)
            cands = by_h.get(h)
            if cands:
                tid = cands[-1]
                how = "哈希"

        # ③ 相似度匹配——应对"换了措辞"（如插入括注、改标点）
        #    判据=覆盖率（短串被匹配块覆盖的比例），不是对称 ratio
        if not tid and len(norm) >= SIM_MIN_LEN:
            best_tid, best_cov = None, 0.0
            for cand_norm, tids in by_norm.items():
                if not cand_norm:
                    continue
                sm = difflib.SequenceMatcher(None, norm, cand_norm)
                matched = sum(b.size for b in sm.get_matching_blocks())
                shorter = min(len(norm), len(cand_norm))
                cov = matched / shorter if shorter else 0.0
                if cov > best_cov:
                    best_tid, best_cov = tids[-1], cov
            if best_tid and best_cov >= SIM_COVERAGE:
                tid = best_tid
                how = f"覆盖{best_cov:.2f}"

        # ④ 同一位置（文件:行）延续——应对"标题微调但位置没动"
        if not tid and loc in by_loc:
            tid = by_loc[loc]
            how = "同位"

        if not tid:
            tid = _mint(d)
            minted += 1
            how = "新铸"
        else:
            reused += 1

        h = _hash(norm)
        items_map[tid] = {
            "h": h,
            "norm": norm,
            "loc": loc,
            "title": title[:120],
            "first_seen": items_map.get(tid, {}).get("first_seen")
            or time.strftime("%Y-%m-%d %H:%M"),
            "last_seen": time.strftime("%Y-%m-%d %H:%M"),
            "n_seen": int(items_map.get(tid, {}).get("n_seen", 0)) + 1,
        }
        result[loc] = tid

    save(d)
    return result


def mint_fresh(norm_title: str, loc: str, title: str) -> str:
    """**铸造全新 tid**（写侧专用入口）——不参与相似度复用。

    为什么写侧要单独一个入口（2026-9-17 单2 实测抓出）：
      `assign()` 的②③④三条复用判据是为**扫描器追同一笔账**设计的——它要把
      「同一笔账换了措辞/挪了位置」认回来。但对**写入器**来说，语义正好相反：
      box_add 每建一个文件就是一笔**新账**，必须拿新号。
      实测事故：连造三个标题相近的新待办，第二、三个被 assign 的覆盖率匹配
      判成第一笔的「换了措辞」→ 复用 T000001 → 撞上 tid 唯一性 → 全部 rc=3 建不出来。
      结论：**同号判定归扫描器，铸号归写入器**——两者共用注册表，但入口分开。

    登记字段与 assign 对齐（h/norm/loc/title/first_seen/last_seen/n_seen），
    保证扫描器后续能接着认这笔账。
    """
    d = load()
    items_map = d.setdefault("items", {})
    tid = _mint(d)
    norm = _norm(norm_title)
    items_map[tid] = {
        "h": _hash(norm),
        "norm": norm,
        "loc": loc,
        "title": str(title)[:120],
        "first_seen": time.strftime("%Y-%m-%d %H:%M"),
        "last_seen": time.strftime("%Y-%m-%d %H:%M"),
        "n_seen": 1,
    }
    save(d)
    return tid


def stats() -> dict:
    d = load()
    return {
        "total_ids": len(d.get("items", {})),
        "next_seq": d.get("next_seq", 0),
    }


if __name__ == "__main__":
    s = stats()
    print(f"注册表：{REG}")
    print(f"  已分配 ID: {s['total_ids']}")
    print(f"  下一个序号: {s['next_seq']}（= T{s['next_seq']:06x}）")
