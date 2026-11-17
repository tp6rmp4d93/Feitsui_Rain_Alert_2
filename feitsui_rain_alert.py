#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
翡翠水庫集水區雨量警戒 LINE 通知

資料來源：翡翠水庫即時水情資料（JSON 系統介接）
          https://w3.feitsui.gov.tw/FeitsuiWebService/tpe/Feitsui_HourlyJSON.ashx
警戒條件：任一雨量站或集水區平均
          24 小時累積 >= 170 mm  或  3 小時累積 >= 70 mm

JSON 提供的是逐時資料，3 小時 / 24 小時累積由本程式把每小時雨量存進 state.json 後自行加總。
若 JSON 本身已有「3小時」「24小時」累積欄位，則直接採用。

用法：
  python feitsui_rain_alert.py --once       # 只檢查一次（GitHub Actions 排程使用）
  python feitsui_rain_alert.py              # 常駐執行（自有主機使用）
  python feitsui_rain_alert.py --dump       # 印出原始欄位與計算結果，不發訊息（首次部署請先跑這個）
  python feitsui_rain_alert.py --test-line  # 發一則測試訊息到 LINE
  python feitsui_rain_alert.py --dump --file sample.json   # 用另存的 JSON 檔測試
"""
import argparse
import json
import logging
import os
import re
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

BASE_DIR = Path(__file__).resolve().parent
TZ = ZoneInfo("Asia/Taipei")
log = logging.getLogger("feitsui")


# ---------------------------------------------------------------- 設定
def load_env_file(path: Path) -> None:
    """讀取 .env（KEY=VALUE），已存在的環境變數優先。"""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


load_env_file(BASE_DIR / ".env")


def env(key, default=""):
    return os.environ.get(key) or default


SOURCE_URL = env("SOURCE_URL", "https://w3.feitsui.gov.tw/FeitsuiWebService/tpe/Feitsui_HourlyJSON.ashx")
POLL_SECONDS = int(env("POLL_SECONDS", "600"))
TH_3H = float(env("TH_3H", "70"))
TH_24H = float(env("TH_24H", "170"))
RENOTIFY_MINUTES = int(env("RENOTIFY_MINUTES", "180"))   # 持續超標時每隔多久再提醒；0 = 不重複
NOTIFY_CLEAR = env("NOTIFY_CLEAR", "1") == "1"           # 降回警戒值以下時是否通知解除
FAIL_ALERT_AFTER = int(env("FAIL_ALERT_AFTER", "6"))     # 連續失敗幾次後發 LINE 告警
STALE_HOURS = float(env("STALE_HOURS", "3"))             # 最新一筆資料超過幾小時未更新視為異常
VERIFY_SSL = env("VERIFY_SSL", "1") == "1"
STATE_FILE = Path(env("STATE_FILE", str(BASE_DIR / "state.json")))
LINE_TOKEN = env("LINE_CHANNEL_ACCESS_TOKEN")
LINE_TO = [x.strip() for x in env("LINE_TO").split(",") if x.strip()]  # 空白 = broadcast 給所有好友

# hourly     = 雨量欄位是「該小時雨量」（預設）
# cumulative = 雨量欄位是「當日累積雨量」，程式改以前後兩筆相減求出每小時雨量
RAIN_MODE = env("RAIN_MODE", "hourly").lower()

# 欄位名稱比對（JSON 欄位用字不同時，可用環境變數覆寫）
RAIN_KEY = re.compile(env("RAIN_KEY_PATTERN", r"雨|rain"), re.I)
TIME_KEY = re.compile(env("TIME_KEY_PATTERN", r"時間|日期|time|date"), re.I)
STATION_KEY = re.compile(r"站名|測站|站別|station|stname", re.I)
LEVEL_KEY = re.compile(r"水位|waterlevel|water_level", re.I)
CUM_KEY = re.compile(r"累積|累計|本日|日雨量|total|accum|sum", re.I)
KEY_3H = re.compile(env("KEY_3H_PATTERN", r"(?<![\d.])3\s*(?:小時|hrs?)"), re.I)
KEY_24H = re.compile(env("KEY_24H_PATTERN", r"(?<![\d.])24\s*(?:小時|hrs?)"), re.I)

WINDOWS = {
    "3h": {"label": "3小時累積", "threshold": TH_3H, "hours": 3},
    "24h": {"label": "24小時累積", "threshold": TH_24H, "hours": 24},
}
TFMT = "%Y-%m-%dT%H:%M"


# ---------------------------------------------------------------- 抓取與解析
def fetch_json():
    r = requests.get(
        SOURCE_URL,
        timeout=30,
        verify=VERIFY_SSL,
        headers={"User-Agent": "Mozilla/5.0 (feitsui-rain-alert)", "Accept": "application/json"},
    )
    r.raise_for_status()
    return decode_json(r.content)


def decode_json(raw: bytes):
    for enc in ("utf-8-sig", "cp950"):
        try:
            return json.loads(raw.decode(enc))
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
    raise RuntimeError("來源回應不是有效的 JSON（可能被擋或服務異常）")


def to_float(v):
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        t = v.replace(",", "").strip()
        if re.fullmatch(r"-?\d+(?:\.\d+)?", t):
            return float(t)
    return None


def parse_dt(s: str):
    """支援 2026/11/16 14:00、2026-11-16T14:00:00、115/11/16 14:00、20261116140000、/Date(ms)/。"""
    m = re.search(r"Date\((\d{10,13})", s)
    if m:
        ts = int(m.group(1))
        return datetime.fromtimestamp(ts / 1000 if ts > 1e11 else ts, TZ).replace(tzinfo=None)
    m = re.search(r"(\d{2,4})\D+(\d{1,2})\D+(\d{1,2})\D+(\d{1,2})(?:\D+(\d{1,2}))?", s)
    if not m:
        m = re.fullmatch(r"\s*(\d{4})(\d{2})(\d{2})(\d{2})(\d{2})?\d*\s*", s)
    if not m:
        return None
    y, mo, d, h = (int(m.group(i)) for i in range(1, 5))
    mi = int(m.group(5) or 0)
    if y < 1911:
        y += 1911                                   # 民國年
    try:
        return datetime(y, mo, d) + timedelta(hours=h, minutes=mi)   # 容許 24:00
    except ValueError:
        return None


def record_time(rec: dict):
    vals = [str(v) for k, v in rec.items() if TIME_KEY.search(k) and isinstance(v, (str, int))]
    for cand in vals + [" ".join(vals)]:
        dt = parse_dt(cand)
        if dt:
            return dt
    return None


def walk(node):
    if isinstance(node, dict):
        yield node
        for v in node.values():
            yield from walk(v)
    elif isinstance(node, list):
        for v in node:
            yield from walk(v)


def make_label(key: str, station, n_rain_keys: int) -> str:
    core = re.sub(r"時?雨量|累積|累計|本日|24\s*小時|3\s*小時|\(?mm\)?|rainfall|rain|[_\s]", "", key, flags=re.I)
    if station:
        return station if n_rain_keys == 1 or not core else f"{station}{core}"
    return core or "集水區雨量"


def ingest(payload, state: dict, now: datetime, check_stale: bool = True) -> dict:
    """
    把 JSON 內的逐時雨量併入 state["history"]，並算出各站 3hr / 24hr 累積。
    回傳 {"data_time", "stations": {站: {"3h","24h"}}, "coverage": {站: {"3h","24h"}}, "level", "sample"}
    """
    records = []
    for d in walk(payload):
        rains = {}
        for k, v in d.items():
            if RAIN_KEY.search(k) and not TIME_KEY.search(k):
                f = to_float(v)
                if f is not None and f >= 0:        # 負值（如 -999）視為缺測
                    rains[k] = f
        if not rains:
            continue
        t = record_time(d) or now
        station = next((v.strip() for k, v in d.items()
                        if STATION_KEY.search(k) and isinstance(v, str) and v.strip()), None)
        records.append((t.replace(minute=0, second=0, microsecond=0), station, rains, d))
    if not records:
        raise RuntimeError("JSON 中找不到雨量欄位，請用 --dump 檢查欄位名稱並調整 RAIN_KEY_PATTERN")
    records.sort(key=lambda r: r[0])

    history = state.setdefault("history", {})      # {時間: {站: 該小時雨量}}
    last_raw = state.setdefault("last_raw", {})    # cumulative 模式用
    direct = {}                                    # JSON 直接提供的 3hr/24hr 累積
    for t, station, rains, _ in records:
        tk = t.strftime(TFMT)
        for k, v in rains.items():
            label = make_label(k, station, len(rains))
            if KEY_24H.search(k):
                direct.setdefault(tk, {}).setdefault(label, {})["24h"] = v
            elif KEY_3H.search(k):
                direct.setdefault(tk, {}).setdefault(label, {})["3h"] = v
            elif RAIN_MODE == "cumulative":
                prev = last_raw.get(label)
                if prev and tk <= prev["t"]:
                    continue
                # 第一筆沒有前值無法相減，記 0 以免把整天累積當成一小時而誤報
                delta = 0.0 if not prev else (v - prev["v"] if v >= prev["v"] else v)
                last_raw[label] = {"t": tk, "v": v}
                history.setdefault(tk, {})[label] = round(delta, 2)
            elif not CUM_KEY.search(k):
                history.setdefault(tk, {})[label] = v

    if not history and not direct:
        raise RuntimeError("只找到累積型雨量欄位，請設定 RAIN_MODE=cumulative")

    latest_key = max(list(history) + list(direct))
    latest = datetime.strptime(latest_key, TFMT)
    if check_stale and now - latest > timedelta(hours=STALE_HOURS):
        raise RuntimeError(f"來源資料過舊，最新一筆為 {latest:%Y/%m/%d %H:%M}")
    cutoff = (latest - timedelta(hours=30)).strftime(TFMT)
    for k in [k for k in history if k < cutoff]:
        del history[k]

    stations, coverage = {}, {}
    labels = list(dict.fromkeys(l for h in history.values() for l in h))
    for label in labels:
        stations[label], coverage[label] = {}, {}
        for key, w in WINDOWS.items():
            vals = [history.get((latest - timedelta(hours=i)).strftime(TFMT), {}).get(label)
                    for i in range(w["hours"])]
            vals = [v for v in vals if v is not None]
            stations[label][key] = round(sum(vals), 2) if vals else None
            coverage[label][key] = len(vals)
    for label, vals in direct.get(latest_key, {}).items():
        stations.setdefault(label, {"3h": None, "24h": None}).update(vals)
        coverage.setdefault(label, {}).update({k: WINDOWS[k]["hours"] for k in vals})

    last_rec = records[-1][3]
    level = next((to_float(v) for k, v in last_rec.items()
                  if LEVEL_KEY.search(k) and to_float(v) is not None), None)
    return {"data_time": latest.strftime("%Y/%m/%d %H:%M"), "stations": stations,
            "coverage": coverage, "level": level, "sample": last_rec}


# ---------------------------------------------------------------- LINE
def send_line(text: str) -> None:
    if not LINE_TOKEN:
        raise RuntimeError("未設定 LINE_CHANNEL_ACCESS_TOKEN")
    headers = {"Authorization": f"Bearer {LINE_TOKEN}", "Content-Type": "application/json"}
    messages = [{"type": "text", "text": text[:4900]}]
    if LINE_TO:
        for to in LINE_TO:
            r = requests.post(
                "https://api.line.me/v2/bot/message/push",
                headers=headers, json={"to": to, "messages": messages}, timeout=20,
            )
            if r.status_code != 200:
                raise RuntimeError(f"LINE push 失敗: {r.status_code} {r.text}")
    else:
        r = requests.post(
            "https://api.line.me/v2/bot/message/broadcast",
            headers=headers, json={"messages": messages}, timeout=20,
        )
        if r.status_code != 200:
            raise RuntimeError(f"LINE broadcast 失敗: {r.status_code} {r.text}")


# ---------------------------------------------------------------- 狀態
def load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(state: dict) -> None:
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(STATE_FILE)


# ---------------------------------------------------------------- 判斷
def evaluate(result: dict, state: dict, now: float):
    """比對警戒值，回傳 (新達標, 持續超標提醒, 解除)；同時更新 state['alerts']。"""
    alerts = state.setdefault("alerts", {})
    new, remind, cleared = [], [], []
    for name, vals in result["stations"].items():
        for key, w in WINDOWS.items():
            v = vals.get(key)
            if v is None:
                continue                      # 缺值不改變既有狀態
            sid = f"{name}|{key}"
            st = alerts.get(sid, {"active": False, "last_notified": 0})
            if v >= w["threshold"]:
                if not st["active"]:
                    new.append((name, key, v))
                    st = {"active": True, "last_notified": now}
                elif RENOTIFY_MINUTES and now - st["last_notified"] >= RENOTIFY_MINUTES * 60:
                    remind.append((name, key, v))
                    st["last_notified"] = now
            elif st["active"]:
                cleared.append((name, key, v))
                st = {"active": False, "last_notified": 0}
            alerts[sid] = st
    return new, remind, cleared


def build_message(result, new, remind, cleared) -> str:
    data_time = result["data_time"]
    lines = []

    def section(title, items):
        if not items:
            return
        lines.append(title)
        for key, w in WINDOWS.items():
            rows = [(n, v) for n, k, v in items if k == key]
            if rows:
                lines.append(f"▍{w['label']}（警戒值 {w['threshold']:g} mm）")
                for n, v in sorted(rows, key=lambda x: -x[1]):
                    got = result.get("coverage", {}).get(n, {}).get(key, w["hours"])
                    note = f"（僅累計到 {got}/{w['hours']} 小時）" if got < w["hours"] else ""
                    lines.append(f"・{n}：{v:g} mm{note}")

    section("⚠️ 翡翠水庫集水區雨量達警戒值", new)
    if remind:
        if lines:
            lines.append("")
        section("🔁 持續超過警戒值", remind)
    if cleared and NOTIFY_CLEAR:
        if lines:
            lines.append("")
        section("✅ 已降至警戒值以下", cleared)
    lines.append("")
    if result.get("level") is not None:
        lines.append(f"水庫水位：{result['level']:g} m")
    lines += [f"資料時間：{data_time}", "來源：翡翠水庫即時水情資料"]
    return "\n".join(lines)


def now_taipei() -> datetime:
    return datetime.now(TZ).replace(tzinfo=None)


def check_once(state: dict, payload=None) -> None:
    from_file = payload is not None
    if not from_file:
        payload = fetch_json()
    draft = json.loads(json.dumps(state))      # 先在副本上處理，LINE 送出成功才寫回，避免漏發
    result = ingest(payload, draft, now_taipei(), check_stale=not from_file)
    new, remind, cleared = evaluate(result, draft, time.time())
    log.info("資料時間 %s；測站 %d 個；新達標 %d、持續 %d、解除 %d",
             result["data_time"], len(result["stations"]), len(new), len(remind), len(cleared))
    if new or remind or (cleared and NOTIFY_CLEAR):
        send_line(build_message(result, new, remind, cleared))
        log.info("已發送 LINE 通知")
    state.clear()
    state.update(draft)


def run_cycle(state: dict, payload=None) -> bool:
    """執行一次檢查；連續失敗達門檻時發 LINE 告警。回傳本次是否成功。"""
    try:
        check_once(state, payload)
        if state.get("fail_notified"):
            send_line("✅ 翡翠水庫雨量監測已恢復正常")
        state["fails"], state["fail_notified"] = 0, False
        return True
    except Exception as e:  # noqa: BLE001 單次錯誤不可讓監測中止
        state["fails"] = state.get("fails", 0) + 1
        log.error("檢查失敗（連續第 %d 次）：%s", state["fails"], e)
        if state["fails"] >= FAIL_ALERT_AFTER and not state.get("fail_notified"):
            try:
                send_line(f"❗翡翠水庫雨量監測連續 {state['fails']} 次失敗，"
                          f"目前無法判斷警戒狀態。\n最後錯誤：{e}")
                state["fail_notified"] = True
            except Exception as e2:  # noqa: BLE001
                log.error("失敗告警也送不出去：%s", e2)
        return False


# ---------------------------------------------------------------- 主程式
def main() -> int:
    ap = argparse.ArgumentParser(description="翡翠水庫集水區雨量警戒 LINE 通知")
    ap.add_argument("--once", action="store_true", help="只檢查一次")
    ap.add_argument("--dump", action="store_true", help="印出解析結果，不發訊息")
    ap.add_argument("--test-line", action="store_true", help="發送 LINE 測試訊息")
    ap.add_argument("--file", help="改用本機 JSON 檔（測試用）")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    payload = decode_json(Path(args.file).read_bytes()) if args.file else None

    if args.test_line:
        send_line("✅ 翡翠水庫雨量警戒通知：LINE 連線測試成功")
        print("已送出測試訊息")
        return 0

    if args.dump:
        from_file = payload is not None
        result = ingest(payload if from_file else fetch_json(), load_state(), now_taipei(),
                        check_stale=False)
        print("最新一筆原始資料：")
        print(json.dumps(result["sample"], ensure_ascii=False, indent=2))
        print(f"\n資料時間：{result['data_time']}　水位：{result['level']}　RAIN_MODE={RAIN_MODE}")
        print(f"{'測站':<12}{'3小時':>10}{'24小時':>10}   已累計時數")
        for name, v in result["stations"].items():
            c = result["coverage"][name]
            print(f"{name:<12}{str(v['3h']):>10}{str(v['24h']):>10}   {c.get('3h', 0)}/3、{c.get('24h', 0)}/24")
        return 0

    state = load_state()
    if args.once:
        ok = run_cycle(state, payload)
        save_state(state)
        return 0 if ok else 1

    log.info("啟動：每 %d 秒檢查一次；警戒值 3hr>=%g mm、24hr>=%g mm", POLL_SECONDS, TH_3H, TH_24H)
    while True:
        run_cycle(state)
        save_state(state)
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(0)
