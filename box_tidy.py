#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
box_tidy.py — 待办箱整理器（B2 · 2026-9-12）

【职责】定期批量处理：收件箱分流 / 状态修复 / 格式巡检 / 转码 / 重建索引 / 变更日志。
对应 GTD 的「process」环节——写入是 capture，这里是 process。

【设计依据】《待办箱写入器与整理器方案 v4》§6.3

【用法】
    python box_tidy.py                 # 手动：完整报告
    python box_tidy.py --auto          # cron：无事静默，有事简报
    python box_tidy.py --apply <清单>  # 执行待确认修复清单（熔断后的人工通道）
    python box_tidy.py --selftest      # 自检

【安全设计】（全部来自三轮审查的血泪）
  · 开工前整树快照（仅文本）——批量事故的回滚单位
  · 状态修复有熔断（超 50 条 → 落清单待人工 --apply）+ TOCTOU 校验
  · 格式巡检**白名单枚举**——绝不碰普通编号列表（不可逆语义污染）
  · 转码 strict 预检——失败列人工审，不自动转
  · 重建索引走 `--trust-lock`（持锁调子进程，避免重入死锁）
  · 一切写回走 write_safe（原子 + 备份 + 回读）
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import shutil
import subprocess
import sys
import time
import zipfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from box_config import BACKUP_LOG  # noqa: E402
import box_config as bc  # noqa: E402
import write_safe  # noqa: E402
from box_lock import BoxLock, LockBusy  # noqa: E402

SH = datetime.timezone(datetime.timedelta(hours=8))


def now_str() -> str:
    return datetime.datetime.now(SH).strftime("%Y-%m-%d %H:%M:%S")


# ══════════════════════════════════════════════════════
# 编码安全的读/写回（2026-9-12 实测事故修正）
# ══════════════════════════════════════════════════════
# 事故：收件箱分流测试读 GBK 账本用 `utf-8/replace` → 中文全成占位符 →
# 又按 UTF-8 写回 → **原内容被毁**（幸好 write_safe 写前备份救回）。
# 铁律：**读什么编码，写回什么编码**。无法可靠解码的文件禁止写回。
# ★ FIX-12（A包 A1+D2+D7）：本函数已收敛为 write_safe.read_text_strict 的薄包装——
#   三工具（扫描器/写入器/整理器）共用同一实现，杜绝「修一处漏两处」。
def read_text_strict(p: str) -> tuple[str, str, bool]:
    """读文件 → (文本, 编码, lossy)。薄包装：真源在 write_safe.read_text_strict。"""
    return write_safe.read_text_strict(p)


def write_back_enc(p: str, text: str, enc: str) -> bool:
    """按**原编码**写回。失败（如 emoji 编不进 GBK）→ False 且文件不动。

    write_safe 的编码预检保证：编不了就抛，此时文件还没动。
    """
    try:
        write_safe.safe_write(p, text, encoding=enc)
        return True
    except write_safe.WriteSafeError:
        return False


# ══════════════════════════════════════════════════════
# 变更日志记录器（schema：时间/工具/文件/动作/前后/置信级）
# ══════════════════════════════════════════════════════
class ChangeLog:
    def __init__(self):
        self.entries: list[str] = []

    def add(self, file: str, action: str, before: str = "", after: str = "",
            confidence: str = "high"):
        rel = os.path.relpath(file, bc.BOX) if file.startswith(bc.BOX) else file
        line = f"  · [{confidence}] {rel} | {action}"
        if before or after:
            line += f" | {before[:40]!r} → {after[:40]!r}" if after else f" | {before[:40]!r}"
        self.entries.append(line)

    def flush(self, trigger: str = "manual"):
        if not self.entries:
            return
        header = f"\n{now_str()}  box_tidy（{trigger}）\n"
        try:
            write_safe.append_safe(bc.CHANGELOG_MD, header + "\n".join(self.entries) + "\n")
        except Exception:
            pass


# ══════════════════════════════════════════════════════
# 1. 整树快照（仅文本扩展名；失败即中止）
# ══════════════════════════════════════════════════════
def make_tree_snapshot(log: ChangeLog) -> str | None:
    """开工前整树快照。返回快照路径或 None（失败）。

    范围：整树中 .txt/.md（工具的事故半径本就只有文本）
    失败语义：**中止本次整理**（方案 🟡-2——批量操作无回滚不裸奔）

    v2（单5）：快照落**新家** `_snapshots\\`（随箱子走，箱内独立区）。
    """
    snap_root = bc.SNAPSHOT_DIR
    os.makedirs(snap_root, exist_ok=True)
    stamp = datetime.datetime.now(SH).strftime("%Y%m%d_%H%M%S")
    zip_path = os.path.join(snap_root, f"tree_{stamp}.zip")

    files = []
    total = 0
    for root, dirs, fns in os.walk(bc.BOX):
        dirs[:] = [d for d in dirs if d not in ("_snapshots", "_快照", "00_收件箱",
                                                 "_registry", "_done")]
        for fn in fns:
            if not fn.lower().endswith(bc.TEXT_EXTS):
                continue
            p = os.path.join(root, fn)
            try:
                sz = os.path.getsize(p)
            except Exception:
                continue
            files.append((p, os.path.relpath(p, bc.BOX)))
            total += sz

    # 体积保护：超阈值降级为 manifest 模式
    if total > bc.TREE_SNAPSHOT_MAX_MB * 1024 * 1024:
        mani = zip_path.replace(".zip", "_manifest.txt")
        with open(mani, "w", encoding="utf-8") as f:
            f.write(f"# 树快照降级为 manifest（总体积 {total/1e6:.1f}MB > {bc.TREE_SNAPSHOT_MAX_MB}MB）\n")
            for p, rel in files:
                f.write(f"{rel}\t{os.path.getsize(p)}\n")
        log.add(mani, "整树 manifest（体积降级）")
        return mani

    try:
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
            for p, rel in files:
                z.write(p, rel)
    except Exception as e:
        print(f"✗ 树快照失败：{e}")
        return None

    # 清理超出保留份数的旧快照
    try:
        snaps = sorted([f for f in os.listdir(snap_root)
                        if f.startswith("tree_") and f.endswith((".zip", "_manifest.txt"))])
        for old in snaps[:-bc.TREE_SNAPSHOT_KEEP]:
            try:
                os.remove(os.path.join(snap_root, old))
            except Exception:
                pass
    except Exception:
        pass

    log.add(zip_path, f"整树快照（{len(files)} 个文本文件）")
    return zip_path


# ══════════════════════════════════════════════════════
# 2. 收件箱分流
# ══════════════════════════════════════════════════════
PREFIX_MAP = {
    "[待办]": ("待办", "04"),      # 默认进数据区（保守：人工可再归位）
    "[完成]": ("完成", "04"),
    "[事件]": ("事件", "05"),
    "[笔记]": ("笔记", "05"),
}


def process_inbox(log: ChangeLog, dry: bool = False) -> int:
    """分流 = **文件移动**（收件箱 → 领域分区，rename 保 tid）。

    v2（2026-9-17 单5）：输入面从「账本里的行」改成「箱里的文件」——
      · 协议文件（OPEN_/DONE_ 五段）→ 按它自带的节奏/标题归位到合适分区
      · 非协议文件（用户手写、写错前缀）→ **不动、不静默**：报告出来交人工
    修复动作纪律不变：只移动/改名/追加，禁止删除与内容改写。
    """
    if not os.path.isdir(bc.INBOX):
        return 0
    n = 0
    for fn in sorted(os.listdir(bc.INBOX)):
        p = os.path.join(bc.INBOX, fn)
        if not os.path.isfile(p) or not fn.lower().endswith((".txt", ".md")):
            continue
        pr = bc.parse_filename(fn)
        if not pr:
            # ── 人写协议（方案 §1.2）：写错 → 标「待整理」，不静默，不擅自改名 ──
            log.add(p, "不合文件名协议 → 留收件箱待人工处置（扫描器已列「待整理」）",
                    confidence="low")
            continue
        # 归位目标分区：按文件体上下文里的原区名优先，其次按 tid/节奏兜底 04
        zid = _zone_from_body(p) or "04"
        zdir = bc.ZONES.get(zid, bc.ZONES["05"])
        dst = os.path.join(zdir, fn)
        if os.path.abspath(dst) == os.path.abspath(p):
            continue
        if os.path.exists(dst):                       # 同名不覆盖（铁律：不删除）
            log.add(p, f"归位受阻：目标同名已存在 {os.path.relpath(dst, bc.BOX)}", confidence="low")
            continue
        if not dry:
            try:
                os.replace(p, dst)                    # 同卷 rename，NTFS 原子，tid 不变
            except Exception as e:
                log.add(p, f"归位失败: {e}", confidence="low")
                continue
        log.add(p, f"归位 → {os.path.relpath(dst, bc.BOX)}（节奏 {pr['rhythm']}）")
        n += 1
    return n



def _zone_from_body(p: str) -> str | None:
    """从文件体的「上下文: <旧区名>\\文件:行」指针推出新家分区号。

    旧区名 → 分区号零翻译（bc.LEGACY_ZONE_TO_NEW）。读不出 → None（调用方兜底）。
    """
    try:
        txt, _enc, lossy = read_text_strict(p)
        if lossy:
            return None
    except Exception:
        return None
    for old_name, zid in bc.LEGACY_ZONE_TO_NEW.items():
        if old_name in txt:
            return zid
    return None


def archive_done(log: ChangeLog, dry: bool = False) -> int:
    """DONE 归档 = rename OPEN_→DONE_（已在 DONE_ 则跳过）+ 移 `_done/YYYY-MM/`。

    触发条件：文件体含 `done: true` 标记 或 文件名已是 DONE_ 前缀（由销账动作产生）。
    纪律不变：只改名/移动，禁止删除与内容改写。
    """
    n = 0
    month = datetime.datetime.now(SH).strftime("%Y-%m")
    dst_dir = os.path.join(bc.DONE_DIR, month)
    for zdir in list(bc.ZONES.values()):
        if not os.path.isdir(zdir):
            continue
        for fn in sorted(os.listdir(zdir)):
            pr = bc.parse_filename(fn)
            if not pr:
                continue
            p = os.path.join(zdir, fn)
            # 2026-09-17 v3修复：docstring 承诺的 done: true 触发路径此前断链——
            # 文件体带 done: true 的 OPEN_ 文件先 rename 成 DONE_（同卷原子），
            # 再走下方既有归档移动。不改写内容（销账证据块原样保留）。
            if pr["prefix"] == "OPEN":
                try:
                    body = open(p, encoding="utf-8").read()
                except Exception:
                    body = ""
                if re.search(r"(?m)^\s*done\s*[:：]\s*true\s*$", body, re.I):
                    done_fn = "DONE_" + fn[len("OPEN_"):]
                    done_p = os.path.join(zdir, done_fn)
                    if os.path.exists(done_p):
                        log.add(p, "销账受阻：DONE_ 同名已存在", confidence="low")
                        continue
                    if not dry:
                        os.replace(p, done_p)
                    log.add(p, f"done: true → rename {done_fn}")
                    fn, p, pr = done_fn, done_p, dict(pr, prefix="DONE")
            if pr["prefix"] != "DONE":
                continue
            dst = os.path.join(dst_dir, fn)
            if os.path.exists(dst):
                log.add(p, f"归档受阻：目标同名已存在 {os.path.relpath(dst, bc.BOX)}",
                        confidence="low")
                continue
            if not dry:
                try:
                    os.makedirs(dst_dir, exist_ok=True)
                    os.replace(p, dst)
                except Exception as e:
                    log.add(p, f"归档失败: {e}", confidence="low")
                    continue
            log.add(p, f"归档 → {os.path.relpath(dst, bc.BOX)}")
            n += 1
    return n


# ══════════════════════════════════════════════════════
# 3. 状态修复（数据正确性）+ 熔断
# ══════════════════════════════════════════════════════
#: 高置信：文件体内 `done: true` 标记 或 `勾选框 [x]`（v2：一文件一笔，行协议已退役）
HIGH_CONF_RE = re.compile(r"^(\s*done\s*[:：]\s*true\s*)$|^\s*[-*]\s*\[[xX]\]", re.I)
#: 低置信：正文自述完成标记（但形态不标准）—— 只报告
LOW_CONF_RE = re.compile(r"(✅|~~)")

#: 旧协议清单文件（行协议时代的格式巡检白名单）——v2 只作只读巡检对象，不再是账
PREFIX_MAP = {
    "[待办]": ("待办", "04"),      # 旧收件箱前缀（历史兼容；新协议已不用）
    "[完成]": ("完成", "04"),
    "[事件]": ("事件", "05"),
    "[笔记]": ("笔记", "05"),
}


def _status_fix_files() -> list[str]:
    """状态修复的**文件白名单**（2026-9-12 修正——防误伤文档正文）。

    事故预演（kimi-k3 预言 + 实测确认）：不加白名单时，扫描会命中
    `_方案_*.md`、`_审查_*.md` 等**文档正文里的编号说明行**（如
    「3. ~~删除线~~ 与 ✅ 中缀的语义确认」——那是讨论格式的正文，不是待办！），
    把它们改成勾选框 = **不可逆语义污染**。

    规则（v2 单5）：只修**协议待办文件**（`OPEN_/DONE_` 五段）与总账本（`_总账本.md`）。
    旧协议 `*_待办账.txt` 已随旧箱封存，新家不再有。
    """
    files = []
    for d in list(bc.ZONES.values()) + [bc.INBOX]:
        if not os.path.isdir(d):
            continue
        for fn in sorted(os.listdir(d)):
            if bc.parse_filename(fn) or fn == "_总账本.md":
                files.append(os.path.join(d, fn))
    return files


def scan_status_fix(log: ChangeLog, dry: bool = False):
    """扫描待修的状态条目。返回 (高置信数, 低置信数, 高置信清单)。

    ⚠ 2026-9-12 修正两处：
    ① **只扫白名单文件**（待办账 + 总账本）——文档/报告的正文编号行不得进入
       修复范围（kimi-k3 预言的不可逆污染）。
    ② **严格按原编码读**——与 apply_status_fix 的行比对（TOCTOU）保持一致；
       用 utf-8/replace 读 GBK 会错位，导致修复永远无法命中。
    """
    high, low = [], []
    for p in _status_fix_files():
        try:
            txt, enc, lossy = read_text_strict(p)
            if lossy:
                continue                       # 编码不明 → 不列为可修（保守）
        except Exception:
            continue
        for i, ln in enumerate(txt.splitlines(), 1):
            s = ln.strip()
            if s.startswith("#") or s.startswith(">"):
                continue
            if HIGH_CONF_RE.match(ln):
                high.append((p, i, ln))
            elif LOW_CONF_RE.match(ln):
                low.append((p, i, ln))
    return len(high), len(low), high


def apply_status_fix(items: list, log: ChangeLog, dry: bool = False) -> int:
    """应用高置信修复：`1. [x] 内容` → `- [x] 内容`。返回修复数。

    dry=True：只预演（不改盘、日志标「预演」、返回 0）。
    ⚠ 编码安全：按原编码读、原编码写回；解不开的跳过（2026-9-12 事故修正）。
    """
    # 按文件分组
    by_file: dict[str, list] = {}
    for p, i, ln in items:
        by_file.setdefault(p, []).append((i, ln))

    n = 0
    for p, entries in by_file.items():
        try:
            txt, enc, lossy = read_text_strict(p)
            if lossy:
                log.add(p, "状态修复跳过（编码不明，拒写）", confidence="low")
                continue
        except Exception:
            continue
        lines = txt.splitlines(keepends=True)
        changed = False
        for i, old_ln in entries:
            if i - 1 >= len(lines):
                continue
            cur = lines[i - 1].rstrip("\r\n")
            # TOCTOU 防护：当前行必须仍等于记录时的那行
            if cur != old_ln:
                log.add(p, f"行 {i} 已变动（TOCTOU 保护，跳过）", confidence="low")
                continue
            m = HIGH_CONF_RE.match(cur)
            if not m:
                continue
            # 2026-09-17 v3修复：旧协议的行改写（- [x] 重写）在文件协议下已无意义——
            # 新协议的销账=done: true 标记→archive_done 归档（rename+移动），不改写内容。
            # 此处只记录命中，不再重写行（原 m.group(3) 是行协议残留，且正则无组3会崩）。
            if dry:
                log.add(p, f"预演：状态确认（行 {i}，done 标记待归档）", before=cur, confidence="dry")
                continue
            changed = False   # 不改写；归档交给 archive_done
            n += 1
            log.add(p, f"状态确认（行 {i}，done 标记待归档）", before=cur, confidence="high")
        if changed and not dry:
            new_txt = "".join(lines)
            try:
                write_safe.safe_write(p, new_txt, encoding=enc)
            except write_safe.WriteSafeError as e:
                log.add(p, f"修复写回失败（原样保留）: {str(e)[:60]}", confidence="low")
    return n


def write_fix_list(high: list, log: ChangeLog):
    """熔断：把待修清单落盘待人工确认（方案 🟡-3）。"""
    lines = [f"# 待确认修复清单（{now_str()}）", "",
             f"> 状态修复熔断触发：高置信条目 {len(high)} 条超过阈值 {bc.FIX_FUSE_THRESHOLD}。",
             "> 确认后执行：`python box_tidy.py --apply <本文件>`", "",
             "| 文件 | 行号 | 旧行 |", "|---|---|---|"]
    for p, i, ln in high:
        rel = os.path.relpath(p, bc.BOX)
        lines.append(f"| {rel} | {i} | `{ln.strip()[:80]}` |")
    try:
        write_safe.safe_write(bc.PENDING_FIX_MD, "\n".join(lines) + "\n")
        log.add(bc.PENDING_FIX_MD, f"熔断：落待确认清单（{len(high)} 条）")
    except Exception:
        pass


# ══════════════════════════════════════════════════════
# 4. 格式巡检（仅白名单枚举文件）
# ══════════════════════════════════════════════════════
#: 坏格式：勾选框位置错 —— `- [分类] [ ] 内容` 之类
BAD_FORMAT_RE = re.compile(
    r"^\s*[-*]\s*\[(" + "|".join(bc.RHYTHMS) + r")\]\s*(\[[ xX]\])\s*(.*)$")


def format_patrol(log: ChangeLog, dry: bool = False) -> int:
    """白名单文件内的格式巡检。返回修复数。

    ⚠ 编码安全：按原编码读、原编码写回（2026-9-12 事故修正）。
    """
    n = 0
    for rel in bc.TIDY_WHITELIST:
        p = os.path.join(bc.BOX, rel)
        if not os.path.exists(p):
            continue
        try:
            txt, enc, lossy = read_text_strict(p)
            if lossy:
                log.add(p, "格式巡检跳过（编码不明，拒写）", confidence="low")
                continue
        except Exception:
            continue
        lines = txt.splitlines(keepends=True)
        changed = False
        for i, ln in enumerate(lines):
            cur = ln.rstrip("\r\n")
            m = BAD_FORMAT_RE.match(cur)
            if m:
                cls, box, rest = m.groups()
                new_ln = f"- {box} [{cls}] {rest}".rstrip()
                lines[i] = new_ln + "\n"
                changed = True
                n += 1
                log.add(p, f"格式修复（行 {i+1}）", before=cur, after=new_ln)
        if changed and not dry:
            try:
                write_safe.safe_write(p, "".join(lines), encoding=enc)
            except write_safe.WriteSafeError as e:
                log.add(p, f"格式修复写回失败（原样保留）: {str(e)[:60]}", confidence="low")
    return n


# ══════════════════════════════════════════════════════
# 5. 转码（GBK → UTF-8，strict 预检）
# ══════════════════════════════════════════════════════
def transcode_backup_path(file_path: str, stamp: str) -> str:
    """转码备份路径——**按相对路径镜像**（B3 预检：防同名文件互覆备份）。

    原实现 `{fn}.gbk_original.{秒级戳}`：箱内有 5 个同名 `_说明.txt`（各区一份），
    同一秒内转码会撞名 → 后写覆盖先写 → 回滚时丢备份。
    现改为 `todo_snapshots/transcode_<批次戳>/<相对路径>`：结构镜像、天然唯一；
    回滚 = 把该目录整树拷回箱内。
    """
    rel = os.path.relpath(file_path, bc.BOX)
    return os.path.join(bc.SNAPSHOT_DIR, f"transcode_{stamp}", rel)


def transcode(log: ChangeLog, dry: bool = False, limit: int | None = None) -> tuple[int, int]:
    """GBK → UTF-8 转码。返回 (转码数, 列入人工审数)。

    ⚠ 2026-9-12：**默认不在 tidy 里自动执行**（转码是 B3 批，方案要求
    「首跑分批人工护送」）。tidy 只**报告**待转码数量；真正执行走
    `box_tidy.py --transcode`（B3 专用入口）。
    备份：`todo_snapshots/transcode_<批次戳>/`（相对路径镜像；回滚=整树拷回）。
    """
    n, manual = 0, 0
    stamp = datetime.datetime.now(SH).strftime("%Y%m%d_%H%M%S")   # 批次戳（一次运行一个）
    bak_logged = False
    for root, dirs, fns in os.walk(bc.BOX):
        dirs[:] = [d for d in dirs if d not in ("_快照", "00_收件箱")]
        for fn in fns:
            if not fn.lower().endswith(bc.TEXT_EXTS):
                continue
            p = os.path.join(root, fn)
            raw = open(p, "rb").read()
            # 已是 UTF-8？
            try:
                raw.decode("utf-8")
                continue
            except Exception:
                pass
            # strict GBK 预检
            try:
                text = raw.decode("gbk")
            except Exception as e:
                manual += 1
                log.add(p, f"转码预检失败（列人工审）: {str(e)[:50]}", confidence="low")
                continue
            if dry:
                n += 1
                continue
            try:
                # 备份 GBK 原文件（相对路径镜像 → 同名不互覆；一批一个目录）
                bak = transcode_backup_path(p, stamp)
                os.makedirs(os.path.dirname(bak), exist_ok=True)
                shutil.copy2(p, bak)
                if not bak_logged:
                    log.add(bak, "转码批备份目录（回滚=整树拷回）")
                    bak_logged = True
                write_safe.safe_write(p, text, backup=False)
                n += 1
                log.add(p, "转码 GBK → UTF-8")
            except write_safe.WriteSafeError as e:
                log.add(p, f"转码写回失败: {e}", confidence="low")
            if limit and n >= limit:
                return n, manual
    return n, manual


def count_transcode_candidates() -> tuple[int, int]:
    """统计待转码 (可转数, 人工审数)——只读不动手。"""
    n, manual = 0, 0
    for root, dirs, fns in os.walk(bc.BOX):
        dirs[:] = [d for d in dirs if d not in ("_快照", "00_收件箱")]
        for fn in fns:
            if not fn.lower().endswith(bc.TEXT_EXTS):
                continue
            raw = open(os.path.join(root, fn), "rb").read()
            try:
                raw.decode("utf-8")
                continue
            except Exception:
                pass
            try:
                raw.decode("gbk")
                n += 1
            except Exception:
                manual += 1
    return n, manual


# ══════════════════════════════════════════════════════
# 6. 重建索引（走信任链模式——持锁调子进程）
# ══════════════════════════════════════════════════════
def rebuild_index(log: ChangeLog) -> bool:
    """调用扫描器重建索引。**必须在持锁状态下调用**（--trust-lock）。"""
    scan = os.path.join(bc.HOME, "todo_box_scan.py")
    try:
        r = subprocess.run([sys.executable, scan, "--trust-lock", "--auto"],
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", cwd=bc.HOME, timeout=300)
        ok = r.returncode == 0
        if not ok:
            log.add(scan, f"索引重建失败 exit={r.returncode}", confidence="low")
        return ok
    except Exception as e:
        log.add(scan, f"索引重建异常: {e}", confidence="low")
        return False


# ══════════════════════════════════════════════════════
# 7. 巡检：可执行文件 + 冲突副本
# ══════════════════════════════════════════════════════
def patrol_executables(log: ChangeLog) -> list[str]:
    """箱内可执行文件巡检（响应所有者「手滑点到」关切）。只报告，不删。"""
    found = []
    for root, dirs, fns in os.walk(bc.BOX):
        for fn in fns:
            if fn.lower().endswith(bc.EXECUTABLE_EXTS):
                found.append(os.path.relpath(os.path.join(root, fn), bc.BOX))
    return found


def patrol_conflicts(log: ChangeLog) -> list[str]:
    """OneDrive 冲突副本巡检。只报告，绝不自动合并。"""
    found = []
    pats = [re.compile(p) for p in bc.CONFLICT_PATTERNS]
    for root, dirs, fns in os.walk(bc.BOX):
        for fn in fns:
            for pat in pats:
                if pat.search(fn) and "待办账" not in fn:
                    found.append(os.path.relpath(os.path.join(root, fn), bc.BOX))
                    break
    return found


# ══════════════════════════════════════════════════════
# 8. 运行状态（心跳）+ 运行日志 + 周报（B2 · 2026-9-12）
# ══════════════════════════════════════════════════════
def load_state() -> dict:
    """读整理器状态文件。损坏时返回初始结构（宁丢历史不卡壳）。"""
    try:
        st = json.load(open(bc.TIDY_STATE_JSON, encoding="utf-8"))
        if isinstance(st, dict):
            return st
    except Exception:
        pass
    return {"runs": []}


def save_state(state: dict):
    """原子写状态文件（保留最近 30 条运行记录）。"""
    try:
        state["runs"] = state.get("runs", [])[-30:]
        tmp = bc.TIDY_STATE_JSON + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=1)
        os.replace(tmp, bc.TIDY_STATE_JSON)
    except Exception:
        pass


def record_run(trigger: str, alerts: list, hb_stalled: bool, exit_code: int):
    """记录一次运行 = **心跳**（last_run 时间戳 + 近期运行）。

    静默型自动化的命门：没有心跳，坏掉无人知。
    runs 是心跳的持久载体，周报是心跳的**消费者**（会读它并报告异常）。
    """
    state = load_state()
    state["last_run"] = now_str()
    state["last_exit"] = exit_code
    state["last_alerts"] = alerts[:10]
    state["runs"] = state.get("runs", [])
    state["runs"].append({
        "ts": datetime.datetime.now(SH).isoformat(timespec="seconds"),
        "trigger": trigger,
        "alerts": len(alerts),
        "hb_stalled": bool(hb_stalled),
    })
    save_state(state)


def append_runlog(trigger: str, report: list, exit_code: int):
    """每次运行追加一行（审计轨迹；append-only 无截断窗口）。"""
    brief = "; ".join(report)[:200] if report else "(静默)"
    line = f"{now_str()} | {trigger} | exit={exit_code} | {brief}\n"
    try:
        write_safe.append_safe(bc.TIDY_RUNLOG, line)
    except Exception:
        pass


def count_backup_failures(days: int = 7) -> tuple[int, int]:
    """统计近 N 天备份日志的 (失败数, 总条目数)。数据源：PutputBackup/backup_log.txt。"""
    log_p = BACKUP_LOG
    if not os.path.exists(log_p):
        return 0, 0
    cutoff = datetime.datetime.now(SH) - datetime.timedelta(days=days)
    fail = total = 0
    try:
        for ln in open(log_p, encoding="utf-8", errors="replace").read().splitlines():
            m = re.match(r"^(\d{4}-\d{2}-\d{2}) (\d{2}:\d{2})\s+(OK|FAIL|SKIP|SNAP|=+)", ln)
            if not m:
                continue
            try:
                ts = datetime.datetime.strptime(f"{m.group(1)} {m.group(2)}", "%Y-%m-%d %H:%M")
                ts = ts.replace(tzinfo=SH)
            except Exception:
                continue
            if ts < cutoff:
                continue
            if m.group(3) == "FAIL":
                fail += 1
            if m.group(3) in ("OK", "FAIL"):
                total += 1
    except Exception:
        pass
    return fail, total


def summarize_cron_runs(days: int = 7) -> tuple[int, int, list]:
    """统计近 N 天 cron 执行情况。返回 (总次数, 失败数, 未跑任务名列表)。

    数据源：hermes cron 的 executions.db + jobs.json（只读）。
    「各 cron 执行情况」是周报固定字段（方案 §六.10）。
    """
    import sqlite3
    if not os.path.exists(bc.HERMES_CRON_DB):
        return 0, 0, []
    since = (datetime.datetime.now(SH) - datetime.timedelta(days=days)).strftime("%Y-%m-%d")
    try:
        con = sqlite3.connect(f"file:{bc.HERMES_CRON_DB}?mode=ro", uri=True)
        rows = con.execute(
            "SELECT job_id, COUNT(*), "
            "SUM(CASE WHEN status!='completed' THEN 1 ELSE 0 END) "
            "FROM executions WHERE started_at >= ? GROUP BY job_id", (since,)).fetchall()
        con.close()
    except Exception:
        return 0, 0, []
    total = sum(r[1] or 0 for r in rows)
    fail = sum(r[2] or 0 for r in rows)
    ran = {r[0] for r in rows if (r[1] or 0) > 0}
    missing = []
    try:
        import json as _json
        jobs = _json.load(open(bc.HERMES_CRON_JOBS, encoding="utf-8")).get("jobs", [])
        for j in jobs:
            if j.get("enabled") and ("every 15m" not in str(j.get("schedule"))):
                # 高频任务（15分钟级）不算「没跑」——它们按天有记录
                if j.get("id") not in ran:
                    miss_days = None
                    missing.append(j.get("name") or j.get("id"))
    except Exception:
        pass
    return total, fail, missing


def generate_weekly_report() -> str:
    """生成周报（≤5 行核心 + 异常才展开）。周一由 tidy --auto 调用。

    固定字段（方案 §六.10）：
      · 本周备份失败次数
      · 心跳停摆次数
      · 各 cron 执行情况
    另加：整理器自身运行情况（心跳消费者——如果它自己没跑，周报会大声说）。
    """
    state = load_state()
    runs = state.get("runs", [])
    cutoff = datetime.datetime.now(SH) - datetime.timedelta(days=7)

    recent = []
    for r in runs:
        try:
            ts = datetime.datetime.fromisoformat(r.get("ts", ""))
            if ts >= cutoff:
                recent.append(r)
        except Exception:
            continue

    hb_stalled = sum(1 for r in recent if r.get("hb_stalled"))
    bak_fail, bak_total = count_backup_failures(7)
    cron_total, cron_fail, cron_missing = summarize_cron_runs(7)

    # 整理器自身是否按时跑（每日一次 → 7 天应有 ~7 次；<5 次可疑）
    n_runs = len(recent)
    self_warn = ""
    if n_runs < 5:
        self_warn = f"⚠ 整理器自身近 7 天只跑了 {n_runs} 次（应为每日一次）"

    lines = [
        f"【待办箱 · 周报】{datetime.datetime.now(SH).strftime('%Y-%m-%d')}",
        f"· 整理器：近 7 天运行 {n_runs} 次 ｜ 心跳停摆 {hb_stalled} 次",
        f"· 备份：近 7 天 {bak_total} 项 ｜ 失败 {bak_fail} 次",
        f"· cron：近 7 天 {cron_total} 次执行 ｜ 失败 {cron_fail} 次",
    ]
    problems = []
    if cron_missing:
        problems.append(f"· ⚠ 近 7 天未见执行：{'、'.join(cron_missing[:5])}")
    if self_warn:
        problems.append(self_warn)
    if hb_stalled:
        problems.append(f"· ⚠ 锁心跳曾停摆 {hb_stalled} 次（详见 _变更日志）")
    if bak_fail:
        problems.append(f"· ⚠ 备份失败 {bak_fail} 次（看 PutputBackup/backup_log.txt）")
    if problems:
        lines.extend(problems)

    # 落盘固定文件（供 agent/所有者查阅）
    try:
        write_safe.safe_write(bc.WEEKLY_REPORT_MD, "\n".join(lines) + "\n")
    except Exception:
        pass
    return "\n".join(lines)


def archive_changelog(log: ChangeLog) -> int:
    """变更日志超 500 行 → 归档到箱外（保留最近 100 行）。返回归档行数。"""
    if not os.path.exists(bc.CHANGELOG_MD):
        return 0
    try:
        raw = open(bc.CHANGELOG_MD, encoding="utf-8").read()
    except Exception:
        return 0
    lines = raw.splitlines()
    if len(lines) <= bc.CHANGELOG_ARCHIVE_LINES:
        return 0
    keep = lines[-100:]
    arch = lines[:-100]
    try:
        os.makedirs(bc.CHANGELOG_ARCHIVE_DIR, exist_ok=True)
        stamp = datetime.datetime.now(SH).strftime("%Y%m%d_%H%M%S")
        write_safe.safe_write(os.path.join(bc.CHANGELOG_ARCHIVE_DIR, f"changelog_{stamp}.md"),
                              "\n".join(arch) + "\n", backup=False)
        write_safe.safe_write(bc.CHANGELOG_MD, "\n".join(keep) + "\n", backup=False)
        log.add(bc.CHANGELOG_MD, f"归档 {len(arch)} 行 → {bc.CHANGELOG_ARCHIVE_DIR}")
        return len(arch)
    except Exception:
        return 0


# ══════════════════════════════════════════════════════
# 主流程
# ══════════════════════════════════════════════════════
def tidy(auto: bool = False, dry: bool = False) -> int:
    log = ChangeLog()
    report = []
    alerts = []          # 「有事」清单——只有它才会打破静默
    hb_stalled = False
    exit_code = 0

    # 静默规则（2026-9-12 修）：例行事项（树快照/待转码提示）**不算有事**。
    # 只有异常（快照失败/索引失败/熔断/箱内可执行/冲突副本/修复动作）才出声。

    try:
        lk_ctx = BoxLock("box_tidy", quiet=auto)
        lk_ctx.acquire()
    except LockBusy as e:
        print(f"✗ {e}")
        record_run("auto" if auto else "manual", [f"抢锁失败: {e}"], False, 4)
        return 4

    try:
        # 1. 整树快照（失败即中止）
        snap = make_tree_snapshot(log)
        if snap is None:
            print("✗ 树快照失败——中止本次整理（不裸奔）")
            log.flush("aborted-no-snapshot")
            record_run("auto" if auto else "manual", ["树快照失败——中止"], False, 5)
            return 5

        # 2. 收件箱分流（有动作=有事）
        n_inbox = process_inbox(log, dry)
        if n_inbox:
            report.append(f"收件箱分流 {n_inbox} 件")
            alerts.append(f"收件箱分流 {n_inbox} 件")

        # 2b. DONE 归档（v2 单5：DONE_ 文件 → _done\YYYY-MM\）
        n_done = archive_done(log, dry)
        if n_done:
            report.append(f"DONE 归档 {n_done} 件 → _done\\{datetime.datetime.now(SH).strftime('%Y-%m')}\\")
            alerts.append(f"DONE 归档 {n_done} 件")

        # 3. 状态修复（含熔断）
        cnt = scan_status_fix(log, dry)
        high_n, low_n, high_items = cnt
        if high_n:
            if high_n > bc.FIX_FUSE_THRESHOLD:
                write_fix_list(high_items, log)
                report.append(f"⚠ 状态修复熔断：{high_n} 条超阈值 {bc.FIX_FUSE_THRESHOLD}，已落清单待 --apply")
                alerts.append(f"状态修复熔断：{high_n} 条待人工 --apply")
            else:
                fixed = apply_status_fix(high_items, log, dry)
                report.append(f"状态修复 {fixed} 条（低置信 {low_n} 条仅报告）")
                alerts.append(f"状态修复 {fixed} 条" + (f"（低置信 {low_n} 条待人工看）" if low_n else ""))

        # 4. 格式巡检
        n_fmt = format_patrol(log, dry)
        if n_fmt:
            report.append(f"格式修复 {n_fmt} 处")
            alerts.append(f"格式修复 {n_fmt} 处")

        # 5. 转码（B2 只报告；执行走 --transcode = B3 批，需人工护送）
        n_tr, n_manual = count_transcode_candidates()
        if n_tr or n_manual:
            report.append(f"待转码 {n_tr} 个" + (f"（{n_manual} 个预检失败待人工审）" if n_manual else "")
                          + " —— 执行走 box_tidy.py --transcode（B3 批）")
            # 常态（B3 未实施），不算「有事」；但预检失败要出声
            if n_manual:
                alerts.append(f"转码预检失败 {n_manual} 个待人工审")

        # 6. 重建索引（信任链）
        ok_idx = rebuild_index(log)
        if not ok_idx:
            report.append("⚠ 索引重建失败")
            alerts.append("索引重建失败")

        # 7. 巡检
        exes = patrol_executables(log)
        if exes:
            report.append(f"⚠ 箱内发现可执行文件 {len(exes)} 个: {', '.join(exes[:3])}")
            alerts.append(f"箱内可执行文件 {len(exes)} 个")
        confs = patrol_conflicts(log)
        if confs:
            report.append(f"⚠ 疑似 OneDrive 冲突副本 {len(confs)} 个: {', '.join(confs[:3])}")
            alerts.append(f"冲突副本 {len(confs)} 个")

        # 8. 变更日志归档（超 500 行）
        n_arch = archive_changelog(log)
        if n_arch:
            report.append(f"变更日志归档 {n_arch} 行")
            alerts.append(f"变更日志归档 {n_arch} 行")

        # 9. 周报（周一生成——心跳消费者）
        if datetime.datetime.now(SH).weekday() == 0:
            wk = generate_weekly_report()
            report.append("周一：已生成周报（todo_tidy_weekly.md）")
            # 周报本身要投递（周一摘要）——直接打印，不走 alerts 前缀
            alerts.append("WEEKLY_REPORT\n" + wk)

        hb_stalled = lk_ctx.heartbeat_stalled
    finally:
        try:
            lk_ctx.release()
        except Exception:
            pass

    log.flush("auto" if auto else "manual")
    record_run("auto" if auto else "manual", alerts, hb_stalled, exit_code)
    append_runlog("auto" if auto else "manual", report, exit_code)

    # 输出
    if auto:
        if not alerts:
            return 0                                    # 无事 → 静默（空 stdout）
        print("【待办箱整理 · 有事】")
        for r in alerts:
            if r.startswith("WEEKLY_REPORT\n"):
                print(r[len("WEEKLY_REPORT\n"):])       # 周报整段打印
            else:
                print(" ·", r)
        return 0
    else:
        print(f"=== box_tidy 整理完成（{now_str()}）===")
        if report:
            for r in report:
                print(" ·", r)
        else:
            print(" · 无事（一切整洁）")
        # 2026-09-18 看板即时刷新（用户报告：白天销账看板不动，要等次日05:35）：
        # 整理/归档后顺手重生成看板。dry 模式不刷（没动账就不动派生视图）。
        # ★ 2026-09-28 修：看板只读索引——先扫描重建索引再生成（box_tidy 主流程
        #   已持锁，子进程扫描走 --trust-lock 信任链，与既有索引重建同款）。
        if not dry:
            try:
                import subprocess, sys as _sys
                _here = os.path.dirname(os.path.abspath(__file__))
                # ① 索引重建（持锁中 → 信任链模式，照抄 box_tidy 既有 scan 调用）
                subprocess.run([_sys.executable, os.path.join(_here, "todo_box_scan.py"),
                                "--trust-lock", "--auto"], capture_output=True, timeout=120)
                # ② 看板重生成
                subprocess.run([_sys.executable, os.path.join(_here, "_make_dashboard.py")],
                               capture_output=True, timeout=60)
            except Exception:
                pass
        return 0


# ══════════════════════════════════════════════════════
# --apply：执行待确认清单（熔断后的人工通道）
# ══════════════════════════════════════════════════════
def apply_list(list_path: str) -> int:
    """执行待确认修复清单。逐条校验当前行仍等于旧行（TOCTOU 防护）。"""
    if not os.path.exists(list_path):
        print(f"✗ 清单不存在: {list_path}")
        return 2
    txt = open(list_path, encoding="utf-8").read()
    rows = re.findall(r"^\|\s*(.+?)\s*\|\s*(\d+)\s*\|\s*`(.+?)`\s*\|", txt, re.M)
    if not rows:
        print("✗ 清单里没有可执行的行")
        return 2

    log = ChangeLog()
    items = []
    for rel, i, old in rows:
        p = os.path.join(bc.BOX, rel)
        items.append((p, int(i), old.replace("\\|", "|")))
    n = apply_status_fix(items, log, dry=False)
    log.flush("apply")
    print(f"✓ --apply 完成：修复 {n} 条（清单 {len(rows)} 条）")
    return 0


# ══════════════════════════════════════════════════════
# 自检
# ══════════════════════════════════════════════════════
def selftest() -> int:
    print("=== box_tidy 自检 ===")
    fails = []

    # 1. 树快照可生成
    log = ChangeLog()
    snap = make_tree_snapshot(log)
    ok1 = snap is not None and os.path.exists(snap)
    print(f"  [{'OK' if ok1 else 'FAIL'}] 树快照生成: {os.path.basename(snap) if snap else 'None'}")
    if not ok1:
        fails.append("树快照")

    # 2. 状态修复正则（v2：文件体内 done:true / 勾选框；行编号协议已退役）
    ok2 = bool(HIGH_CONF_RE.match("done: true"))
    ok3 = not HIGH_CONF_RE.match("1. 普通编号文字")            # 普通编号不该匹配
    ok4 = not HIGH_CONF_RE.match("- [ ] 正常待办正文")          # 未勾选不该匹配
    print(f"  [{'OK' if ok2 else 'FAIL'}] 高置信正则命中 `done: true`")
    print(f"  [{'OK' if ok3 else 'FAIL'}] 普通编号不误判")
    print(f"  [{'OK' if ok4 else 'FAIL'}] 未勾选不误判")
    if not (ok2 and ok3 and ok4):
        fails.append("状态正则")

    # 3. 格式巡检正则（只碰坏格式）
    ok5 = bool(BAD_FORMAT_RE.match("- [现在就做] [ ] 内容"))  # 坏格式：分类前勾选框后
    ok6 = not BAD_FORMAT_RE.match("- [ ] [现在就做] 内容")     # 正常不该匹配
    print(f"  [{'OK' if ok5 else 'FAIL'}] 坏格式被识别")
    print(f"  [{'OK' if ok6 else 'FAIL'}] 正常格式不受影响")
    if not (ok5 and ok6):
        fails.append("格式正则")

    # 4. 可执行巡检
    exes = patrol_executables(log)
    ok7 = isinstance(exes, list)
    print(f"  [{'OK' if ok7 else 'FAIL'}] 可执行巡检可运行（发现 {len(exes)} 个）")

    # 5. dry 模式：不做实际修改
    n_high, n_low, _ = scan_status_fix(log, dry=True)
    print(f"  [OK] dry 扫描：高置信 {n_high} / 低置信 {n_low}")

    # 6. 编码安全（2026-9-12 事故②：读什么编码，写回什么编码）
    import tempfile as _tf2
    import shutil as _sh2
    _td2 = _tf2.mkdtemp(prefix="tidy_enc_")
    _f = os.path.join(_td2, "gbk.txt")
    open(_f, "wb").write("中文内容\r\n".encode("gbk"))
    _txt, _enc, _lossy = read_text_strict(_f)
    ok8 = (_enc == "gbk" and not _lossy and "中文内容" in _txt)
    print(f"  [{'OK' if ok8 else 'FAIL'}] 严格读识别 GBK（enc={_enc}, lossy={_lossy}）")
    if not ok8:
        fails.append("编码识别")
    # utf-16 也不能误判成 gbk
    _f2 = os.path.join(_td2, "u16.txt")
    open(_f2, "wb").write("中文内容".encode("utf-16"))
    _t2, _e2, _l2 = read_text_strict(_f2)
    ok9 = (_t2.strip("\ufeff") == "中文内容")
    print(f"  [{'OK' if ok9 else 'FAIL'}] 严格读识别 UTF-16（enc={_e2}）")
    if not ok9:
        fails.append("utf16识别")
    _sh2.rmtree(_td2, ignore_errors=True)

    # 7. 转码备份路径唯一性（B3 预检：5 个同名 _说明.txt 不互覆）
    _b1 = transcode_backup_path(os.path.join(bc.BOX, "01_语音", "_说明.txt"), "t")
    _b2 = transcode_backup_path(os.path.join(bc.BOX, "05_杂项", "_说明.txt"), "t")
    ok10 = (_b1 != _b2)
    print(f"  [{'OK' if ok10 else 'FAIL'}] 转码备份路径镜像唯一（同名文件不互覆）")
    if not ok10:
        fails.append("转码备份唯一性")

    if fails:
        print(f"\n✗ 失败: {fails}")
        return 1
    print("✅ box_tidy 自检通过")
    return 0


def run_transcode(limit: int | None = None, dry: bool = False) -> int:
    """B3 专用转码入口：**抢锁 + 整树快照 + 预检**（与 tidy 同级安全前提）。

    B3 预检加固（2026-9-12）：原 `--transcode` 裸奔（无锁、无整树快照）——
    补上与 tidy 相同的安全前提，并把「预检失败的条目」列进报告。
    dry=True：预演（只列清单，不改动、不备份）。
    """
    try:
        lk = BoxLock("box_tidy", quiet=True)
        lk.acquire()
    except LockBusy as e:
        print(f"✗ {e}")
        return 4
    try:
        log = ChangeLog()
        # 1. 整树快照（失败即中止）
        if not dry:
            snap = make_tree_snapshot(log)
            if snap is None:
                print("✗ 树快照失败——中止转码（不裸奔）")
                return 5
            print(f"· 整树快照: {os.path.basename(snap)}")

        # 2. 转码
        n, manual = transcode(log, dry=dry, limit=limit)
        log.flush("transcode" if not dry else "transcode-dry")

        if dry:
            print(f"=== 转码预演（limit={limit or '全部'}）===")
            print(f"  · 可转 {n} 个 ｜ 预检失败 {manual} 个")
            return 0
        print(f"✓ 转码完成：{n} 个" + (f"（{manual} 个预检失败待人工审）" if manual else ""))
        return 0
    finally:
        try:
            lk.release()
        except Exception:
            pass


def main() -> int:
    ap = argparse.ArgumentParser(description="待办箱整理器")
    ap.add_argument("--auto", action="store_true", help="cron 模式：无事静默")
    ap.add_argument("--dry", action="store_true", help="只报告不修改（含 --transcode 预演）")
    ap.add_argument("--apply", metavar="清单文件", help="执行待确认修复清单")
    ap.add_argument("--transcode", action="store_true", help="执行 GBK→UTF-8 转码（B3 批专用，建议分批）")
    ap.add_argument("--limit", type=int, default=0, help="--transcode 时限制本次数量（分批护送）")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()

    if args.selftest:
        return selftest()
    if args.apply:
        return apply_list(args.apply)
    if args.transcode:
        return run_transcode(limit=args.limit or None, dry=args.dry)
    return tidy(auto=args.auto, dry=args.dry)


if __name__ == "__main__":
    sys.exit(main())
