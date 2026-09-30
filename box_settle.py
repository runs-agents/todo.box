#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
box_settle.py — 待办箱销账器（2026-9-30 · 盲审二轮 Tier 2 正解）

【定位】写入器开账（box_add），销账器平账（box_settle）——「完成」从此是一条
    显式、大声、可审计的命令，而不是手改文件体的隐性路径。

【用法】
    python box_settle.py T000003                # 销账（rename + 归档 + 刷新）
    python box_settle.py T000003 --note "备注"  # 销账并附备注（写入文件体）
    python box_settle.py T000003 --dry-run      # 只报告要做什么，不动盘

【全链（全部在锁内）】
    1. 按 tid 定位：扫五区+收件箱，找 OPEN_<date>_<tid>_*.txt（唯一）
    2. 验状态：找不到 / 已是 DONE_ → 大声报错退出（rc=4），绝不静默
    3. 附备注（可选）：文件体追加 `note: <备注>`（write_safe 原子写 + 回读）
    4. rename OPEN_→DONE_（同卷原子，tid/日期/节奏/标题不动）
    5. 归档 _done/YYYY-MM/（复用 tidy 的归档纪律：目标同名 → 受阻出声不覆盖）
    6. 触发扫描+看板刷新（账一动，板就新；失败不阻塞销账）
    7. 大声回执：销账前后路径、tid 不变声明

【纪律】
  · 只 rename + 移动 + 追加备注，禁止删除与内容改写（销账证据块原样保留）
  · tid 是对账主键：全链 tid 不变（宪法「TID-as-key」）
  · 失败必须响（宪法 4）：定位失败/已销/DONE 同名/写失败全部出声 + 非零 rc

【rc 约定】0=销账成功  2=参数错  4=定位失败（无此 tid 或已 DONE）  5=写/移动失败  6=锁忙
"""
from __future__ import annotations

import argparse
import datetime
import os
import re
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import box_config as bc  # noqa: E402
import write_safe  # noqa: E402
from box_lock import BoxLock, LockBusy  # noqa: E402

#: 时区（与扫描器同款：tzdata 缺席时 UTC+8 兜底）
try:
    from zoneinfo import ZoneInfo
    SH = ZoneInfo("Asia/Shanghai")
except Exception:
    import datetime as _dt
    SH = _dt.timezone(_dt.timedelta(hours=8))

#: 销账备注行格式（追加在文件体末尾）
NOTE_LINE_RE = re.compile(r"(?m)^note\s*[:：]\s*(.+)$")


def find_open_by_tid(tid: str) -> tuple[str, dict] | None:
    """在五区+收件箱里找唯一 OPEN_<date>_<tid>_*.txt。返回 (绝对路径, parse 结果)。

    找不到 OPEN 但存在同 tid 的 DONE_ → 返回 None（调用方报「已销账」）。
    同 tid 多个 OPEN（理论不可能，tid 唯一）→ 报错退出。
    """
    opens, dones = [], []
    roots = list(bc.ZONES.values()) + [bc.INBOX]
    for zdir in roots:
        if not os.path.isdir(zdir):
            continue
        for fn in sorted(os.listdir(zdir)):
            pr = bc.parse_filename(fn)
            if not pr or pr.get("tid") != tid:
                continue
            p = os.path.join(zdir, fn)
            if pr["prefix"] == "OPEN":
                opens.append((p, pr))
            elif pr["prefix"] == "DONE":
                dones.append((p, pr))
    if len(opens) > 1:
        print(f"✗ 异常：tid {tid} 有 {len(opens)} 个 OPEN 文件（tid 应唯一）——"
              f"请人工核查：")
        for p, _ in opens:
            print(f"    {os.path.relpath(p, bc.BOX)}")
        sys.exit(4)
    if opens:
        return opens[0]
    if dones:
        print(f"✗ tid {tid} 已是 DONE（已销账）：{os.path.relpath(dones[0][0], bc.BOX)}")
        sys.exit(4)
    # 归档区也查一遍（销账后文件在 _done/，不在五区）——区分「已销」与「无此账」
    for root in (bc.DONE_DIR,):
        if not os.path.isdir(root):
            continue
        for sub in sorted(os.listdir(root)):
            subp = os.path.join(root, sub)
            if not os.path.isdir(subp):
                continue
            for fn in sorted(os.listdir(subp)):
                pr = bc.parse_filename(fn)
                if pr and pr.get("tid") == tid and pr["prefix"] == "DONE":
                    print(f"✗ tid {tid} 已销账（归档于 {os.path.relpath(os.path.join(subp, fn), bc.BOX)}）")
                    sys.exit(4)
    return None


def settle(tid: str, note: str | None, dry: bool) -> int:
    """销账主链。返回 rc。"""
    found = find_open_by_tid(tid)
    if found is None:
        print(f"✗ 找不到 OPEN 状态的 tid {tid}（五区+收件箱均无）——未动任何文件。")
        return 4
    src, pr = found
    fn = os.path.basename(src)
    zdir = os.path.dirname(src)
    done_fn = "DONE_" + fn[len("OPEN_"):]
    done_p = os.path.join(zdir, done_fn)
    month = datetime.datetime.now(SH).strftime("%Y-%m")
    dst_dir = os.path.join(bc.DONE_DIR, month)
    dst = os.path.join(dst_dir, done_fn)

    print(f"◎ 目标：{os.path.relpath(src, bc.BOX)}")
    print(f"  计划：rename → {done_fn} → 归档 → _done/{month}/")
    if note:
        print(f"  备注：{note}")
    if dry:
        print("  [dry-run] 未动任何文件。")
        return 0

    try:
        with BoxLock("box_settle") as lk:
            # 1. 复核（锁内重验，防 TOCTOU：拿锁前文件可能已被 tidy 归档）
            if not os.path.exists(src):
                print(f"✗ 拿锁后目标已消失（可能刚被归档）：{fn}——请重扫后再试。")
                return 4
            # 2. 追加备注（可选）——write_safe 原子写 + 回读
            note_written = False
            if note:
                txt, _enc, lossy = write_safe.read_text_strict(src)
                if lossy:
                    print(f"✗ 文件体编码不明（拒写，铁律）——备注未追加，销账中止。")
                    return 5
                if NOTE_LINE_RE.search(txt):
                    print("⚠ 文件体已有 note 行，跳过追加（不改写既有内容）。")
                else:
                    if not txt.endswith("\n"):
                        txt += "\n"
                    txt += f"note: {note}\n"
                    if not write_safe.safe_write(src, txt, backup=False):
                        print("✗ 备注写入失败（write_safe 报告）——销账中止。")
                        return 5
                    note_written = True
            # 3. rename OPEN_→DONE_（同卷原子）
            # ★ 终审 P2 修复（可露希尔 2026-9-30）：note 已追加场景下，本分支
            #   原文「未动文件」说谎（备注写入了=文件被动过）。回执必须真。
            if os.path.exists(done_p):
                if note_written:
                    print(f"✗ 销账受阻：DONE_ 同名已存在 {done_fn}——"
                          f"备注已追加进文件体，rename 受阻（请人工处理）。")
                else:
                    print(f"✗ 销账受阻：DONE_ 同名已存在 {done_fn}——未动文件。")
                return 5
            os.replace(src, done_p)
            # 4. 归档 _done/YYYY-MM/
            os.makedirs(dst_dir, exist_ok=True)
            if os.path.exists(dst):
                print(f"✗ 归档受阻：目标同名已存在 _done/{month}/{done_fn}——"
                      f"文件已 rename 为 DONE_ 但留在原分区，请人工归档。")
                return 5
            os.replace(done_p, dst)
    except LockBusy:
        print("✗ 箱子正被其他工具占用（锁忙）——请稍后重试。")
        return 6

    # 5. 大声回执
    print(f"✓ 已销账：tid {tid}")
    print(f"  {os.path.relpath(src, bc.BOX)}")
    print(f"  → _done/{month}/{done_fn}")
    print(f"  tid 不变（对账主键）：{tid}")

    # 6. 触发扫描+看板刷新（账一动，板就新；失败不阻塞销账）
    _refresh()
    return 0


def _refresh() -> None:
    """扫描重建索引 + 重生成看板（与 box_add 同款链路；失败静默不阻塞）。"""
    here = os.path.dirname(os.path.abspath(__file__))
    for script in ("todo_box_scan.py", "_make_dashboard.py"):
        try:
            subprocess.run([sys.executable, os.path.join(here, script)],
                           capture_output=True, timeout=120)
        except Exception:
            pass


def main() -> int:
    ap = argparse.ArgumentParser(description="待办箱销账器：tid 直达，rename+归档+刷新")
    ap.add_argument("tid", help="待销账的 tid（如 T000003）")
    ap.add_argument("--note", help="销账备注（追加进文件体 note: 行）")
    ap.add_argument("--dry-run", action="store_true", help="只报告计划，不动盘")
    args = ap.parse_args()
    if not re.fullmatch(r"T[0-9a-fA-F]+", args.tid):
        print("✗ tid 格式应为 T + 十六进制（如 T000003）。")
        return 2
    return settle(args.tid, args.note, args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
