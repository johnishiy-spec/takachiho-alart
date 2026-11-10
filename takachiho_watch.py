#!/usr/bin/env python3
"""高千穂峡貸しボート キャンセル待ち監視 → スマホ(ntfy)に通知

使い方（初回）:
  pip install playwright
  playwright install chromium
  python takachiho_watch.py --topic あなた専用の長いランダム文字列 --test   # 通知テスト
  python takachiho_watch.py --topic ... --dump                              # 読み取り結果の確認
  python takachiho_watch.py --topic ...                                     # 本番(ループ)
"""
import argparse
import datetime as dt
import json
import random
import re
import sys
import time
import urllib.request
from pathlib import Path

from playwright.sync_api import sync_playwright

URL = "https://eipro.jp/takachiho1/eventCalendars/index"
STATE_FILE = Path(__file__).with_name("takachiho_state.json")

# ページ内のカレンダー表を読む。構造が想定と違えば error を返す（黙って失敗しない）
JS_EXTRACT = r"""
() => {
  const R = e => e.getBoundingClientRect();
  const dateRe = /(\d{1,2})\/(\d{1,2})/;
  // --- 日付ヘッダ行（日付を7つ以上含む行）。列のx範囲を取る
  let cols = [];
  for (const tr of document.querySelectorAll('tr')) {
    const cs = [...tr.children].filter(c => dateRe.test(c.innerText || '') && R(c).width > 0);
    if (cs.length >= 7) {
      cols = cs.map(c => {
        const m = c.innerText.match(dateRe), r = R(c);
        return {date: m[1].padStart(2, '0') + '/' + m[2].padStart(2, '0'), x0: r.left, x1: r.right};
      });
      break;
    }
  }
  if (!cols.length) return {error: '日付ヘッダ行が見つかりません'};
  // --- 時刻ラベル（葉要素で "H:MM" だけの要素）。行の上端yを使う
  const labels = [];
  for (const e of document.querySelectorAll('*')) {
    if (e.children.length) continue;
    const t = (e.textContent || '').trim();
    if (!/^\d{1,2}:\d{2}$/.test(t)) continue;
    const row = e.closest('tr') || e;
    const r = R(row);
    if (r.height <= 0) continue;
    const [h, m] = t.split(':').map(Number);
    labels.push({t: h * 60 + m, y: r.top, h: r.height});
  }
  labels.sort((p, q) => p.y - q.y);
  const uniq = labels.filter((l, i) => i === 0 || Math.abs(l.y - labels[i - 1].y) > 2);
  if (uniq.length < 2) return {error: '時刻ラベルが見つかりません', cols: cols.map(c => c.date)};
  const first = uniq[0], last = uniq[uniq.length - 1];
  const slope = (last.t - first.t) / (last.y - first.y);   // 分/ピクセル
  const minAt = y => first.t + (y - first.y) * slope;
  const fmt = m => String(Math.floor(m / 60)).padStart(2, '0') + ':' + String(m % 60).padStart(2, '0');
  const colOf = x => cols.find(c => x >= c.x0 && x <= c.x1);
  // --- イベント要素（FullCalendarのルート要素）
  const roots = [...document.querySelectorAll('.fc-event,.fc-bg-event,.fc-bgevent,.fc-time-grid-event,.fc-timegrid-event')]
    .filter(e => R(e).width > 0 && R(e).height > 0);
  const out = [], seen = new Set();
  const gridTop = first.y - 2, gridBot = last.y + last.h;
  const add = (e, how, center) => {
    const r = R(e), y = center ? (r.top + r.bottom) / 2 : r.top;
    if (y < gridTop || y > gridBot) return;                 // 表の外(凡例など)は無視
    const col = colOf((r.left + r.right) / 2);
    if (!col) return;
    const mins = Math.floor((minAt(y) + (center ? 0 : 2)) / 30) * 30;
    const key = col.date + '_' + mins;
    if (seen.has(key)) return;
    seen.add(key);
    out.push({date: col.date, time: fmt(mins), text: (e.innerText || e.textContent || '').replace(/\s+/g, ''),
              hasLink: e.tagName === 'A' || !!e.querySelector('a,button,input'),
              alt: [...e.querySelectorAll('img')].map(x => x.alt || '').join(''),
              cls: (e.className || '').toString().slice(0, 40), how: how});
  };
  roots.forEach(e => add(e, 'root', false));
  // --- 予備: 記号だけの葉要素（クラスが想定外でも拾う）
  for (const e of document.querySelectorAll('*')) {
    if (e.children.length) continue;
    const t = (e.textContent || '').trim();
    if (/^[×✕✖○◯◎△▲]$/.test(t) && R(e).width > 0) add(e, 'symbol', true);
  }
  // --- 予備: 「残1艇」のような表示（クラス名が不明でも拾う。最も内側の要素のみ）
  const zanRe = /残\d+艇?/;
  for (const e of document.querySelectorAll('*')) {
    const t = (e.innerText || '').replace(/\s+/g, '');
    if (!zanRe.test(t) || t.length > 12 || R(e).width <= 0) continue;
    if ([...e.children].some(c => zanRe.test((c.innerText || '').replace(/\s+/g, '')))) continue;
    add(e, 'zan', true);
  }
  return {slots: out, cols: cols.map(c => c.date), labelCount: uniq.length,
          firstLabel: fmt(first.t), lastLabel: fmt(last.t), rootCount: roots.length};
}
"""


def classify(cell):
    """'full' | 'notopen' | 'avail' | 'unknown'（空きは確実な記号のみ。誤通知を避ける）"""
    s = cell["text"] + cell["alt"]
    if any(k in s for k in "×✕✖満締休終"):
        return "full"
    if "砂時計" in s or "⌛" in s or "⏳" in s:
        return "notopen"
    if any(k in s for k in "○◯◎△残空") or (cell["hasLink"] and s):
        return "avail"
    return "unknown"


DEBUG = {}
NET = []


HEADER_JS = r"""
() => {
  for (const tr of document.querySelectorAll('tr')) {
    const ds = [...tr.children].map(c => (c.innerText || '').match(/(\d{1,2})\/(\d{1,2})/))
      .filter(Boolean).map(m => m[1].padStart(2, '0') + '/' + m[2].padStart(2, '0'));
    if (ds.length >= 7) return ds;
  }
  return [];
}
"""

ANCHOR_JS = r"""
() => {
  const re = /\d{4}\/\d{2}\/\d{2}\D{1,3}\d{4}\/\d{2}\/\d{2}/;
  let best = null, bl = 1e9;
  for (const e of document.querySelectorAll('select,div,span,a,button,label,p')) {
    const t = (e.textContent || '');
    if (!re.test(t) || e.getBoundingClientRect().width <= 0) continue;
    if (t.length < bl) { best = e; bl = t.length; }
  }
  if (!best) return null;
  best.scrollIntoView({block: 'center'});
  const r = best.getBoundingClientRect();
  return {x: r.right, y: r.top + r.height / 2, h: r.height, tag: best.tagName};
}
"""


def week_dates(page):
    try:
        return page.evaluate(HEADER_JS)
    except Exception:
        return []


def pick_week(page, target):
    """週表示にして、target日を含む週へ移動する"""
    key = target.strftime("%m/%d")
    try:
        page.get_by_text("週", exact=True).first.click(timeout=5000)
        DEBUG["week_click"] = "ok"
    except Exception as e:
        DEBUG["week_click"] = "失敗: " + str(e)[:60]
    page.wait_for_timeout(2500)
    DEBUG["dates_before"] = week_dates(page)[:1] + week_dates(page)[-1:]
    try:  # 動作確認用: 移動前の週で「full以外」に判定された枠（今見えている空きが拾えているか）
        r0 = page.evaluate(JS_EXTRACT)
        DEBUG["initial_week_nonfull"] = ["%s %s %s %s" % (c["date"], c["time"], c["text"][:6], classify(c))
                                         for c in r0.get("slots", []) if classify(c) != "full"][:8]
    except Exception as e:
        DEBUG["initial_week_nonfull"] = "err " + str(e)[:40]
    if key in week_dates(page):
        return
    # 1) 週のプルダウン（〜は全角/半角の違いがあるので全て許容）
    sel = page.locator("select").filter(has_text=re.compile(r"\d{4}/\d{2}/\d{2}"))
    DEBUG["week_select_count"] = sel.count()
    if sel.count():
        for opt in sel.first.locator("option").all():
            label = opt.inner_text().strip()
            m = re.findall(r"(\d{4})/(\d{2})/(\d{2})", label)
            if len(m) == 2 and dt.date(*map(int, m[0])) <= target <= dt.date(*map(int, m[1])):
                sel.first.select_option(label=label)
                DEBUG["week_selected"] = label
                try:
                    page.wait_for_load_state("networkidle", timeout=15000)
                except Exception:
                    pass
                page.wait_for_timeout(2500)
                if key in week_dates(page):
                    return
                break
    # 2) 次週の矢印（週表示の右側の「▶」）を座標でクリックして移動（最大4回）
    for i in range(4):
        pos = page.evaluate(ANCHOR_JS)
        DEBUG["anchor_%d" % i] = pos
        if not pos:
            break
        before = week_dates(page)[:1]
        for dx in (22, 38, 12):
            page.mouse.click(pos["x"] + dx, pos["y"])
            try:
                page.wait_for_load_state("networkidle", timeout=15000)
            except Exception:
                pass
            page.wait_for_timeout(2500)
            if week_dates(page)[:1] != before:
                DEBUG["arrow_dx"] = dx
                break
        if key in week_dates(page):
            return


def fetch_slots(pw, target, dump=False):
    browser = pw.chromium.launch(headless=True)
    try:
        page = browser.new_page(viewport={"width": 1100, "height": 900})
        NET.clear()

        def on_resp(r):
            try:
                ct = r.headers.get("content-type", "")
                if "json" in ct or "javascript" in ct:
                    NET.append((r.url[-90:], r.status, r.text()[:220].replace("\n", " ")))
            except Exception:
                pass
        page.on("response", on_resp)
        page.goto(URL, wait_until="networkidle", timeout=60000)
        pick_week(page, target)
        res = page.evaluate(JS_EXTRACT)
        if "error" not in res and target.strftime("%m/%d") not in res["cols"]:
            res = {"error": "対象日を含む週に移動できませんでした", "cols": res["cols"]}
        if dump:
            Path("takachiho_page.html").write_text(page.content(), encoding="utf-8")
        return res
    finally:
        browser.close()


def notify(topic, title, message, priority=3, tags=None):
    body = {"topic": topic, "title": title, "message": message,
            "priority": priority, "click": URL, "tags": tags or []}
    req = urllib.request.Request(
        "https://ntfy.sh/", data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"})
    urllib.request.urlopen(req, timeout=20).read()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--topic", required=True, help="ntfyのトピック名（長いランダム文字列）")
    ap.add_argument("--date", action="append", default=None, help="監視日 YYYY-MM-DD（複数可）。既定 2026-11-20")
    ap.add_argument("--from-time", default="08:00")
    ap.add_argument("--to-time", default="17:00")
    ap.add_argument("--interval", type=int, default=300, help="確認間隔(秒)。既定300")
    ap.add_argument("--until", default=None, help="全体の終了日時を手動指定(省略時は各日の予約締切=2日前9時)")
    ap.add_argument("--heartbeat-hours", type=float, default=6, help="生存通知の間隔(時間)。0で無し")
    ap.add_argument("--test", action="store_true", help="通知テストだけして終了")
    ap.add_argument("--dump", action="store_true", help="現在の読み取り結果を表示して終了")
    ap.add_argument("--once", action="store_true", help="1回だけ確認して終了")
    args = ap.parse_args()

    if args.test:
        notify(args.topic, "高千穂ボート監視 テスト", "このメッセージが届けば通知はOKです", 3, ["white_check_mark"])
        print("送信しました。スマホに届いたか確認してください")
        return

    targets = [dt.date.fromisoformat(d) for d in (args.date or ["2026-11-20", "2026-11-21", "2026-11-22"])]

    def deadline(t):  # ネット予約は2日前の9:00まで
        d = dt.datetime.combine(t - dt.timedelta(days=2), dt.time(9, 0))
        return min(d, dt.datetime.fromisoformat(args.until)) if args.until else d

    def active_targets():
        return [t for t in targets if dt.datetime.now() < deadline(t)]
    state = json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {}
    fails, last_beat = 0, time.time()

    if args.once and not active_targets():
        print("全日程が予約締切を過ぎています。終了します")
        return

    with sync_playwright() as pw:
        if args.dump:
            for t in targets:
                res = fetch_slots(pw, t, dump=True)
                print("=== 診断", t, "===")
                print("DEBUG:", DEBUG)
                print({k: v for k, v in res.items() if k != "slots"})
                for u, st, body in NET[:4]:
                    print("NET:", st, u, body)
                if "error" in res:
                    continue
                key = t.strftime("%m/%d")
                rows = [c for c in res["slots"] if c["date"] == key]
                print("該当日の枠数:", len(rows))
                for c in sorted(rows, key=lambda c: c["time"])[:14]:
                    print(c["date"], c["time"], repr(c["text"]), classify(c), c["cls"], c["how"])
            return

        if args.heartbeat_hours and not args.once:
            notify(args.topic, "高千穂ボート監視 開始",
                   f"{', '.join(map(str, targets))} を{args.interval // 60}分おきに監視します", 2)

        while active_targets():
            try:
                found = []
                for t in active_targets():
                    res = fetch_slots(pw, t)
                    if "error" in res:
                        raise RuntimeError(res["error"])
                    key = t.strftime("%m/%d")
                    if not any(c["date"] == key for c in res["slots"]):
                        raise RuntimeError(f"{key} の枠が読み取れません(週の切替か表の構造を確認)")
                    for c in res["slots"]:
                        if c["date"] != key or not (args.from_time <= c["time"] <= args.to_time):
                            continue
                        k = f'{t}_{c["time"]}'
                        now_avail = classify(c) == "avail"
                        if now_avail and not state.get(k):
                            found.append(f'{t.strftime("%m/%d")} {c["time"]} {c["text"][:8]}')
                        state[k] = now_avail
                STATE_FILE.write_text(json.dumps(state))
                fails = 0
                if found:
                    notify(args.topic, "🚣 高千穂ボート 空きが出ました！",
                           "、".join(found) + "\n今すぐ予約ページへ", 5, ["rotating_light"])
                    print(dt.datetime.now(), "通知:", found)
                else:
                    print(dt.datetime.now(), "空きなし")
            except Exception as e:
                fails += 1
                print(dt.datetime.now(), "エラー:", e, file=sys.stderr)
                if args.once:
                    sys.exit(1)  # GitHub Actionsが失敗メールで知らせる
                if fails == 3:
                    notify(args.topic, "高千穂ボート監視 エラー",
                           f"3回連続で取得に失敗: {e}", 4, ["warning"])

            if args.heartbeat_hours and time.time() - last_beat > args.heartbeat_hours * 3600:
                notify(args.topic, "高千穂ボート監視 稼働中", "まだ空きは出ていません（動作中）", 1)
                last_beat = time.time()
            if args.once:
                return
            time.sleep(args.interval + random.randint(0, 60))

    notify(args.topic, "高千穂ボート監視 終了", "全ての日がネット予約の締切時刻になったので監視を終了しました", 3)


if __name__ == "__main__":
    main()
