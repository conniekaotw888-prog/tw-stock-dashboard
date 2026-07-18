#!/usr/bin/env python3
"""GitHub Actions 用：純 stdlib（urllib）直接呼叫 FinMind API，抓取台股儀表板每日更新所需資料，
不需要瀏覽器、不需要 subagent。取代 Cowork 排程裡「開瀏覽器分頁 same-origin fetch + 派 subagent」
那一段，其餘（tw_dash_update_data.py / tw_dash_build_html.py 的合併與建置邏輯）完全沿用不改。

欄位名稱與公式已於 2026-07-18 用 Chrome javascript_tool 直接打 FinMind API 實測確認，
不是憑文件猜的：
- TaiwanStockPrice: date/stock_id/open/max/min/close/spread/Trading_Volume（TAIEX、TPEx、個股皆同格式）
- TaiwanStockInstitutionalInvestorsBuySell（個股）: date/stock_id/name(分類)/buy/sell
- TaiwanStockTotalInstitutionalInvestors（大盤合計）: date/name(分類，含"total")/buy/sell

用法：
  python3 gh_fetch.py --repo . --outputs ./_work [--token FINMIND_TOKEN]

行為：
  1. 用 TAIEX 從今天往前找到最近有資料的交易日 = LATEST_AVAILABLE。
  2. 對照 data/tw-stock-history.json 目前最新日期 HD_LAST，算出待補日期清單
     MISSING = (HD_LAST, LATEST_AVAILABLE] 之間的交易日（由舊到新）。
     若 MISSING 為空，印 "already updated" 並結束（exit 0，不寫檔）。
  3. 對 85 檔股票 + 需要另抓的權值股（2881,8299,6274,6488,5347,3529,3081,6147）各抓一個
     涵蓋 (HD_LAST, LATEST_AVAILABLE] 的日期區間（每檔固定2個request，不因補天數增加而變多）。
  4. 依 MISSING 逐日切出當天資料，寫 outputs/fetch_batch_<date>.json（85檔，給 tw_dash_update_data.py 用）
     與 outputs/params_<date>.json（給 tw_dash_build_html.py 用）。
  5. 印出 MISSING 清單（空白分隔，YYYY-MM-DD）到 stdout 最後一行，workflow 用這行逐日呼叫兩支腳本。

Exit code：0=成功（含「已是最新」的情況）；2=抓資料失敗或驗證不過。
"""
import argparse, datetime, json, os, sys, time, urllib.request, urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed

FINMIND = "https://api.finmindtrade.com/api/v4/data"

# 上市權值股10檔（部分B用），其中 2881 不在85檔，要另抓
TAIEX_TOP10 = ["2330", "2454", "2308", "2317", "3711", "2327", "2303", "2383", "2881", "3037"]
# 上櫃權值股10檔（部分B用），其中這幾檔不在85檔，要另抓
TPEX_TOP10 = ["5274", "6223", "8299", "6274", "6488", "5347", "3529", "8069", "3081", "6147"]


def http_get_json(url, retries=3, timeout=30):
    last_err = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "tw-stock-dashboard-gh-actions/1.0"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as e:
            last_err = e
            time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"GET failed after {retries} tries: {url}: {last_err}")


def fmind(dataset, data_id=None, start_date=None, end_date=None, token=None):
    params = {"dataset": dataset}
    if data_id:
        params["data_id"] = data_id
    if start_date:
        params["start_date"] = start_date
    if end_date:
        params["end_date"] = end_date
    if token:
        params["token"] = token
    url = FINMIND + "?" + urllib.parse.urlencode(params)
    j = http_get_json(url)
    if j.get("status") != 200:
        raise RuntimeError(f"FinMind status {j.get('status')} for {dataset}/{data_id}: {j.get('msg')}")
    return j.get("data", [])


def round_lots(x):
    """股數轉張數，四捨五入"""
    return int(round(x / 1000.0))


def _split_categories(rows):
    by_name = {r["name"]: (r["buy"] - r["sell"]) for r in rows}

    def g(*names):
        return sum(by_name.get(n, 0) for n in names)

    foreign = g("Foreign_Investor", "Foreign_Dealer_Self")
    trust = g("Investment_Trust")
    dealer = g("Dealer_self", "Dealer_Hedging")
    total = by_name.get("total")
    if total is None:
        total = foreign + trust + dealer
    return foreign, trust, dealer, total


def inst_from_rows(rows):
    """個股 TaiwanStockInstitutionalInvestorsBuySell 專用：buy/sell 單位是「股」，
    轉成 {inst, instForeign, instTrust, instDealer}（單位：張，(buy-sell)/1000 四捨五入）"""
    foreign, trust, dealer, total = _split_categories(rows)
    return {
        "inst": round_lots(total),
        "instForeign": round_lots(foreign),
        "instTrust": round_lots(trust),
        "instDealer": round_lots(dealer),
    }


def total_inst_billions(rows):
    """大盤 TaiwanStockTotalInstitutionalInvestors 專用：buy/sell 單位是「元」，
    轉成 {inst, instForeign, instTrust, instDealer}（單位：億元，(buy-sell)/1e8，四捨五入到小數1位）"""
    foreign, trust, dealer, total = _split_categories(rows)
    r = lambda x: round(x / 1e8, 1)
    return {"inst": r(total), "instForeign": r(foreign), "instTrust": r(trust), "instDealer": r(dealer)}


def trading_days_between(all_dates_available, after, upto):
    """all_dates_available: 某檔TaiwanStockPrice在區間內實際回傳的日期集合（用來當交易日曆），
    回傳 after < d <= upto 的排序清單"""
    return sorted(d for d in all_dates_available if d > after and d <= upto)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True, help="repo checkout 路徑（GitHub Actions 裡通常是 '.'）")
    ap.add_argument("--outputs", required=True, help="暫存輸出資料夾，會自動建立")
    ap.add_argument("--token", default=os.environ.get("FINMIND_TOKEN", ""), help="FinMind token（選填，可拉高速率上限）")
    ap.add_argument("--max-workers", type=int, default=6)
    a = ap.parse_args()
    token = a.token or None
    os.makedirs(a.outputs, exist_ok=True)

    hist_p = os.path.join(a.repo, "data/tw-stock-history.json")
    hist = json.load(open(hist_p, encoding="utf-8"))
    hd_dates = sorted(set(r["date"] for r in hist))
    hd_last = hd_dates[-1]
    last_day_recs = [r for r in hist if r["date"] == hd_last]
    codes = sorted(set(r["code"] for r in last_day_recs))
    code_info = {r["code"]: {"name": r["name"], "industry": r["industry"]} for r in last_day_recs}
    print(f"data/tw-stock-history.json 最新日期: {hd_last}，85檔代號數: {len(codes)}")

    # 1. 找 LATEST_AVAILABLE：TAIEX 最近7天內最新一筆
    today = datetime.date.today()
    start7 = (today - datetime.timedelta(days=7)).isoformat()
    end7 = today.isoformat()
    taiex_rows = fmind("TaiwanStockPrice", data_id="TAIEX", start_date=start7, end_date=end7, token=token)
    if not taiex_rows:
        print("錯誤：近7天 TAIEX 完全沒有資料，FinMind可能異常")
        return 2
    taiex_rows.sort(key=lambda r: r["date"])
    latest_available = taiex_rows[-1]["date"]
    print(f"LATEST_AVAILABLE（TAIEX最新交易日）: {latest_available}")

    if latest_available <= hd_last:
        print(f"already updated: LATEST_AVAILABLE {latest_available} 未晚於現有 {hd_last}，不動作")
        return 0

    # 2. 用「85檔其中一檔」的實際回傳日期集合當交易日曆（用最大量的那檔比較保險，這裡直接用全部85檔聯集）
    all_extra_codes = sorted(set(TAIEX_TOP10 + TPEX_TOP10) - set(codes))
    all_codes = codes + all_extra_codes
    print(f"另抓（不在85檔內，只需法人資料）: {all_extra_codes}")

    price_data = {}   # code -> {date: {open,max,min,close,spread,Trading_Volume}}
    inst_data = {}    # code -> {date: {inst,instForeign,instTrust,instDealer}}
    trading_calendar = set()

    def fetch_one(code):
        p_rows = fmind("TaiwanStockPrice", data_id=code, start_date=hd_last, end_date=latest_available, token=token)
        i_rows = fmind("TaiwanStockInstitutionalInvestorsBuySell", data_id=code, start_date=hd_last, end_date=latest_available, token=token)
        by_date_p = {r["date"]: r for r in p_rows if r["date"] > hd_last}
        by_date_i = {}
        tmp = {}
        for r in i_rows:
            if r["date"] <= hd_last:
                continue
            tmp.setdefault(r["date"], []).append(r)
        for d, rows in tmp.items():
            by_date_i[d] = inst_from_rows(rows)
        return code, by_date_p, by_date_i

    errors = []
    with ThreadPoolExecutor(max_workers=a.max_workers) as ex:
        futs = {ex.submit(fetch_one, c): c for c in all_codes}
        for fut in as_completed(futs):
            c = futs[fut]
            try:
                code, by_date_p, by_date_i = fut.result()
                price_data[code] = by_date_p
                inst_data[code] = by_date_i
                trading_calendar.update(by_date_p.keys())
            except Exception as e:
                errors.append(f"{c}: {e}")

    if errors:
        print("抓資料失敗的代號：")
        for e in errors:
            print(" -", e)
        return 2

    missing_days = sorted(d for d in trading_calendar if d <= latest_available)
    if not missing_days:
        print("錯誤：85檔股價資料裡找不到任何 > 現有最新日期 的交易日，可能是假日或FinMind延遲")
        return 2
    print("待補交易日：", missing_days)

    # 3. 大盤合計、TPEx OHLC、TAIEX OHLC 逐日抓（這三個request量小，直接逐日抓即可）
    for d in missing_days:
        total_rows = fmind("TaiwanStockTotalInstitutionalInvestors", start_date=d, end_date=d, token=token)
        total_inst = total_inst_billions(total_rows) if total_rows else {"inst": None, "instForeign": None, "instTrust": None, "instDealer": None}
        tpex_rows = fmind("TaiwanStockPrice", data_id="TPEx", start_date=d, end_date=d, token=token)
        tpex_row = next((r for r in tpex_rows if r["date"] == d), None)
        taiex_row = next((r for r in taiex_rows if r["date"] == d), None)
        if taiex_row is None:
            # 補天數涵蓋範圍可能超過一開始抓的近7天窗，個別再抓一次
            extra = fmind("TaiwanStockPrice", data_id="TAIEX", start_date=d, end_date=d, token=token)
            taiex_row = extra[0] if extra else None
        if tpex_row is None or taiex_row is None:
            print(f"錯誤：{d} 缺 TAIEX 或 TPEx OHLC")
            return 2

        # 85檔 batch
        batch = {}
        bad = []
        for c in codes:
            p = price_data.get(c, {}).get(d)
            i = inst_data.get(c, {}).get(d)
            if p is None:
                bad.append(c)
                continue
            batch[c] = {
                "price": {"close": p["close"], "chg": p["spread"], "vol": round_lots(p["Trading_Volume"])},
                "inst": i if i is not None else {"inst": None, "instForeign": None, "instTrust": None, "instDealer": None},
            }
        if bad:
            print(f"錯誤：{d} 這些代號缺股價資料（可能當天停牌，需人工確認)：", bad)
            return 2

        batch_p = os.path.join(a.outputs, f"fetch_batch_{d}.json")
        json.dump(batch, open(batch_p, "w", encoding="utf-8"), ensure_ascii=False)

        # 部分B：taiex_entry / tpex_entry 的 dailyFlow（單位：張，來自個股法人資料，跟總市場億元無關）
        def flow_sum(code_list):
            total = 0
            any_val = False
            for c in code_list:
                i = inst_data.get(c, {}).get(d)
                if i and i.get("inst") is not None:
                    total += i["inst"]
                    any_val = True
            return total if any_val else 0

        taiex_entry = {
            "date": d, "open": taiex_row["open"], "high": taiex_row["max"], "low": taiex_row["min"],
            "close": taiex_row["close"], "dailyFlow": flow_sum(TAIEX_TOP10),
        }
        tpex_entry = {
            "date": d, "open": tpex_row["open"], "high": tpex_row["max"], "low": tpex_row["min"],
            "close": tpex_row["close"], "dailyFlow": flow_sum(TPEX_TOP10),
        }
        # total_inst 已經是 total_inst_billions() 算出來的「億元」單位（(buy-sell)/1e8），直接用，不要再除。
        market = {
            "date": d.replace("-", "/"),
            "taiex": taiex_row["close"], "taiexChg": taiex_row["spread"],
            "instTotal": total_inst["inst"], "foreign": total_inst["instForeign"],
            "invTrust": total_inst["instTrust"], "dealer": total_inst["instDealer"],
        }
        params = {"target_date": d, "market": market, "taiex_entries": [taiex_entry], "tpex_entries": [tpex_entry]}
        json.dump(params, open(os.path.join(a.outputs, f"params_{d}.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=2)
        print(f"寫完 {d}: fetch_batch_{d}.json, params_{d}.json")

    print("MISSING_DATES:" + " ".join(missing_days))
    return 0


if __name__ == "__main__":
    sys.exit(main())
