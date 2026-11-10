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
  const rows = [...document.querySelectorAll('tr')];
  const dateRe = /(\d{1,2})\/(\d{1,2})/;
  const timeRe = /^\d{1,2}:\d{2}$/;
  let header = null, headerIdx = -1;
  for (let i = 0; i < rows.length; i++) {
    const cells = [...rows[i].children];
    const hits = cells.filter(c => dateRe.test(c.innerText || '')).length;
    if (hits >= 7) { header = cells; headerIdx = i; break; }
  }
  if (!header) return {error: '日付ヘッダ行が見つかりません', rowCount: rows.length};
  const dates = [];
  header.forEach(c => {
    const m = (c.innerText || '').match(dateRe);
    if (m) dates.push(m[1].padStart(2, '0') + '/' + m[2].padStart(2, '0'));
  });
  const out = [], rowInfo = [];
  let labeled = 0, skipped = 0;
  for (let i = headerIdx + 1; i < rows.length; i++) {
    const cells = [...rows[i].children];
    const li = cells.findIndex(c => timeRe.test((c.innerText || '').trim()));
    if (rowInfo.length < 4) rowInfo.push(cells.length + ':' + (cells[0] ? (cells[0].innerText || '').trim().slice(0, 8) : ''));
    if (li < 0) continue;
    labeled++;
    const day = cells.slice(li + 1);
    if (day.length !== dates.length) { skipped++; continue; }
    const t = (cells[li].innerText || '').trim().padStart(5, '0');
    day.forEach((c, j) => {
      out.push({
        date: dates[j], time: t,
        text: (c.innerText || '').replace(/\s+/g, ''),
        hasLink: !!c.querySelector('a,button,input'),
        alt: [...c.querySelectorAll('img')].map(x => x.alt || '').join('')
      });
    });
  }
  return {slots: out, dates: dates, headerLen: header.length, rowCount: rows.length,
          labeled: labeled, skipped: skipped, rowInfo: rowInfo};
}
"""


def classify(cell):
    """'full' | 'notopen' | 'avail' | 'unknown'"""
    s = cell["text"] + cell["alt"]
    if "×" in s or "✕" in s or "満" in s:
        return "full"
    if "砂時計" in s or "⌛" in s or "⏳" in s:
        return "notopen"
    if s or cell["hasLink"]:
        return "avail"
    return "unknown"


DEBUG = {}


def pick_week(page, target):
    """週表示にして、target日を含む週を選ぶ"""
    try:
        page.get_by_text("週", exact=True).first.click(timeout=5000)
        DEBUG["week_click"] = "ok"
    except Exception as e:
        DEBUG["week_click"] = "失敗: " + str(e)[:60]
    page.wait_for_timeout(2500)
    sel = page.locator("select").filter(has_text=re.compile(r"\d{4}/\d{2}/\d{2}〜"))
    DEBUG["week_select_count"] = sel.count()
    if sel.count() == 0:
        return
    for opt in sel.first.locator("option").all():
        label = opt.inner_text().strip()
        m = re.findall(r"(\d{4})/(\d{2})/(\d{2})", label)
        if len(m) == 2:
            a = dt.date(*map(int, m[0]))
            b = dt.date(*map(int, m[1]))
            if a <= target <= b:
                sel.first.select_option(label=label)
                DEBUG["week_selected"] = label
                try:
                    page.wait_for_load_state("networkidle", timeout=15000)
                except Exception:
                    pass
                page.wait_for_timeout(2500)
                return


def fetch_slots(pw, target, dump=False):
    browser = pw.chromium.launch(headless=True)
    try:
        page = browser.new_page(viewport={"width": 480, "height": 900})
        page.goto(URL, wait_until="networkidle", timeout=60000)
        pick_week(page, target)
        res = page.evaluate(JS_EXTRACT)
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
                if "error" in res:
                    continue
                n = 0
                for c in res["slots"]:
                    if c["date"] == t.strftime("%m/%d"):
                        n += 1
                        print(c["date"], c["time"], repr(c["text"]), classify(c))
                print("該当日の枠数:", n)
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
                            found.append(f'{t.strftime("%m/%d")} {c["time"]}')
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
