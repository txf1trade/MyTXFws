import asyncio
from datetime import date
from typing import Optional
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
import shioaji as sj

app = FastAPI()

DEFAULT_API_KEY = "CtRaePmj3U4naAinykoq2imMpo4koahkqFzxe6kLUqBn"
DEFAULT_SECRET_KEY = "EKWUBufEST6xDELBqqmkVjVorGxamxPbr7mvkULZZPuR"

def get_api_instance(api_key: Optional[str] = None, secret_key: Optional[str] = None):
    key = api_key if api_key else DEFAULT_API_KEY
    secret = secret_key if secret_key else DEFAULT_SECRET_KEY
    
    inst = sj.Shioaji(simulation=False)
    inst.login(
        api_key=key,
        secret_key=secret,
        subscribe_trade=True
    )
    return inst

api = get_api_instance()

class ConnectionManager:
    def __init__(self):
        self.active_connections: list[WebSocket] = []

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)

    def disconnect(self, websocket: WebSocket):
        if websocket in self.active_connections:
            self.active_connections.remove(websocket)

    async def broadcast(self, message: dict):
        for connection in self.active_connections:
            try:
                await connection.send_json(message)
            except Exception:
                pass

manager = ConnectionManager()
loop = None
latest_quotes = {}
SYMBOLS = ["IX0001", "IX0043", "TXFR1"]
active_option_codes = set()

root_display_map = {
    "TXU": "F1", "TXV": "F2", "TXX": "F3", "TXY": "F4", "TXZ": "F5",
    "TX1": "W1", "TX2": "W2", "TXO": "O", "TX4": "W4", "TX5": "W5",
}

def get_filtered_contracts(custom_api_key=None, custom_secret_key=None):
    try:
        target_api = api
        if custom_api_key and custom_secret_key:
            target_api = get_api_instance(custom_api_key, custom_secret_key)
            
        options = target_api.contracts.list(sj.SecurityType.Option)
        today = date.today()
        
        fridays, wednesdays, monthlies = [], [], []
        
        for opt in options:
            if not opt.code.startswith("TX"):
                continue
            info = target_api.contracts.info(opt)
            if not info or info.delivery_date < today:
                continue
            
            root = info.root
            dm = info.delivery_month
            
            if len(dm) == 6 and dm.isdigit():
                year_str = dm[:4]
                month_str = str(int(dm[4:]))
            else:
                year_str = str(info.delivery_date.year)
                month_str = dm
            
            if root in ["TXU", "TXV", "TXX", "TXY", "TXZ"]:
                label = f"{year_str}/{month_str}{root_display_map[root]}"
                fridays.append((info.delivery_date, opt.code, label))
            elif root in ["TX1", "TX2", "TX4", "TX5"]:
                label = f"{year_str}/{month_str}{root_display_map[root]}"
                wednesdays.append((info.delivery_date, opt.code, label))
            elif root == "TXO":
                label = f"{year_str}/{month_str}O"
                monthlies.append((info.delivery_date, opt.code, label))
        
        fridays.sort(key=lambda x: x[0])
        wednesdays.sort(key=lambda x: x[0])
        monthlies.sort(key=lambda x: x[0])
        
        def pick_unique(items, count):
            seen_dates = set()
            result = []
            for d, code, label in items:
                if d not in seen_dates:
                    seen_dates.add(d)
                    result.append({"delivery_date": d, "code": code, "label": label})
                    if len(result) >= count:
                        break
            return result

        selected_fridays = pick_unique(fridays, 2)
        selected_wednesdays = pick_unique(wednesdays, 2)
        selected_monthlies = pick_unique(monthlies, 1)
        
        combined = selected_fridays + selected_wednesdays + selected_monthlies
        combined.sort(key=lambda x: x["delivery_date"])
        
        return [{
            "code": item["code"],
            "label": item["label"],
            "expiry_date": item["delivery_date"].strftime("%Y-%m-%d")
        } for item in combined]
    except Exception as e:
        print(f"取得選擇權合約失敗: {e}")
        return []

@app.get("/api/contracts")
async def get_contracts(api_key: Optional[str] = None, secret_key: Optional[str] = None):
    return get_filtered_contracts(api_key, secret_key)

@app.get("/api/t_quotes/{code}")
async def get_t_quotes(code: str, current_index: Optional[float] = None, api_key: Optional[str] = None, secret_key: Optional[str] = None):
    global active_option_codes, latest_quotes
    try:
        target_api = api
        if api_key and secret_key:
            target_api = get_api_instance(api_key, secret_key)
            
        target_contract = get_cached_contract(code)
        if not target_contract:
            return {"error": "找不到指定合約"}
        
        info = target_api.contracts.info(target_contract)
        if not info:
            return {"error": "找不到合約詳細資訊"}
            
        root = info.root
        delivery_month = info.delivery_month
        
        center_price = current_index if current_index and current_index > 0 else 22000
        if not current_index or current_index <= 0:
            if "TXFR1" in latest_quotes and latest_quotes["TXFR1"].get("close"):
                center_price = float(latest_quotes["TXFR1"]["close"])
            elif "IX0001" in latest_quotes and latest_quotes["IX0001"].get("close"):
                center_price = float(latest_quotes["IX0001"]["close"])

        strikes_map = {}
        all_options_candidates = []
        
        try:
            options_list = target_api.contracts.options(root)
        except Exception as e:
            options_list = []
            
        for opt in options_list:
            if getattr(opt, 'delivery_month', '') == delivery_month:
                strike_val = getattr(opt, 'strike_price', None)
                if strike_val is not None:
                    try:
                        s_int = int(float(strike_val))
                        all_options_candidates.append((s_int, opt))
                    except:
                        pass
                
        initial_quotes = {}
        if all_options_candidates:
            unique_strikes = sorted(list(set([x[0] for x in all_options_candidates])))
            closest_idx = min(range(len(unique_strikes)), key=lambda i: abs(unique_strikes[i] - center_price))
            start_idx = max(0, closest_idx - 12)
            end_idx = min(len(unique_strikes), closest_idx + 13)
            target_strikes_set = set(unique_strikes[start_idx:end_idx])

            new_contracts_to_sub = []
            contracts_to_snapshot = []
            
            for s_int, opt in all_options_candidates:
                if s_int in target_strikes_set:
                    if s_int not in strikes_map:
                        strikes_map[s_int] = {"strike": s_int, "call_code": None, "put_code": None}
                    
                    o_info = target_api.contracts.info(opt)
                    if o_info and hasattr(o_info, 'option_right'):
                        right = o_info.option_right
                        if right == sj.constant.OptionRight.Call or str(right).endswith('C'):
                            strikes_map[s_int]["call_code"] = opt.code
                            new_contracts_to_sub.append(opt)
                            contracts_to_snapshot.append(opt)
                        elif right == sj.constant.OptionRight.Put or str(right).endswith('P'):
                            strikes_map[s_int]["put_code"] = opt.code
                            new_contracts_to_sub.append(opt)
                            contracts_to_snapshot.append(opt)

            for opt_contract in new_contracts_to_sub:
                if opt_contract.code not in active_option_codes:
                    try:
                        target_api.subscribe(opt_contract, quote_type=sj.QuoteType.BidAsk)
                        active_option_codes.add(opt_contract.code)
                    except Exception as sub_err:
                        print(f"訂閱選擇權 {opt_contract.code} 失敗: {sub_err}")

            if contracts_to_snapshot:
                try:
                    snaps = target_api.snapshots(contracts_to_snapshot)
                    for snap in snaps:
                        if snap:
                            c_val = float(snap.close) if snap.close else 0.0
                            t_vol = int(snap.total_volume) if hasattr(snap, 'total_volume') and snap.total_volume else 0
                            ask_vol = int(getattr(snap, 'ask_side_total_vol', 0) or 0)
                            bid_vol = int(getattr(snap, 'bid_side_total_vol', 0) or 0)
                            strength = ask_vol - bid_vol

                            q_info = {
                                "close": c_val,
                                "total_volume": t_vol,
                                "strength": strength
                            }
                            initial_quotes[snap.code] = q_info
                            # 同步快照至 latest_quotes，支援休市時不顯示 -
                            latest_quotes[snap.code] = {
                                "type": "quote",
                                "code": snap.code,
                                "close": c_val,
                                "total_volume": t_vol,
                                "strength": strength
                            }
                except Exception as snap_err:
                    print(f"取得選擇權快照失敗: {snap_err}")

        sorted_strikes = sorted(list(strikes_map.values()), key=lambda x: x["strike"])
        
        return {
            "root": root,
            "delivery_month": delivery_month,
            "strikes": sorted_strikes,
            "initial_quotes": initial_quotes
        }
    except Exception as e:
        print(f"取得履約價失敗: {e}")
        return {"error": str(e)}

@app.get("/api/usage")
async def get_usage(api_key: Optional[str] = None, secret_key: Optional[str] = None):
    try:
        target_api = api
        if api_key and secret_key:
            target_api = get_api_instance(api_key, secret_key)
        
        usage = target_api.usage()
        used_mb = round(usage.bytes / 1024 / 1024, 2)
        limit_mb = round(usage.limit_bytes / 1024 / 1024, 0)
        remaining_mb = round(usage.remaining_bytes / 1024 / 1024, 2)
        
        return {
            "used_mb": used_mb,
            "limit_mb": limit_mb,
            "remaining_mb": remaining_mb,
            "usage_str": f"已用 {used_mb:.2f} MB / 剩餘 {remaining_mb:.2f} MB"
        }
    except Exception as e:
        return {"used_mb": 0, "limit_mb": 2048, "remaining_mb": 2048, "usage_str": f"讀取失敗: {str(e)}"}

def extract_chg(obj, contract=None):
    p_chg = 0.0
    pct_chg = 0.0
    
    for attr in ['price_chg', 'change', 'diff']:
        if hasattr(obj, attr) and getattr(obj, attr) is not None:
            try:
                val = float(getattr(obj, attr))
                if val != 0.0:
                    p_chg = val
                    break
            except:
                pass
                
    for attr in ['pct_chg', 'pctChange', 'change_rate']:
        if hasattr(obj, attr) and getattr(obj, attr) is not None:
            try:
                val = float(getattr(obj, attr))
                if val != 0.0:
                    pct_chg = val
                    break
            except:
                pass
                
    close_val = float(obj.close) if hasattr(obj, 'close') and obj.close else 0.0
    
    ref = None
    if contract and hasattr(contract, 'reference') and contract.reference:
        try:
            ref = float(contract.reference)
        except:
            pass
    if not ref:
        for ref_attr in ['reference', 'ref', 'yesterday_price', 'last_close']:
            if hasattr(obj, ref_attr) and getattr(obj, ref_attr):
                try:
                    ref = float(getattr(obj, ref_attr))
                    break
                except:
                    pass
                    
    if close_val > 0:
        if ref and ref > 0:
            if p_chg == 0.0:
                p_chg = close_val - ref
            if pct_chg == 0.0:
                pct_chg = (p_chg / ref) * 100.0
        else:
            if p_chg == 0.0 and pct_chg != 0.0:
                p_chg = (close_val * (pct_chg / 100.0)) / (1.0 + pct_chg / 100.0)
            elif pct_chg == 0.0 and p_chg != 0.0:
                ref_calc = close_val - p_chg
                if ref_calc > 0:
                    pct_chg = (p_chg / ref_calc) * 100.0
                    
    return round(p_chg, 2), round(pct_chg, 2)

contract_cache = {}

def get_cached_contract(code):
    if code in contract_cache:
        return contract_cache[code]
    contract = None
    try:
        contract = api.contracts.indexes.get(code)
    except Exception:
        pass
    if not contract:
        try:
            contract = api.contracts.get(code)
        except Exception:
            pass
    if not contract:
        try:
            options = api.contracts.list(sj.SecurityType.Option)
            for opt in options:
                if opt.code == code:
                    contract = opt
                    break
        except Exception:
            pass
    if contract:
        contract_cache[code] = contract
    return contract

def fetch_snapshots():
    global latest_quotes
    for code in SYMBOLS:
        contract = get_cached_contract(code)
        if contract:
            try:
                snapshots = api.snapshots([contract])
                if snapshots:
                    snap = snapshots[0]
                    p_chg, pct_chg = extract_chg(snap, contract)
                    
                    avg_p = 0.0
                    if hasattr(snap, 'average_price') and snap.average_price:
                        avg_p = float(snap.average_price)
                    elif hasattr(snap, 'avg_price') and snap.avg_price:
                        avg_p = float(snap.avg_price)
                        
                    latest_quotes[code] = {
                        "type": "quote",
                        "code": contract.code,
                        "datetime": str(getattr(snap, 'datetime', '休市快照')),
                        "close": float(snap.close) if snap.close else 0,
                        "open": float(snap.open) if snap.open else 0,
                        "high": float(snap.high) if snap.high else 0,
                        "low": float(snap.low) if snap.low else 0,
                        "price_chg": p_chg,
                        "pct_chg": pct_chg,
                        "total_volume": int(snap.total_volume) if hasattr(snap, 'total_volume') and snap.total_volume else 0,
                        "avg_price": avg_p,
                        "bid_side_total_vol": getattr(snap, 'bid_side_total_vol', 0),
                        "ask_side_total_vol": getattr(snap, 'ask_side_total_vol', 0),
                    }
            except Exception as e:
                print(f"取得 [{code}] 快照失敗：{e}")

def handle_quote(quote):
    global latest_quotes
    code = quote.code
    contract = get_cached_contract(code)
    p_chg, pct_chg = extract_chg(quote, contract)
    
    close_val = float(quote.close) if hasattr(quote, 'close') and quote.close else 0.0
    t_vol = int(quote.total_volume) if hasattr(quote, 'total_volume') and quote.total_volume else 0
    ask_v = int(getattr(quote, 'ask_side_total_vol', 0) or 0)
    bid_v = int(getattr(quote, 'bid_side_total_vol', 0) or 0)
    strength = ask_v - bid_v
    
    quote_data = {
        "type": "quote",
        "code": code,
        "datetime": str(getattr(quote, 'datetime', '')),
        "close": close_val,
        "open": float(quote.open) if hasattr(quote, 'open') and quote.open else 0,
        "high": float(quote.high) if hasattr(quote, 'high') and quote.high else 0,
        "low": float(quote.low) if hasattr(quote, 'low') and quote.low else 0,
        "price_chg": p_chg,
        "pct_chg": pct_chg,
        "total_volume": t_vol,
        "strength": strength
    }
    latest_quotes[code] = quote_data
    
    if loop and loop.is_running():
        asyncio.run_coroutine_threadsafe(manager.broadcast(quote_data), loop)

def handle_tick(tick):
    global latest_quotes
    code = tick.code
    contract = get_cached_contract(code)
    p_chg, pct_chg = extract_chg(tick, contract)

    close_val = 0.0
    if hasattr(tick, 'close') and tick.close is not None:
        close_val = float(tick.close)

    t_vol = 0
    if hasattr(tick, 'total_volume') and tick.total_volume is not None:
        t_vol = int(tick.total_volume)
    elif hasattr(tick, 'volume') and tick.volume is not None:
        t_vol = int(tick.volume)

    ask_v = int(getattr(tick, 'ask_side_total_vol', 0) or 0)
    bid_v = int(getattr(tick, 'bid_side_total_vol', 0) or 0)
    strength = ask_v - bid_v

    tick_data = {
        "type": "tick",
        "code": code,
        "datetime": str(getattr(tick, 'datetime', '')),
        "close": close_val,
        "open": float(tick.open) if hasattr(tick, 'open') and tick.open else 0,
        "high": float(tick.high) if hasattr(tick, 'high') and tick.high else 0,
        "low": float(tick.low) if hasattr(tick, 'low') and tick.low else 0,
        "total_volume": t_vol,
        "strength": strength,
        "price_chg": p_chg,
        "pct_chg": pct_chg,
        "avg_price": float(tick.avg_price) if hasattr(tick, 'avg_price') and tick.avg_price else 0,
    }
    latest_quotes[code] = tick_data
    
    if loop and loop.is_running():
        asyncio.run_coroutine_threadsafe(manager.broadcast(tick_data), loop)

@api.on_quote_fop_v1()
def quote_fop_callback(exchange: sj.Exchange, quote):
    handle_quote(quote)

@api.on_quote_stk_v1()
def quote_stk_callback(exchange: sj.Exchange, quote):
    handle_quote(quote)

@api.on_tick_fop_v1()
def tick_fop_callback(exchange: sj.Exchange, tick):
    handle_tick(tick)

@app.on_event("startup")
async def startup_event():
    global loop, SYMBOLS
    loop = asyncio.get_running_loop()
    
    fetch_snapshots()
    
    for code in SYMBOLS:
        contract = get_cached_contract(code)
        if contract:
            try:
                api.subscribe(contract, quote_type=sj.QuoteType.Quote)
            except Exception as e:
                print(f"訂閱 {code} 失敗：{e}")

    try:
        tx_contract = api.contracts.get("TXFR1")
        if tx_contract:
            api.subscribe(tx_contract, quote_type=sj.QuoteType.Tick)
    except Exception as e:
        print(f"訂閱 TXFR1 Tick 失敗: {e}")

@app.get("/")
async def get_dashboard():
    return FileResponse("index.html")

@app.websocket("/ws/quote")
async def websocket_endpoint(websocket: WebSocket):
    await manager.connect(websocket)
    for code, data in latest_quotes.items():
        await websocket.send_json(data)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(websocket)

if __name__ == "__main__":
    import uvicorn
    import os
    port = int(os.environ.get("PORT", 3000))
    uvicorn.run("main:app", host="0.0.0.0", port=port)