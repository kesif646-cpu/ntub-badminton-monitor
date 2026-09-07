# -*- coding: utf-8 -*-
from __future__ import annotations

import json
import re
import sys
import traceback
from datetime import datetime, timezone, timedelta
from pathlib import Path
from html import escape

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

URL = "https://ntcbadm1.ntub.edu.tw/pub/TchSchedule_Search.aspx"

COURSE_ID = "90240991"
COURSE_NAME = "羽球"
COURSE_NAME_FULL = "羽球(上)"
TEACHER = "黃晉揚"
CLASS_CODE = "0328"
COURSE_DESC = "體育興趣選項(丙)(臺北) 40 90240991 羽球(上) 0328 黃晉揚 401 402/體育館 羽球"

ROOT = Path(__file__).resolve().parent
STATE_FILE = ROOT / "state.json"
SITE_DIR = ROOT / "site"
DEBUG_HTML = SITE_DIR / "debug.html"
DEBUG_PNG = SITE_DIR / "debug.png"

TZ = timezone(timedelta(hours=8))

COUNT_HEADER_PATTERNS = [
    r"已選.*人數", r"選課.*人數", r"選課人數",
    r"修課.*人數", r"修課人數",
    r"實選.*人數", r"實選人數",
    r"目前.*人數", r"目前人數",
    r"已選數", r"選課數", r"修課數", r"實選數",
]
COUNT_HEADER_EXCLUDE = ["限修", "限額", "容量", "名額", "最低", "最高"]


def now_tw() -> str:
    return datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S")


def clean(s: str | None) -> str:
    return re.sub(r"\s+", " ", s or "").strip()


def load_state() -> dict:
    if not STATE_FILE.exists():
        return {"last_count": None, "history": []}
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        data.setdefault("last_count", None)
        data.setdefault("history", [])
        return data
    except Exception:
        return {"last_count": None, "history": []}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(
        json.dumps(state, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def header_is_count(text: str) -> bool:
    h = clean(text)
    if not h:
        return False
    if any(x in h for x in COUNT_HEADER_EXCLUDE):
        return False
    return any(re.search(p, h) for p in COUNT_HEADER_PATTERNS)


def one_int(text: str) -> int | None:
    nums = re.findall(r"(?<!\\d)(\\d{1,3})(?!\\d)", clean(text))
    return int(nums[0]) if len(nums) == 1 else None


def count_from_cell(header: str, text: str) -> int | None:
    """
    北商目前實際欄名是「上限/已選人數」。
    例如儲存格若顯示「40 / 38」，前者是上限、後者才是已選人數，
    因此這種複合欄位固定取最後一個數字。
    """
    h = clean(header)
    value_text = clean(text)
    nums = [int(x) for x in re.findall(r"(?<!\\d)(\\d{1,3})(?!\\d)", value_text)]

    if ("已選" in h or "選課" in h or "修課" in h or "實選" in h):
        if "上限" in h:
            return nums[-1] if nums else None
        if len(nums) == 1:
            return nums[0]
        # 若標題明確是人數欄但畫面用了「上限/已選」類似格式，仍取最後一個數字
        if nums:
            return nums[-1]

    return one_int(value_text)


def visible_text_inputs(page):
    loc = page.locator("input")
    out = []
    for i in range(loc.count()):
        el = loc.nth(i)
        try:
            if not el.is_visible():
                continue
            typ = (el.get_attribute("type") or "text").lower()
            if typ not in ("text", "search", ""):
                continue
            out.append(el)
        except Exception:
            pass
    return out


def input_context(el) -> str:
    try:
        return clean(el.evaluate("""e => {
          let a=[], n=e;
          for(let i=0;i<4 && n;i++, n=n.parentElement) {
            if(n.innerText) a.push(n.innerText);
          }
          return a.join(" | ");
        }"""))
    except Exception:
        return ""


def find_keyword_input(page):
    scored = []
    for el in visible_text_inputs(page):
        try:
            attrs = " ".join(filter(None, [
                el.get_attribute("id"),
                el.get_attribute("name"),
                el.get_attribute("placeholder"),
                el.get_attribute("title"),
            ]))
            ctx = input_context(el)
            score = 100
            if "課程關鍵字" in ctx:
                score -= 80
            if re.search(r"course|subject|keyword|crs|key", attrs, re.I):
                score -= 25
            if "開課資訊" in ctx and "課程關鍵字" not in ctx:
                score += 20
            scored.append((score, el, attrs, ctx))
        except Exception:
            pass

    if not scored:
        raise RuntimeError("找不到可輸入「課程關鍵字」的欄位。")

    scored.sort(key=lambda x: x[0])
    return scored[0][1]


def click_search(page):
    candidates = []
    for selector in ["input[type=submit]", "input[type=button]", "button", "a"]:
        loc = page.locator(selector)
        for i in range(loc.count()):
            el = loc.nth(i)
            try:
                if not el.is_visible():
                    continue
                txt = clean(" ".join(filter(None, [
                    el.inner_text() if selector != "input[type=submit]" and selector != "input[type=button]" else "",
                    el.get_attribute("value"),
                    el.get_attribute("title"),
                ])))
                if any(k in txt for k in ["查詢", "搜尋", "查課"]) or "search" in txt.lower():
                    if not any(bad in txt for bad in ["清除", "重設", "離開", "登出"]):
                        score = 0 if "查詢" in txt else 1
                        candidates.append((score, el, txt))
            except Exception:
                pass

    candidates.sort(key=lambda x: x[0])
    if not candidates:
        raise RuntimeError("找不到「查詢」按鈕。")

    last = None
    for _, el, txt in candidates:
        try:
            el.click(timeout=5000)
            return txt
        except Exception as e:
            last = e
    raise RuntimeError(f"查詢按鈕存在，但無法按下：{last}")


def target_row(page):
    """
    只接受真正的課程資料列。
    北商頁面外層也有 <tr> 會包住整張結果表；如果只看 inner_text，
    外層列也會同時包含 90240991、黃晉揚、羽球，造成誤選。
    實際課程資料列固定有 10 個直接 td/th 欄位，因此以此排除外層容器列。
    """
    rows = page.locator("tr")
    matches = []

    for i in range(rows.count()):
        row = rows.nth(i)

        try:
            direct_cells = row.locator(":scope > th, :scope > td")
            cell_count = direct_cells.count()

            # 北商目前結果表每一筆課程是 10 欄。
            # 外層 wrapper 通常只有 1 格，直接排除。
            if cell_count != 10:
                continue

            cell_texts = [clean(direct_cells.nth(j).inner_text()) for j in range(cell_count)]
            row_text = " ".join(cell_texts)

        except Exception:
            continue

        # 精準鎖定指定課程
        if (
            COURSE_ID in row_text
            and COURSE_NAME_FULL in row_text
            and TEACHER in row_text
            and CLASS_CODE in row_text
            and "體育興趣選項(丙)(臺北)" in row_text
        ):
            # 再用欄位位置確認，避免其他欄文字剛好包含相同關鍵字
            if (
                cell_texts[0] == "體育興趣選項(丙)(臺北)"
                and cell_texts[2] == COURSE_ID
                and COURSE_NAME_FULL in cell_texts[3]
                and TEACHER in cell_texts[4]
            ):
                matches.append((row, row_text))

    if not matches:
        raise RuntimeError(
            f"已執行查詢，但找不到精準課程資料列："
            f"體育興趣選項(丙)(臺北) / {COURSE_ID} / {COURSE_NAME_FULL} / {TEACHER}"
        )

    return matches[0][0], matches[0][1]


def extract_count(row):
    cells = row.locator(":scope > th, :scope > td")
    n = cells.count()
    if n == 0:
        raise RuntimeError("找到課程列，但該列沒有欄位。")

    texts = [clean(cells.nth(i).inner_text()) for i in range(n)]

    # 找所在 table
    table = row.locator("xpath=ancestor::table[1]")
    trs = table.locator("tr")

    row_index = None
    for i in range(trs.count()):
        try:
            if trs.nth(i).evaluate("(a,b)=>a===b", row.element_handle()):
                row_index = i
                break
        except Exception:
            pass

    # 先找表頭；只接受「明確像選課人數」的欄位名稱
    header_rows = []
    if row_index is not None:
        for i in range(max(0, row_index - 8), row_index):
            tr = trs.nth(i)
            hc = tr.locator(":scope > th, :scope > td")
            if hc.count() == n:
                heads = [clean(hc.nth(j).inner_text()) for j in range(n)]
                if any(header_is_count(x) for x in heads):
                    header_rows.append(heads)

    thead_rows = table.locator("thead tr")
    for i in range(thead_rows.count()):
        hc = thead_rows.nth(i).locator(":scope > th, :scope > td")
        if hc.count() == n:
            heads = [clean(hc.nth(j).inner_text()) for j in range(n)]
            if any(header_is_count(x) for x in heads):
                header_rows.append(heads)

    for heads in reversed(header_rows):
        for idx, h in enumerate(heads):
            if header_is_count(h):
                val = count_from_cell(h, texts[idx])
                if val is not None:
                    return val, f"欄位「{h}」"

    # responsive table 的 data-label/data-title
    for i in range(n):
        cell = cells.nth(i)
        meta = clean(" ".join(filter(None, [
            cell.get_attribute("data-label"),
            cell.get_attribute("data-title"),
            cell.get_attribute("aria-label"),
            cell.get_attribute("title"),
        ])))
        if header_is_count(meta):
            val = count_from_cell(meta, texts[i])
            if val is not None:
                return val, f"欄位「{meta}」"

    # 欄位內容直接寫「選課人數：xx」
    for t in texts:
        m = re.search(r"(?:選課|已選|修課|實選).*?人數[^\d]{0,8}(\d{1,3})", t)
        if m:
            return int(m.group(1)), f"文字「{t}」"

    diag = " | ".join(f"[{i}] {t}" for i, t in enumerate(texts))
    raise RuntimeError(
        "已找到正確羽球課程，但無法可靠判斷哪一欄是「選課人數」。"
        "為避免把班級代碼、容量或教室號碼誤當人數，本次不更新。"
        f" 課程列：{diag}"
    )


def scrape():
    SITE_DIR.mkdir(exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(
            viewport={"width": 1440, "height": 1000},
            locale="zh-TW",
        )
        page.set_default_timeout(15000)

        try:
            queries = [COURSE_NAME, COURSE_NAME_FULL, COURSE_ID]
            errors = []
            found = None

            for query in queries:
                try:
                    page.goto(URL, wait_until="domcontentloaded", timeout=45000)
                    page.wait_for_timeout(900)

                    # 北商這個欄位名稱雖然叫「課程關鍵字」，
                    # 實測輸入課號 90240991 會得到空表，因此優先用「羽球」查詢。
                    inp = page.locator("#CosNamekeyWord")
                    if inp.count() == 0:
                        inp = find_keyword_input(page)

                    inp.fill(query)

                    # 明確按網站的「查詢」按鈕，不再用「頁面含 90240991」
                    # 判斷是否查到，因為輸入框本身就會含該字串而造成誤判。
                    btn = page.locator("#btnSearch")
                    if btn.count() and btn.first.is_visible():
                        btn.first.click(timeout=5000)
                    else:
                        click_search(page)

                    page.wait_for_load_state("domcontentloaded", timeout=20000)
                    page.wait_for_timeout(1800)

                    try:
                        row, row_text = target_row(page)
                        found = (row, row_text, query)
                        break
                    except Exception as exc:
                        errors.append(f"{query}: {exc}")

                except Exception as exc:
                    errors.append(f"{query}: {type(exc).__name__}: {exc}")

            if found is None:
                raise RuntimeError(
                    "已依序用「羽球」、「羽球(上)」、課號 90240991 查詢，"
                    "仍找不到指定課程。 " + " | ".join(errors[-3:])
                )

            row, row_text, query_used = found
            count, source = extract_count(row)

            return {
                "ok": True,
                "count": count,
                "source": f"{source}；查詢關鍵字「{query_used}」",
                "row_text": row_text,
                "checked_at": now_tw(),
            }

        except Exception:
            try:
                DEBUG_HTML.write_text(page.content(), encoding="utf-8")
            except Exception:
                pass
            try:
                page.screenshot(path=str(DEBUG_PNG), full_page=True)
            except Exception:
                pass
            raise
        finally:
            browser.close()

def render_page(result: dict, state: dict):
    SITE_DIR.mkdir(exist_ok=True)

    ok = result.get("ok", False)
    count = result.get("count")
    checked = escape(result.get("checked_at", now_tw()))
    err = escape(result.get("error", ""))
    source = escape(result.get("source", ""))
    last_count = state.get("last_count")
    history = state.get("history", [])[-10:][::-1]

    if ok:
        badge = "正常"
        headline = f"{count} 人"
        sub = "目前選課人數"
        status_class = "ok"
    else:
        badge = "檢查失敗"
        headline = "—"
        sub = "本次沒有更新人數"
        status_class = "error"

    history_html = ""
    if history:
        rows = []
        for item in history:
            old = item.get("from")
            new = item.get("to")
            t = escape(str(item.get("at", "")))
            if old is None:
                desc = f"建立基準：{new} 人"
            else:
                desc = f"{old} → {new} 人"
            rows.append(f"<li><strong>{escape(desc)}</strong><span>{t}</span></li>")
        history_html = "\n".join(rows)
    else:
        history_html = "<li><strong>尚無人數變動紀錄</strong><span>第一次成功檢查後會建立基準</span></li>"

    error_box = ""
    if not ok:
        error_box = f"""
        <section class="card errorbox">
          <h2>本次檢查訊息</h2>
          <p>{err}</p>
          <p class="small">如果 GitHub Actions 內有 debug.png，可用來確認北商頁面是否改版。</p>
        </section>
        """

    html = f"""<!doctype html>
<html lang="zh-Hant">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="theme-color" content="#101319">
<title>北商羽球選課監控</title>
<style>
*{{box-sizing:border-box}}
body{{margin:0;background:#0d1015;color:#f4f7fb;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","Noto Sans TC",sans-serif}}
.wrap{{max-width:720px;margin:auto;padding:20px 16px 48px}}
.top{{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-bottom:18px}}
h1{{font-size:21px;margin:0}}
.badge{{padding:7px 11px;border-radius:999px;font-size:13px;font-weight:700}}
.ok{{background:#173b2a;color:#8cf0b5}}
.error{{background:#442126;color:#ffabb4}}
.card{{background:#171b22;border:1px solid #282e38;border-radius:20px;padding:20px;margin:14px 0;box-shadow:0 8px 30px rgba(0,0,0,.16)}}
.hero{{text-align:center;padding:30px 20px}}
.big{{font-size:64px;font-weight:800;letter-spacing:-2px;line-height:1.05;margin:8px 0}}
.label{{color:#aeb7c4;font-size:15px}}
.course{{line-height:1.75;color:#d7dde6}}
.meta{{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-top:18px}}
.meta div{{background:#11151b;border-radius:14px;padding:13px}}
.meta span{{display:block;color:#8e99a8;font-size:12px;margin-bottom:5px}}
.meta strong{{font-size:14px}}
h2{{font-size:16px;margin:0 0 14px}}
ul{{list-style:none;padding:0;margin:0}}
li{{display:flex;justify-content:space-between;gap:12px;padding:13px 0;border-top:1px solid #292f38}}
li:first-child{{border-top:0}}
li span{{color:#8e99a8;font-size:12px;text-align:right}}
.small{{font-size:12px;color:#8e99a8;line-height:1.6}}
.errorbox{{border-color:#583039}}
a{{color:#a9c7ff}}
@media(max-width:440px){{
  .big{{font-size:54px}}
  .meta{{grid-template-columns:1fr}}
  li{{display:block}}
  li span{{display:block;text-align:left;margin-top:4px}}
}}
</style>
</head>
<body>
<div class="wrap">
  <div class="top">
    <h1>🏸 北商羽球選課監控</h1>
    <span class="badge {status_class}">{badge}</span>
  </div>

  <section class="card hero">
    <div class="label">{sub}</div>
    <div class="big">{headline}</div>
    <div class="label">最後檢查：{checked}</div>
  </section>

  <section class="card">
    <h2>監控課程</h2>
    <div class="course">
      <strong>90240991　羽球(上)</strong><br>
      0328　黃晉揚<br>
      體育興趣選項(丙)(臺北)<br>
      401 402／體育館 羽球
    </div>
    <div class="meta">
      <div><span>檢查頻率</span><strong>每 5 分鐘</strong></div>
      <div><span>資料來源</span><strong>{source or "北商課程查詢"}</strong></div>
    </div>
  </section>

  {error_box}

  <section class="card">
    <h2>最近人數變化</h2>
    <ul>{history_html}</ul>
  </section>

  <p class="small">
    此頁面由 GitHub Actions 自動更新。排程服務可能偶爾延遲，因此「每 5 分鐘」代表預定檢查頻率，
    並非保證精確到秒。
  </p>
</div>
</body>
</html>"""
    (SITE_DIR / "index.html").write_text(html, encoding="utf-8")
    (SITE_DIR / "status.json").write_text(
        json.dumps({
            "ok": ok,
            "count": count,
            "checked_at": result.get("checked_at"),
            "error": result.get("error"),
            "course_id": COURSE_ID,
            "course_name": COURSE_NAME_FULL,
            "teacher": TEACHER,
        }, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )


def main():
    state = load_state()
    state_changed = False

    try:
        result = scrape()
        current = result["count"]
        previous = state.get("last_count")

        if previous is None:
            state["last_count"] = current
            state.setdefault("history", []).append({
                "from": None,
                "to": current,
                "at": result["checked_at"],
            })
            state_changed = True
        elif int(previous) != int(current):
            state.setdefault("history", []).append({
                "from": int(previous),
                "to": int(current),
                "at": result["checked_at"],
            })
            state["last_count"] = current
            state_changed = True

        state["history"] = state.get("history", [])[-50:]

    except Exception as exc:
        result = {
            "ok": False,
            "count": state.get("last_count"),
            "checked_at": now_tw(),
            "error": f"{type(exc).__name__}: {exc}",
        }
        print(traceback.format_exc(), file=sys.stderr)

    render_page(result, state)

    if state_changed:
        save_state(state)
        print("STATE_CHANGED=1")
    else:
        print("STATE_CHANGED=0")

    print(json.dumps(result, ensure_ascii=False))
    return 0 if result.get("ok") else 2


if __name__ == "__main__":
    raise SystemExit(main())
