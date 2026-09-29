# -*- coding: utf-8 -*-
"""待办箱看板 v2 —— 按千问评估报告的三条约束修正

v1 的问题（千问指出，已核实）：
  ① 缺防伪四件套 → 在手机端精确重建了一个「假真源」的形态
  ② 把文档正文里的陈述句也渲染成待办 → 漂亮但掺假

v2 补上：
  ① GENERATED 横幅（首行声明 + 生成时间 + scan-id）
  ② 内容哈希漂移检测（与上次生成的 hash 比对，手改即告警）
  ③ 统一 scan-id（与索引共用 source_hash）
  ④ 自我声明截断（共 N 笔，此处 M 笔）
  ⑤ 「疑似非待办」标记 —— 把机器判不准的**交给人的眼睛**，而不是替人决定
"""
import hashlib
import io
import json
import os
import re
import sys
import time
from collections import Counter, defaultdict

# ★ 单6：BOX/OUT/INDEX 走 box_config 单源（删本地硬编码——双源分叉是病根）。
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import box_config as bc          # noqa: E402
INDEX = bc.INDEX_JSON
BOX = bc.BOX
OUT = bc.DASHBOARD_HTML
SIG = bc.DASHBOARD_SIG

NON_REAL = ("参考", "约定", "前置", "子步骤")
# ★ 单6：分区名与图标切新家命名（无「区」字）
ZONE_ORDER = [os.path.basename(bc.INBOX)] + [os.path.basename(p) for p in bc.ZONES.values()]
ZONE_ICON = {"00_收件箱": "📥", "01_工作": "💼", "02_学习": "📚",
             "03_生活": "🏠", "04_参考": "📎", "05_杂项": "🧩"}
ZONE_DESC = {"00_收件箱": "没定归处", "01_工作": "工作事项",
             "02_学习": "学习事项", "03_生活": "生活事项",
             "04_参考": "参考资料", "05_杂项": "暂难归类"}

# ★ 2026-9-13 改：不再自己硬编码判据（那是第四份漂移的真源）——
#   改为**读扫描器的 suspected 字段**（灰带闸门已写入索引）。
#   历史教训：_check_conservation.py 曾自硬编码白名单，三处漂移。


d = json.load(io.open(INDEX, encoding="utf-8"))
scan_id = str(d.get("source_hash", ""))[:12]
real = [i for i in d["items"]
        if i.get("kind") == "明细" and i.get("state") == "todo"
        and i.get("cls") not in NON_REAL]
# ★ FIX-9（B包 C15）：done 也限定 kind=="明细" —— 原实现把非明细条目计入头部
#   「已完成 N」，数字失真（real 侧过滤了 kind 与 NON_REAL，done 侧没有）。
done = [i for i in d["items"] if i.get("kind") == "明细" and i.get("state") == "done"]

by_zone = defaultdict(list)
for i in real:
    f = str(i.get("file", "")).replace("\\", "/")
    by_zone[f.split("/")[0] if "/" in f else "05_杂项"].append(i)

# ══ ★ 提醒节流（2026-9-13 所有者设计：提醒整合进看板，每天算一次）══
# 为什么放这里：看板每天只生成一次 → 一天开多次新对话也不会多次提醒（天然去重）。
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import todo_notify  # noqa: E402
import box_config as bc  # noqa: E402

due, waiting, notify_stats = todo_notify.due_items(
    real, bc.NOTIFY_CADENCE, bc.NOTIFY_CADENCE_DEFAULT)
stale = todo_notify.stale_items(real, BOX, bc.STALE_DAYS)

now = time.time()


def age_days(i):
    p = os.path.join(BOX, str(i.get("file", "")).replace("\\", os.sep))
    try:
        return max(0.0, (now - os.stat(p).st_mtime) / 86400.0)
    except OSError:
        return None


def esc(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


total = len(real)
suspect_count = 0
cards = []
for z in ZONE_ORDER:
    items = by_zone.get(z, [])
    if not items:
        continue
    rows = []
    for i in items:
        raw = str(i.get("title", ""))
        t = esc(raw[:110])
        a = age_days(i)
        if a is None:
            age_html = '<span class="age cold">?</span>'
        elif a < 1:
            age_html = f'<span class="age fresh">{a*24:.0f}小时</span>'
        elif a < 7:
            age_html = f'<span class="age">{a:.0f}天</span>'
        else:
            age_html = f'<span class="age cold">{a:.0f}天</span>'
        cls = esc(str(i.get("cls", "")))
        # ★ 读扫描器的灰带判定（单一真源），不再本地硬编码
        susp_why = i.get("suspected") or ""
        susp = bool(susp_why)
        if susp:
            suspect_count += 1
        badge = (f'<span class="susp" title="{esc(susp_why)}——请人眼确认">?</span>'
                 if susp else "")
        rows.append(
            f'<li{" class=warn" if susp else ""}>{badge}'
            f'<span class="ttl">{t}</span>'
            f'<span class="meta"><span class="cls c-{cls}">{cls}</span>{age_html}</span></li>')
    cards.append(f"""
    <section class="zone">
      <h2>{ZONE_ICON.get(z,'📁')} {esc(z.replace('_',' '))}
          <span class="count">{len(items)}</span>
          <span class="zdesc">{esc(ZONE_DESC.get(z,''))}</span></h2>
      <ul>{''.join(rows)}</ul>
    </section>""")

cls_stat = Counter(str(i.get("cls", "")) for i in real)
cls_bar = " · ".join(f'{esc(k)} {v}' for k, v in cls_stat.most_common())

# ── 提醒区 HTML（★ 2026-9-13：所有者设计——提醒在看板里算，每天一次）──
_notify_rows = []
for tid, cls, title, note in due:
    _notify_rows.append(
        f'<li class="due"><span class="bell">🔔</span>'
        f'<span class="ttl">{esc(title)}</span>'
        f'<span class="meta"><span class="cls">{esc(cls)}</span>'
        f'<span class="note">{esc(note)}</span></span></li>')
notify_html = f"""
<section class="notify">
  <h2>🔔 今日提醒 <span class="count">{len(due)}</span>
      <span class="zdesc">按节奏算出来的——不是每笔都提</span></h2>
  <ul>{''.join(_notify_rows) if _notify_rows else '<li class="quiet">今天没有到期的提醒</li>'}</ul>
  <div class="nfoot">今天该提 <b>{notify_stats['due']}</b> 笔 · 未到提醒期 <b>{notify_stats['waiting']}</b> 笔
    {' · <b>停滞 ' + str(len(stale)) + ' 笔</b>（超 ' + str(bc.STALE_DAYS) + ' 天未动）' if stale else ''}
  　节奏：盯办 1 天 / 现在就做 3 天 / 长线 10 天（可调）</div>
</section>"""

_stale_html = ""
if stale:
    _sr = []
    for tid, cls, title, days in stale[:10]:
        _sr.append(f'<li class="due"><span class="bell">⏳</span>'
                   f'<span class="ttl">{esc(title)}</span>'
                   f'<span class="meta"><span class="cls">{esc(cls)}</span>'
                   f'<span class="note">{days:.0f} 天没动</span></span></li>')
    _stale_html = f"""
<section class="notify stale">
  <h2>⏳ 停滞提醒 <span class="count">{len(stale)}</span>
      <span class="zdesc">超过 {bc.STALE_DAYS} 天没动过</span></h2>
  <ul>{''.join(_sr)}</ul>
</section>"""


html = f"""<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>TodoBox · 看板</title>
<script>
/* 独立窗口判据（v3·glm-5.3审查R1修法）：URL带#standalone时同步加class——
   首帧前生效、reload后自动重建（hash保留）、宿主::preview场景无hash不命中 */
  if(location.hash==='#standalone')document.documentElement.classList.add('standalone');
</script>
<style>
  /* 独立弹窗配色（html.standalone限定，宿主::preview场景零影响）——v3 全量5变量 */
  html.standalone{{
    --foreground:#1f2328; --muted-foreground:#6e7781;
    --accent:#7c6fd0; --border:#d8dee6; --card:#ffffff;
  }}
  html.standalone body{{background:#f0f0ee}}
  /* ── 材质层 v1（抄点：dsh-priestess-skin v0.2.5, MIT, FriksD）── */
  /* 过曝记忆空间白 + 混凝土颗粒 + 切角面板 + 菱形锚 + 扫描线 */
  html.standalone body::before{{  /* 混凝土记忆质感（feTurbulence 噪点，零依赖 data-uri）*/
    content:""; position:fixed; inset:0; pointer-events:none; z-index:0;
    background-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='180' height='180'%3E%3Cfilter id='n'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='0.72' numOctaves='2' stitchTiles='stitch'/%3E%3CfeColorMatrix type='matrix' values='0 0 0 0 0.42 0 0 0 0 0.42 0 0 0 0 0.42 0 0 0 0.05 0'/%3E%3C/filter%3E%3Crect width='100%25' height='100%25' filter='url(%23n)'/%3E%3C/svg%3E");
  }}
  html.standalone body::after{{  /* 观测轨道扫描线——极淡，克制的数字幽灵残影 */
    content:""; position:fixed; inset:0; pointer-events:none; z-index:0;
    background:repeating-linear-gradient(to bottom,
      transparent 0px, transparent 3px, rgba(124,111,208,.016) 4px);
  }}
  html.standalone .zone{{position:relative; z-index:1; overflow:hidden;
    border-radius:0; border:1px solid var(--border);
    clip-path:polygon(0 0, calc(100% - 14px) 0, 100% 14px, 100% 100%, 0 100%)}}
  html.standalone .zone::before{{  /* 切角回填线 + 面板内混凝土颗粒 */
    content:""; position:absolute; inset:0; pointer-events:none;
    background-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='180' height='180'%3E%3Cfilter id='n'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='0.72' numOctaves='2' stitchTiles='stitch'/%3E%3CfeColorMatrix type='matrix' values='0 0 0 0 0.42 0 0 0 0 0.42 0 0 0 0 0.42 0 0 0 0.04 0'/%3E%3C/filter%3E%3Crect width='100%25' height='100%25' filter='url(%23n)'/%3E%3C/svg%3E");
  }}
  html.standalone .zone h2{{position:relative}}
  html.standalone .zone h2::before{{  /* 菱形锚（外框）——PRTS 观测主题的瞳孔外环 */
    content:""; display:inline-block; width:9px; height:9px;
    border:1.5px solid var(--accent); transform:rotate(45deg);
    margin-right:7px; box-shadow:0 0 6px rgba(124,111,208,.35);
  }}
  html.standalone .zone h2::after{{  /* 内层小菱形——层叠瞳孔芯 */
    content:""; display:inline-block; width:4px; height:4px;
    background:var(--accent); transform:rotate(45deg);
    margin-left:-13px; vertical-align:middle; opacity:.85;
  }}
  html.standalone .gen{{border-style:solid; border-left:3px solid var(--accent);
    clip-path:polygon(0 0, calc(100% - 10px) 0, 100% 10px, 100% 100%, 0 100%)}}
  *{{box-sizing:border-box}}
  body{{color:var(--foreground);background:transparent;font-family:inherit;
       padding:4px 2px;margin:0;font-size:13px;line-height:1.5}}
  .gen{{border:1px dashed var(--border);border-radius:8px;padding:6px 10px;
       color:var(--muted-foreground);font-size:11px;margin-bottom:10px}}
  .gen b{{color:var(--foreground)}}
  .hd{{display:flex;align-items:baseline;gap:10px;flex-wrap:wrap;
      padding-bottom:8px;border-bottom:1px solid var(--border);margin-bottom:12px}}
  .big{{font-size:26px;font-weight:650;letter-spacing:-.02em}}
  .sub{{color:var(--muted-foreground);font-size:12px}}
  .grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:10px}}
  .zone{{border:1px solid var(--border);border-radius:10px;padding:10px 12px;background:var(--card)}}
  .zone h2{{margin:0 0 8px;font-size:13px;font-weight:600;display:flex;
           align-items:center;gap:6px;flex-wrap:wrap}}
  .count{{background:var(--accent);color:var(--foreground);border-radius:20px;
         padding:0 7px;font-size:11px;font-weight:600}}
  .zdesc{{color:var(--muted-foreground);font-weight:400;font-size:11px}}
  ul{{list-style:none;margin:0;padding:0}}
  li{{display:flex;justify-content:space-between;gap:8px;align-items:flex-start;
     padding:4px 0;border-top:1px solid var(--border)}}
  li:first-child{{border-top:none}}
  li.warn{{background:color-mix(in srgb,var(--accent) 22%,transparent);
          border-radius:5px;padding-left:4px;padding-right:4px}}
  .susp{{flex-shrink:0;width:15px;height:15px;border-radius:50%;
        border:1px solid var(--muted-foreground);color:var(--muted-foreground);
        font-size:10px;line-height:13px;text-align:center;font-weight:700;
        cursor:help;margin-top:2px}}
  .ttl{{flex:1;min-width:0}}
  .meta{{display:flex;gap:5px;flex-shrink:0;align-items:center;white-space:nowrap}}
  .cls{{font-size:10px;padding:1px 6px;border-radius:20px;
       border:1px solid var(--border);color:var(--muted-foreground)}}
  .age{{font-size:10px;color:var(--muted-foreground);min-width:38px;text-align:right}}
  .age.cold{{opacity:.5}}
  .age.fresh{{font-weight:600}}
  .notify{{border:1px solid var(--accent);border-radius:10px;padding:10px 12px;
          margin-bottom:12px;background:color-mix(in srgb,var(--accent) 12%,transparent)}}
  .notify.stale{{border-color:var(--border);background:transparent}}
  .notify h2{{margin:0 0 8px;font-size:13px;font-weight:600;display:flex;
             align-items:center;gap:6px;flex-wrap:wrap}}
  .notify ul{{list-style:none;margin:0;padding:0}}
  .notify li{{display:flex;justify-content:space-between;gap:8px;align-items:flex-start;
             padding:4px 0;border-top:1px solid var(--border)}}
  .notify li:first-child{{border-top:none}}
  .notify .bell{{flex-shrink:0;font-size:11px;margin-top:2px}}
  .notify .note{{font-size:10px;color:var(--muted-foreground)}}
  .notify li.quiet{{color:var(--muted-foreground);font-size:12px;justify-content:flex-start}}
  .nfoot{{margin-top:8px;padding-top:6px;border-top:1px dashed var(--border);
         font-size:11px;color:var(--muted-foreground)}}
  .ft{{margin-top:12px;padding-top:8px;border-top:1px solid var(--border);
      color:var(--muted-foreground);font-size:11px;display:flex;
      justify-content:space-between;flex-wrap:wrap;gap:8px}}
</style></head><body>

<div class="gen">
  <b>本页由机器自动生成，请勿手改</b> —— 手改会在下次生成时丢失。
  生成于 {time.strftime('%Y-%m-%d %H:%M')} · scan-id <b>{esc(scan_id)}</b> ·
  真源：本地箱子文件夹 · 本页是只读视图，不写任何文件。
</div>

<div class="hd">
  <span class="big">{total}</span>
  <span class="sub">笔活账 · {len([z for z in ZONE_ORDER if by_zone.get(z)])} 个分区有货 · 已完成 {len(done)}{f' · <b>疑似非待办 {suspect_count}</b>' if suspect_count else ''}</span>
</div>
<div class="sub" style="margin:-6px 0 10px">{cls_bar}</div>
{notify_html}
{_stale_html}
<div class="grid">{''.join(cards)}</div>
<div class="ft">
  <span>⚠ 带 <span class="susp" style="display:inline-block">?</span> 的条目疑似陈述句/审查结论，不是可执行的待办——请扫一眼确认</span>
  <span>排序：分区 → 原始顺序；账龄 = 距文件最后修改</span>
</div>
</body></html>"""

# ── 写盘 + 指纹（防伪②：内容哈希漂移检测）──
body = html.encode("utf-8")
new_hash = hashlib.sha256(body).hexdigest()[:16]

drift = None
if os.path.exists(SIG):
    # ── FIX-7（B包 C3）：SIG 读取包 try —— 损坏视为「无历史指纹」继续 ──
    # 原实现裸 json.load，SIG 写一半断电 → JSONDecodeError → 看板崩，
    # 且 SIG 永远是坏的 → 每天崩的死循环，须人工删文件才能恢复。
    try:
        old = json.load(io.open(SIG, encoding="utf-8"))
        old_hash = old.get("hash")
        # 上次生成后，磁盘上的文件是否被人动过？
        if os.path.exists(OUT):
            cur = hashlib.sha256(open(OUT, "rb").read()).hexdigest()[:16]
            if old_hash and cur != old_hash:
                drift = f"⚠ 上次生成后，_看板.html 被外部改动过（{old_hash} → {cur}）"
    except Exception as _e:
        drift = f"⚠ 指纹文件损坏，按「无历史指纹」继续（{type(_e).__name__}）"

# ── v3（glm-5.3审查W9）：原子写——temp+rename，防60s轮询读半截HTML ──
import tempfile  # noqa: E402
OUT_DIR = os.path.dirname(OUT)
_fd, _tmp = tempfile.mkstemp(suffix=".html", dir=OUT_DIR)
try:
    with os.fdopen(_fd, "wb") as _f:
        _f.write(body)
        _f.flush(); os.fsync(_f.fileno())    # FIX-22（A包 D1）：落盘而非仅原子
    os.replace(_tmp, OUT)  # 同盘原子替换
except BaseException:
    if os.path.exists(_tmp):
        os.remove(_tmp)
    raise
# ── FIX-7（B包 C3）：SIG 改原子写（照抄上面 OUT 的 tmp + os.replace 写法）──
_sig_tmp = SIG + ".tmp"
with io.open(_sig_tmp, "w", encoding="utf-8") as _sf:
    _sf.write(json.dumps({"hash": new_hash, "gen": time.strftime("%Y-%m-%d %H:%M:%S"),
                          "scan_id": scan_id, "count": total}, ensure_ascii=False, indent=1))
    _sf.flush(); os.fsync(_sf.fileno())
os.replace(_sig_tmp, SIG)

# ★ 提醒摘要（2026-9-13）：供**会话首轮注入插件**读取。
#   设计纪律：计算只在一处（本看板），插件只读结果 → 数字天然一致，
#   不会出现"看板说3笔、插件说5笔"的多份真源病。
SUMMARY = os.path.join(bc.HOME, "todo_notify_summary.json")
# ── FIX-7（B包 C3）+ FIX-8（B包 C4）：SUMMARY 改原子写；写失败必须 raise ──
# 原实现直接 io.open(SUMMARY,"w")，半截被插件读走；且失败只降级成 _sum_ok 打印，
# 之后照常 mark_notified → 提醒被静默吞掉（「先渲染、后记账」纪律只覆盖了 OUT）。
# 修法：与 OUT 同等待遇 —— 原子写 + 失败 raise（让 mark_notified 不执行）。
_sum_tmp = SUMMARY + ".tmp"
try:
    with io.open(_sum_tmp, "w", encoding="utf-8") as f:
        json.dump({
            "schema": 1,
            "generated": time.strftime("%Y-%m-%d %H:%M"),
            "scan_id": scan_id,
            "total": total,
            "due_count": notify_stats["due"],
            "waiting_count": notify_stats["waiting"],
            "stale_count": len(stale),
            "due_titles": [d[2] for d in due[:5]],
        }, f, ensure_ascii=False, indent=1)
        f.flush(); os.fsync(f.fileno())
    os.replace(_sum_tmp, SUMMARY)
    _sum_ok = "，摘要已写"
except Exception as _e:
    # 写失败 → raise（与 OUT 同等待遇）：宁可明天重提一遍，绝不「提醒被静默吞掉」。
    try:
        if os.path.exists(_sum_tmp):
            os.remove(_sum_tmp)
    except Exception:
        pass
    raise RuntimeError(f"⚠ 摘要写入失败（已中止，不记提醒时间戳）: {_e}")

# ★ 提醒时间戳（2026-9-13）：**写盘成功之后**才记——顺序纪律（所有者定）：
#   先渲染、后记账。万一中途挂，宁可下次多看一遍，绝不"提醒被静默吞掉"。
if due:
    try:
        todo_notify.mark_notified([d[0] for d in due])
        _marked = f"，已记 {len(due)} 笔提醒时间戳（进下一轮）"
    except Exception as _e:
        _marked = f"，⚠ 时间戳写入失败：{_e}"
else:
    _marked = ""

print(f"✅ 看板 v2 已生成：{OUT}")
print(f"   {total} 笔活账 / {len([z for z in ZONE_ORDER if by_zone.get(z)])} 个分区 / 已完成 {len(done)}")
print(f"   疑似非待办：{suspect_count} 笔（已标记，未删除——交人眼终审）")
print(f"   🔔 今日提醒 {notify_stats['due']} 笔 / 未到提醒期 {notify_stats['waiting']} 笔"
      + (f" / 停滞 {len(stale)} 笔" if stale else "") + _marked)
print(f"   防伪：GENERATED 横幅 ✓ / scan-id {scan_id} ✓ / 指纹 {new_hash} ✓")
print(f"   {drift if drift else '指纹漂移检测：无异常 ✓'}")
