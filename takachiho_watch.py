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
  let header = null, headerIdx = -1;
  for (let i = 0; i < rows.length; i++) {
    const cells = [...rows[i].children];