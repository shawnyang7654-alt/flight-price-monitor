#!/usr/bin/env python3
"""Ctrip round-trip fare monitor.

Monitors the total round-trip fare shown on Ctrip's round-trip result page.
Designed for GitHub Actions: state/history are persisted under docs/data and
committed by the workflow.
"""
import argparse
import json
import re
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from playwright.sync_api import sync_playwright


def build_url(from_code: str, to_code: str, depart_date: str, return_date: str) -> str:
    return (
        "https://flights.ctrip.com/online/list/"
        f"round-{from_code.lower()}-{to_code.lower()}"
        f"?depdate={depart_date}_{return_date}"
        "&cabin=y_s_c_f&adult=1&child=0&infant=0"
    )


def parse_prices(text: str) -> list[int]:
    """Extract plausible CNY fares from one rendered flight card."""
    values = []
    for m in re.finditer(r"[¥￥]\s*([\d,]{3,7})", text or ""):
        try:
            value = int(m.group(1).replace(",", ""))
        except ValueError:
            continue
        if 500 <= value <= 100000:
            values.append(value)
    return values


def extract_lowest_roundtrip(page) -> tuple[int | None, list[dict], str]:
    """Return the cheapest round-trip total from rendered Ctrip flight cards.

    Ctrip's round-trip outbound list displays one total round-trip fare on each
    .flight-item card. We deliberately do not scan the whole page for numbers:
    unrelated coupons/add-ons must never trigger a low-price alert.
    """
    try:
        state = page.evaluate("""() => {
          const t = document.body?.innerText || '';
          if (location.pathname.includes('captcha') || /验证码|安全验证|verify the human/i.test(t)) {
            return 'captcha';
          }
          return document.querySelectorAll('.flight-item').length ? 'content' : 'loading';
        }""")
        for _ in range(18):
            if state != "loading":
                break
            page.wait_for_timeout(1000)
            state = page.evaluate("""() => {
              const t = document.body?.innerText || '';
              if (location.pathname.includes('captcha') || /验证码|安全验证|verify the human/i.test(t)) {
                return 'captcha';
              }
              return document.querySelectorAll('.flight-item').length ? 'content' : 'loading';
            }""")
    except Exception:
        state = "loading"

    if state == "captcha":
        return None, [], "captcha"
    if state != "content":
        return None, [], "no_cards"

    last_count = -1
    stable_rounds = 0
    for _ in range(10):
        cards = page.locator(".flight-item")
        count = cards.count()
        if count == last_count:
            stable_rounds += 1
        else:
            stable_rounds = 0
            last_count = count
        page.mouse.wheel(0, 2600)
        page.wait_for_timeout(1200)
        if stable_rounds >= 2:
            break

    rows = []
    cards = page.locator(".flight-item")
    for i in range(cards.count()):
        try:
            text = cards.nth(i).inner_text(timeout=3000)
        except Exception:
            continue
        prices = parse_prices(text)
        if not prices:
            continue
        # The first currency amount on the outbound card is its displayed
        # round-trip total. Do not choose the smallest arbitrary number.
        rows.append({"price": prices[0], "text": text[:1000]})

    if not rows:
        return None, [], "parse_failed"
    return min(r["price"] for r in rows), rows, "ok"


def send_serverchan(send_key: str, title: str, desp: str) -> bool:
    if not send_key:
        return False
    url = f"https://sctapi.ftqq.com/{send_key}.send"
    data = urllib.parse.urlencode({"title": title[:60], "desp": desp[:32000]}).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            obj = json.loads(resp.read().decode("utf-8", "ignore"))
        return obj.get("code") == 0
    except Exception as exc:
        print(f"[warn] Server酱推送失败: {exc}")
        return False


def load_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def save_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser(description="携程国际/国内往返总价监控")
    ap.add_argument("--from-code", required=True)
    ap.add_argument("--from-name", required=True)
    ap.add_argument("--to-code", required=True)
    ap.add_argument("--to-name", required=True)
    ap.add_argument("--depart-date", required=True)
    ap.add_argument("--return-date", required=True)
    ap.add_argument("--threshold", type=float, required=True)
    ap.add_argument("--serverchan-key", default="")
    ap.add_argument("--state-file", default="docs/data/roundtrip_state.json")
    ap.add_argument("--history-file", default="docs/data/roundtrip_history.json")
    ap.add_argument("--latest-file", default="docs/data/roundtrip_latest.json")
    ap.add_argument("--drop-min", type=float, default=30)
    args = ap.parse_args()

    url = build_url(args.from_code, args.to_code, args.depart_date, args.return_date)
    print(f"[roundtrip] GET {url}")

    lowest = None
    rows = []
    parse_status = "unknown"
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-blink-features=AutomationControlled"],
        )
        page = browser.new_page(
            viewport={"width": 1440, "height": 1200},
            locale="zh-CN",
            timezone_id="Asia/Shanghai",
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/126.0.0.0 Safari/537.36"
            ),
        )
        page.add_init_script("Object.defineProperty(navigator,'webdriver',{get:()=>undefined});")
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(10000)
            lowest, rows, parse_status = extract_lowest_roundtrip(page)
            if parse_status != "ok":
                debug_dir = Path("debug")
                debug_dir.mkdir(parents=True, exist_ok=True)
                try:
                    page.screenshot(path=str(debug_dir / "ctrip_roundtrip.png"), full_page=True)
                except Exception as exc:
                    print(f"[warn] 保存截图失败: {exc}")
                try:
                    (debug_dir / "ctrip_roundtrip.html").write_text(page.content(), encoding="utf-8")
                except Exception as exc:
                    print(f"[warn] 保存HTML失败: {exc}")
                try:
                    (debug_dir / "ctrip_roundtrip.txt").write_text(
                        page.locator("body").inner_text(timeout=5000),
                        encoding="utf-8",
                    )
                except Exception as exc:
                    print(f"[warn] 保存页面文本失败: {exc}")
        finally:
            browser.close()

    now = datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d %H:%M:%S")
    latest = {
        "from": args.from_code.upper(),
        "from_name": args.from_name,
        "to": args.to_code.upper(),
        "to_name": args.to_name,
        "depart_date": args.depart_date,
        "return_date": args.return_date,
        "roundtrip_total": lowest,
        "threshold": args.threshold,
        "source": "ctrip",
        "fetched_at": now,
        "url": url,
        "cards_parsed": len(rows),
        "status": parse_status,
    }
    save_json(Path(args.latest_file), latest)

    history_path = Path(args.history_file)
    history = load_json(history_path, [])
    history.append(latest)
    save_json(history_path, history[-500:])

    state_path = Path(args.state_file)
    state = load_json(state_path, {})
    last_alert = state.get("last_alert_price")

    if lowest is None:
        print(f"[roundtrip] 未解析到往返总价（status={parse_status}）；可能尚未开售、被风控或页面结构变化。")
        state.update({"last_checked_at": now, "last_price": None})
        save_json(state_path, state)
        return 0

    print(
        f"[roundtrip] {args.from_name}⇄{args.to_name} "
        f"{args.depart_date}→{args.return_date} 最低往返总价 ¥{lowest}"
    )

    should_push = False
    reason = ""
    if args.threshold > 0 and lowest <= args.threshold:
        if last_alert is None:
            should_push = True
            reason = "首次跌破阈值"
        elif lowest <= float(last_alert) - args.drop_min:
            should_push = True
            reason = f"较上次提醒再降 ¥{float(last_alert) - lowest:.0f}"
        else:
            reason = "已低于阈值，但未比上次提醒明显更低"
    elif lowest > args.threshold:
        state["last_alert_price"] = None
        reason = "高于阈值"

    print(f"[roundtrip] 阈值 ¥{args.threshold:.0f} -> {reason}")

    if should_push:
        title = (
            f"✈️ {args.from_name}⇄{args.to_name} 往返 "
            f"¥{lowest:.0f} ≤ ¥{args.threshold:.0f}"
        )
        desp = (
            "## 携程往返低价提醒\n\n"
            f"- **行程**：{args.from_name}（{args.from_code.upper()}） ⇄ "
            f"{args.to_name}（{args.to_code.upper()}）\n"
            f"- **去程**：{args.depart_date}\n"
            f"- **返程**：{args.return_date}\n"
            f"- **当前最低往返总价**：**¥{lowest:.0f}**\n"
            f"- **目标价**：¥{args.threshold:.0f}\n"
            f"- **触发原因**：{reason}\n"
            f"- **抓取时间**：{now}\n\n"
            f"[👉 打开携程往返搜索]({url})\n\n"
            "> 价格以携程最终下单页为准；当前监控的是往返结果页展示的含税总价。"
        )
        if send_serverchan(args.serverchan_key, title, desp):
            print("[roundtrip] Server酱推送成功")
            state["last_alert_price"] = lowest
            state["last_alert_at"] = now
        else:
            print("[roundtrip] 满足阈值，但 Server酱未配置或推送失败")

    state.update({"last_checked_at": now, "last_price": lowest})
    save_json(state_path, state)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
