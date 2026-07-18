#!/usr/bin/env python3
"""台股儀表板排程・步驟2：以部署副本為基底重建 artifact HTML 與 GitHub Pages 部署副本。

前置：tw_dash_update_data.py 已跑完（data/*.json 已到 TARGET_DATE，outputs/all85.json 存在）。

用法（路徑用該次 session 的 bash 掛載路徑）：
  python3 tw_dash_build_html.py --repo "<Fable 5 掛載路徑>" --outputs "<outputs 掛載路徑>" --params "<params.json 路徑>"

params.json 格式（taiex/tpex entries 為陣列；補斷點時放多天、由舊到新）：
{
  "target_date": "2026-07-20",
  "market": {"date": "2026/07/20", "taiex": 0, "taiexChg": 0, "instTotal": 0, "foreign": 0, "invTrust": 0, "dealer": 0},
  "taiex_entries": [{"date": "2026-07-20", "open": 0, "high": 0, "low": 0, "close": 0, "dailyFlow": 0}],
  "tpex_entries":  [{"date": "2026-07-20", "open": 0, "high": 0, "low": 0, "close": 0, "dailyFlow": 0}]
}

行為：讀 repo/deploy/tw-stock-dashboard/index.html → 剝 PWA meta 得 live 基底 → 只替換
MARKET / GROUPS(close,chg,vol,inst) / TAIEX_HISTORY / TPEX_HISTORY(滾動+重算cumFlow) /
STOCK_HISTORY(8欄精簡版) / INDUSTRY_FLOW / STOCK_HISTORY_EXT(全量重建) → node --check →
寫 outputs/tw_dashboard_live.html（live artifact 用，無 PWA meta）→ 備份後寫回部署副本（插回 PWA meta）並驗證。

Exit code：0=成功；3=部署副本已是 target_date（冪等，未改任何檔）；其他=失敗。
"""
import argparse, json, os, re, shutil, subprocess, sys

PWA_BLOCK = '''<title>台股儀表板</title>
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="theme-color" content="#f7f7f8">
<meta http-equiv="Cache-Control" content="no-cache, no-store, must-revalidate">
<meta http-equiv="Pragma" content="no-cache">
<meta http-equiv="Expires" content="0">
<meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="default">
<meta name="apple-mobile-web-app-title" content="台股儀表板">
<link rel="apple-touch-icon" href="icon-180.png">
<link rel="icon" type="image/png" sizes="192x192" href="icon-192.png">
<link rel="manifest" href="manifest.json">'''


def grab_span(s, name):
    i = s.index("const " + name)
    j = s.index("=", i) + 1
    k = j
    while s[k] in " \n":
        k += 1
    openc = s[k]
    closec = {"[": "]", "{": "}"}[openc]
    depth = 0
    instr = False
    x = k
    while x < len(s):
        ch = s[x]
        if instr:
            if ch == "\\":
                x += 2
                continue
            if ch == '"':
                instr = False
        else:
            if ch == '"':
                instr = True
            elif ch == openc:
                depth += 1
            elif ch == closec:
                depth -= 1
                if depth == 0:
                    return k, x + 1
        x += 1
    raise ValueError("unbalanced " + name)


def replace_const(s, name, value_str):
    k, e = grab_span(s, name)
    return s[:k] + value_str + s[e:]


def jd(o):
    return json.dumps(o, ensure_ascii=False)


def bak(src, bdir, tag):
    os.makedirs(bdir, exist_ok=True)
    base = os.path.basename(src) + ".bak-" + tag
    p = os.path.join(bdir, base)
    n = 2
    while os.path.exists(p):
        p = os.path.join(bdir, f"{base}-{n}")
        n += 1
    shutil.copy(src, p)
    return p


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True)
    ap.add_argument("--outputs", required=True)
    ap.add_argument("--params", required=True)
    a = ap.parse_args()
    P = json.load(open(a.params))
    TD = P["target_date"]
    tag = TD.replace("-", "")
    for key in ("market", "taiex_entries", "tpex_entries"):
        assert key in P, "params 缺 " + key
    assert P["market"]["date"] == TD.replace("-", "/"), "market.date 與 target_date 不一致"
    assert set(P["market"].keys()) == {"date", "taiex", "taiexChg", "instTotal", "foreign", "invTrust", "dealer"}
    for e in P["taiex_entries"] + P["tpex_entries"]:
        assert set(e.keys()) == {"date", "open", "high", "low", "close", "dailyFlow"}, "entry 欄位錯誤: " + str(e)
    assert P["taiex_entries"][-1]["date"] == TD and P["tpex_entries"][-1]["date"] == TD

    deploy_p = os.path.join(a.repo, "deploy/tw-stock-dashboard/index.html")
    h = open(deploy_p).read()
    assert PWA_BLOCK in h, "部署副本缺 PWA 區塊（基底異常，停止）"

    # 冪等檢查
    k, e = grab_span(h, "MARKET")
    cur_market = json.loads(h[k:e])
    if cur_market.get("date") == P["market"]["date"]:
        print(f"already updated: 部署副本 MARKET 已是 {TD}，不動作")
        return 3

    # 剝 PWA → live 基底
    h = h.replace("\n" + PWA_BLOCK, "", 1) if ("\n" + PWA_BLOCK) in h else h.replace(PWA_BLOCK, "", 1)
    assert "apple-mobile-web-app-capable" not in h

    all85 = json.load(open(os.path.join(a.outputs, "all85.json")))
    hist = json.load(open(os.path.join(a.repo, "data/tw-stock-history.json")))
    flow = json.load(open(os.path.join(a.repo, "data/tw-industry-flow.json")))
    assert max(r["date"] for r in hist) == TD, "tw-stock-history.json 尚未更新到 target_date，先跑 tw_dash_update_data.py"

    h = replace_const(h, "MARKET", jd(P["market"]))

    k, e = grab_span(h, "GROUPS")
    groups = json.loads(h[k:e])
    nup = 0
    for arr in groups.values():
        for st in arr:
            c = st["code"]
            assert c in all85, "GROUPS 內代號不在 all85: " + c
            p, i = all85[c]["price"], all85[c]["inst"]
            st["close"], st["chg"], st["vol"], st["inst"] = p["close"], p["chg"], p["vol"], i["inst"]
            nup += 1
    h = replace_const(h, "GROUPS", jd(groups))

    def roll(name, entries):
        nonlocal h
        k, e = grab_span(h, name)
        arr = json.loads(h[k:e])
        n0 = len(arr)
        dates = [r["date"] for r in arr] + [x["date"] for x in entries]
        assert dates == sorted(dates) and len(dates) == len(set(dates)), name + " 日期不連續遞增或重複"
        arr.extend(entries)
        del arr[:len(entries)]
        cum = 0.0
        for r in arr:
            cum = round(cum + r["dailyFlow"], 1)
            r["cumFlow"] = cum
        assert len(arr) == n0
        h = replace_const(h, name, jd(arr))
        print(name, "n=", len(arr), "last:", arr[-1])

    roll("TAIEX_HISTORY", P["taiex_entries"])
    roll("TPEX_HISTORY", P["tpex_entries"])

    slim = [{"date": r["date"], "code": r["code"], "name": r["name"], "industry": r["industry"],
             "close": r["close"], "chg": r["chg"], "vol": r["vol"], "inst": r["inst"]} for r in hist]
    h = replace_const(h, "STOCK_HISTORY", jd(slim))
    h = replace_const(h, "INDUSTRY_FLOW", jd(flow))
    ext = {f'{r["date"]}|{r["code"]}': [r["instForeign"], r["instTrust"], r["instDealer"]] for r in hist}
    h = replace_const(h, "STOCK_HISTORY_EXT", jd(ext))

    # 驗證：常數可解析、EXT/SH 對齊、8欄
    for name in ("MARKET", "GROUPS", "TAIEX_HISTORY", "TPEX_HISTORY", "STOCK_HISTORY", "INDUSTRY_FLOW", "STOCK_HISTORY_EXT"):
        json.loads(h[slice(*grab_span(h, name))])
    sh = json.loads(h[slice(*grab_span(h, "STOCK_HISTORY"))])
    ex2 = json.loads(h[slice(*grab_span(h, "STOCK_HISTORY_EXT"))])
    assert len(sh) == len(ex2) == len(hist)
    assert set(r["date"] for r in sh) == set(kk.split("|")[0] for kk in ex2)
    assert all(set(r.keys()) == {"date", "code", "name", "industry", "close", "chg", "vol", "inst"} for r in sh[:50])

    # node --check 內嵌 JS
    scripts = re.findall(r"<script((?![^>]*src)[^>]*)>(.*?)</script>", h, re.S)
    njs = 0
    for idx, (attrs, sc) in enumerate(scripts):
        if "application/json" in attrs:
            json.loads(sc)
            continue
        njs += 1
        p = f"/tmp/twdash_sc{idx}.js"
        open(p, "w").write(sc)
        r = subprocess.run(["node", "--check", p], capture_output=True, text=True)
        assert r.returncode == 0, "node --check 失敗 script %d: %s" % (idx, r.stderr[:400])
    assert njs >= 1, "沒有任何內嵌 JS 被檢查到"

    live_p = os.path.join(a.outputs, "tw_dashboard_live.html")
    open(live_p, "w").write(h)

    # 部署副本：備份 → 插回 PWA → 寫檔 → 驗證
    print("backup:", bak(deploy_p, os.path.join(a.repo, "backups"), tag))
    anchor = '<meta charset="UTF-8">'
    assert anchor in h
    d = h.replace(anchor, anchor + "\n" + PWA_BLOCK, 1)
    open(deploy_p, "w").write(d)
    d2 = open(deploy_p).read()
    assert len(d2.encode()) >= len(h.encode())
    assert d2.count("<script") == h.count("<script") and d2.count("</script>") == h.count("</script>")
    assert "manifest.json" in d2 and "apple-mobile-web-app-capable" in d2

    print("GROUPS updated:", nup, "| STOCK_HISTORY:", len(slim), "| EXT:", len(ext), "| INDUSTRY_FLOW:", len(flow))
    print("live html:", live_p, len(h.encode()), "bytes（用這個檔呼叫 update_artifact）")
    print("deploy 已更新:", deploy_p, len(d2.encode()), "bytes")
    return 0


if __name__ == "__main__":
    sys.exit(main())
