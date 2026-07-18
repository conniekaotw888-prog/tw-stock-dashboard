#!/usr/bin/env python3
"""台股儀表板排程・步驟1：合併 fetch_batch*.json，更新兩個持久化 JSON 檔。

用法（路徑用該次 session 的 bash 掛載路徑，每個 session 都不同，不要沿用舊的）：
  python3 tw_dash_update_data.py --repo "<Fable 5 掛載路徑>" --outputs "<outputs 掛載路徑>" --date 2026-07-20

行為：
  1. 合併 outputs/fetch_batch*.json，逐鍵驗證（price.close 為數字、inst 節點含4欄），寫 outputs/all85.json
  2. 備份後把 --date 當天85筆附加進 data/tw-stock-history.json，維持32個交易日滾動視窗
  3. 依 industry 加總產生當天產業記錄，附加進 data/tw-industry-flow.json，同樣32天視窗
補斷點：缺多天時，一天跑一次（舊的先跑），每天先備妥該天的 fetch batch 檔（可用 --batch-glob 指定不同檔名樣式）。

Exit code：0=成功或該日已存在（冪等）；2=batch 資料缺漏/格式錯誤（stdout 列出代號）；其他=例外。
"""
import argparse, glob, json, os, shutil, sys


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


def trim32(recs):
    dates = sorted(set(r["date"] for r in recs))
    while len(dates) > 32:
        oldest = dates.pop(0)
        recs = [r for r in recs if r["date"] != oldest]
    return recs


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True, help="Fable 5 資料夾（bash 掛載路徑）")
    ap.add_argument("--outputs", required=True, help="session outputs（bash 掛載路徑）")
    ap.add_argument("--date", required=True, help="TARGET_DATE，YYYY-MM-DD")
    ap.add_argument("--batch-glob", default="fetch_batch*.json")
    a = ap.parse_args()
    TD = a.date
    tag = TD.replace("-", "")

    hist_p = os.path.join(a.repo, "data/tw-stock-history.json")
    flow_p = os.path.join(a.repo, "data/tw-industry-flow.json")
    hist = json.load(open(hist_p))
    flow = json.load(open(flow_p))
    hd = sorted(set(r["date"] for r in hist))
    fd = sorted(set(r["date"] for r in flow))

    if TD in hd and TD in fd:
        print(f"already updated: {TD} 已存在於兩檔，不動作")
        return 0
    if TD <= hd[-1] and TD not in hd:
        print(f"錯誤：--date {TD} 比現有最新 {hd[-1]} 舊且不存在，不支援回填過去日期")
        return 2

    # 合併 batch 檔
    files = sorted(glob.glob(os.path.join(a.outputs, a.batch_glob)))
    if not files:
        print("錯誤：outputs 裡找不到", a.batch_glob)
        return 2
    m = {}
    for f in files:
        for k, v in json.load(open(f)).items():
            if k in m:
                print(f"錯誤：代號 {k} 在多個 batch 檔重複（{f}）")
                return 2
            m[k] = v

    last_day = [r for r in hist if r["date"] == hd[-1]]
    codes = [r["code"] for r in last_day]
    missing = [c for c in codes if c not in m]
    bad = []
    for c in codes:
        if c in m:
            p, i = m[c].get("price"), m[c].get("inst")
            if not (isinstance(p, dict) and isinstance(p.get("close"), (int, float))):
                bad.append(c + ":price")
            elif not (isinstance(i, dict) and all(k in i for k in ("inst", "instForeign", "instTrust", "instDealer"))):
                bad.append(c + ":inst")
    if missing or bad:
        print("驗證失敗。缺代號:", missing, "格式錯誤:", bad)
        return 2

    json.dump({c: m[c] for c in codes}, open(os.path.join(a.outputs, "all85.json"), "w"), ensure_ascii=False)

    bdir = os.path.join(a.repo, "backups")
    print("backup:", bak(hist_p, bdir, tag), bak(flow_p, bdir, tag))

    new_recs = []
    for r in last_day:
        c = r["code"]
        p, i = m[c]["price"], m[c]["inst"]
        new_recs.append({
            "date": TD, "code": c, "name": r["name"], "industry": r["industry"],
            "close": p["close"], "chg": p["chg"], "vol": p["vol"],
            "inst": i["inst"], "instForeign": i["instForeign"],
            "instTrust": i["instTrust"], "instDealer": i["instDealer"],
        })

    if TD not in hd:
        hist.extend(new_recs)
        hist = trim32(hist)
        json.dump(hist, open(hist_p, "w"), ensure_ascii=False)

    if TD not in fd:
        ind_order = [r["industry"] for r in flow if r["date"] == fd[-1]]
        agg = {}
        for r in new_recs:
            x = agg.setdefault(r["industry"], {"inst": 0, "n": 0})
            if r["close"] is not None:
                x["n"] += 1
            if r["inst"] is not None:
                x["inst"] += r["inst"]
        order = [i for i in ind_order if i in agg] + [i for i in agg if i not in ind_order]
        flow.extend({"date": TD, "industry": i, "inst": agg[i]["inst"], "stockCount": agg[i]["n"]} for i in order)
        flow = trim32(flow)
        json.dump(flow, open(flow_p, "w"), ensure_ascii=False)

    print("hist:", len(hist), "records,", len(set(r["date"] for r in hist)), "dates,",
          len([r for r in hist if r["date"] == TD]), "on", TD)
    print("flow:", len(flow), "records,", len(set(r["date"] for r in flow)), "dates,",
          len([r for r in flow if r["date"] == TD]), "industries on", TD)
    print("null-inst:", [r["code"] for r in new_recs if r["inst"] is None])
    return 0


if __name__ == "__main__":
    sys.exit(main())
