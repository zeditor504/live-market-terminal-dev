import gspread
from google.oauth2.service_account import Credentials
import yfinance as yf
import pandas as pd
from datetime import datetime
import pytz
import sys
import traceback
import concurrent.futures
import pandas_market_calendars as mcal

# ==========================================
# INSTITUTIONAL MARKET CALENDAR FIREWALL
# ==========================================
def is_market_open_now():
    nyse = mcal.get_calendar('NYSE')
    now_ny = pd.Timestamp.now(tz='America/New_York')
    now_utc = pd.Timestamp.now(tz='UTC')
    
    sched = nyse.schedule(start_date=now_ny.date(), end_date=now_ny.date())
    
    if sched.empty:
        return False, "Market is closed today (Weekend/Holiday)"
        
    market_open = sched.iloc[0]['market_open']
    market_close = sched.iloc[0]['market_close']
    
    if now_utc < market_open:
        return False, "Pre-market"
        
    if now_utc > (market_close + pd.Timedelta(minutes=15)):
        return False, "Post-market (Final settlements already collected)"
        
    return True, "Market Open"

def fetch_ticker_data(ticker):
    try:
        stock = yf.Ticker(ticker)
        
        exact_current_price = round(float(stock.fast_info['last_price']), 2)
        
        recent_hist = stock.history(period="5d", auto_adjust=True)
        recent_hist = recent_hist.dropna(subset=['Close'])
        
        if len(recent_hist) >= 2:
            ny_tz = pytz.timezone('America/New_York')
            ny_date = datetime.now(ny_tz).date()
            last_hist_date = recent_hist.index[-1].date()
            
            if last_hist_date == ny_date:
                previous_price = round(float(recent_hist['Close'].iloc[-2]), 2)
            else:
                previous_price = round(float(recent_hist['Close'].iloc[-1]), 2)
                
            if exact_current_price == previous_price and len(recent_hist) >= 3:
                previous_price = round(float(recent_hist['Close'].iloc[-2]), 2)
        else:
            previous_price = round(float(stock.fast_info['previous_close']), 2)
            
        hist = stock.history(period="1y", auto_adjust=False, actions=True)
        hist = hist.dropna(subset=['Close', 'High'])
        
        if len(hist) < 2:
            return None, f"  [!] Warning: Not enough historical data found for {ticker}."
            
        if 'Stock Splits' in hist.columns:
            splits = hist['Stock Splits'].replace(0.0, 1.0)
            cum_future_splits = splits.iloc[::-1].cumprod().iloc[::-1].shift(-1).fillna(1.0)
            hist['True_High'] = hist['High'] / cum_future_splits
        else:
            hist['True_High'] = hist['High']
            
        high_52w = round(float(hist['True_High'].max()), 2)
        
        high_idx = hist['True_High'].idxmax()
        if getattr(high_idx, 'tzinfo', None) is None:
            high_idx = high_idx.tz_localize('America/New_York')
        high_date = high_idx.tz_convert('America/Chicago').strftime('%m/%d/%Y')
        
        dollar_change = round(exact_current_price - previous_price, 2)
        
        if previous_price > 0:
            percent_change = (dollar_change / previous_price) * 100
        else:
            percent_change = 0.0
            
        cost_of_25 = round(exact_current_price * 25, 2)
        profit_25 = round((high_52w - exact_current_price) * 25, 2)
        
        if exact_current_price > 0:
            upside_raw = (high_52w - exact_current_price) / exact_current_price
        else:
            upside_raw = 0.0
            
        row_data = [
            "", ticker, exact_current_price, percent_change, dollar_change, 
            high_52w, high_date, cost_of_25, profit_25, upside_raw
        ]
        
        return row_data, None
        
    except Exception as e:
        return None, f"  [X] Data processing exception encountered for {ticker}: {e}"

def fetch_intraday_data(ticker):
    try:
        stock = yf.Ticker(ticker)
        exact_price = round(float(stock.fast_info['last_price']), 2)
        exact_volume = stock.fast_info.get('last_volume', 0)
        
        hist = stock.history(period="1d", interval="1m", auto_adjust=False)
        hist = hist.dropna(subset=['Close'])
        
        if hist.empty:
            return [], None

        # ==========================================
        # PERMANENT VECTORIZED TIMEZONE FIX
        # ==========================================
        if getattr(hist.index, 'tz', None) is None:
            hist.index = hist.index.tz_localize('America/New_York')
        hist.index = hist.index.tz_convert('America/Chicago')
        
        rows = []
        for timestamp, row in hist.iterrows():
            dt_str = timestamp.strftime('%Y-%m-%d %H:%M:%S')
            price = round(float(row['Close']), 2)
            volume = int(row['Volume']) if pd.notna(row['Volume']) else 0
            rows.append([dt_str, ticker, price, volume])
            
        if rows:
            rows[-1][2] = exact_price
            if exact_volume:
                rows[-1][3] = int(exact_volume)
            
        return rows, None
        
    except Exception as e:
        return None, f"  [X] Intraday data failure for {ticker}: {e}"

def fetch_macro_data(ticker):
    macro_configs = {
        "5D": {"period": "5d", "interval": "15m"},
        "1M": {"period": "1mo", "interval": "1h"},
        "YTD": {"period": "ytd", "interval": "1d"},
        "ALL": {"period": "max", "interval": "1d"}
    }

    all_rows = []
    try:
        stock = yf.Ticker(ticker)
        exact_price = round(float(stock.fast_info['last_price']), 2)

        for timeframe, config in macro_configs.items():
            hist = stock.history(period=config["period"], interval=config["interval"], auto_adjust=True)
            hist = hist.dropna(subset=['Close'])

            if not hist.empty:
                # Only shift intraday macro data. Shifting 1d data causes midnight rollback bugs.
                if config["interval"] in ["15m", "1h"]:
                    if getattr(hist.index, 'tz', None) is None:
                        hist.index = hist.index.tz_localize('America/New_York')
                    hist.index = hist.index.tz_convert('America/Chicago')

            timeframe_rows = []
            for timestamp, row in hist.iterrows():
                if config["interval"] == "1d":
                    dt_str = timestamp.strftime('%Y-%m-%d') + " 15:00:00"
                else:
                    dt_str = timestamp.strftime('%Y-%m-%d %H:%M:%S')
                    
                price = round(float(row['Close']), 2)
                volume = int(row['Volume']) if pd.notna(row['Volume']) else 0
                timeframe_rows.append([dt_str, ticker, timeframe, price, volume])

            if timeframe_rows:
                timeframe_rows[-1][3] = exact_price
                
            all_rows.extend(timeframe_rows)

        return all_rows, None
        
    except Exception as e:
        return None, f"  [X] Macro data failure for {ticker}: {e}"

def main():
    try:
        print("Checking NYSE Operational Status...")
        is_open, reason = is_market_open_now()
        
        if not is_open:
            print(f"🛑 {reason}. Script aborted to conserve GitHub server minutes.")
            return

        print("Authenticating with Google Cloud...")
        scopes = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
        credentials = Credentials.from_service_account_file("credentials.json", scopes=scopes)
        client = gspread.authorize(credentials)

        print("Connecting to Google Sheet...")
        sheet = client.open("Daily Market Data").sheet1
        tickers = ['TSLA', 'NVDA', 'AAPL', 'MSFT', 'AMZN', 'GOOG', 'META']
        data_rows = []
        
        ct_tz = pytz.timezone('America/Chicago')
        run_date = datetime.now(ct_tz).strftime('%m/%d/%Y')
        
        print(f"Fetching live market data for {len(tickers)} tickers asynchronously...\n")

        with concurrent.futures.ThreadPoolExecutor(max_workers=len(tickers)) as executor:
            future_to_ticker = {executor.submit(fetch_ticker_data, ticker): ticker for ticker in tickers}
            for future in concurrent.futures.as_completed(future_to_ticker):
                ticker = future_to_ticker[future]
                row_data, error_msg = future.result()
                if row_data:
                    data_rows.append(row_data)
                    print(f"  -> {ticker} successfully processed.")
                else:
                    print(error_msg)

        if not data_rows:
            print("\n[!] CRITICAL: No data was successfully fetched. Aborting Google Sheets update.")
            return

        print("\nFormatting daily payload...")
        df = pd.DataFrame(data_rows, columns=[
            'Date of Data Refresh', 'Stock Symbol', 'Closing Price', 'Daily Change (%)', 'Daily Change ($)',
            '52-Week High', 'Date High Reached', 'Cost of 25 shares', 'Expected Profit', 'Upside Potential'
        ])
        df = df.sort_values(by='Upside Potential', ascending=False)

        def format_currency(x): return f"${float(x):,.2f}" if pd.notna(x) else "---"
        def format_pct(x): return f"{float(x) * 100:.2f}%" if pd.notna(x) else "---"
        def format_dollar_change(x):
            if pd.isna(x): return "'$0.00"
            val = float(x)
            if val > 0: return f"'+${val:,.2f}"
            if val < 0: return f"'-${abs(val):,.2f}"
            return "'$0.00"
        def format_arrow_pct(x):
            if pd.isna(x): return "'0.00%"
            val = float(x)
            if val > 0: return f"'↑{val:.2f}%"
            if val < 0: return f"'↓{abs(val):.2f}%"
            return "'0.00%"

        df['Closing Price'] = df['Closing Price'].apply(format_currency)
        df['52-Week High'] = df['52-Week High'].apply(format_currency)
        df['Cost of 25 shares'] = df['Cost of 25 shares'].apply(format_currency)
        df['Expected Profit'] = df['Expected Profit'].apply(format_currency)
        df['Upside Potential'] = df['Upside Potential'].apply(format_pct)
        df['Daily Change ($)'] = df['Daily Change ($)'].apply(format_dollar_change)
        df['Daily Change (%)'] = df['Daily Change (%)'].apply(format_arrow_pct)

        final_batch = [[f"{run_date}", "", "", "", "", "", "", "", "", ""]] + df.values.tolist() + [["", "", "", "", "", "", "", "", "", ""]]
        
        try:
            top_cell = sheet.get('A2')
            existing_date = top_cell[0][0] if (top_cell and len(top_cell[0]) > 0) else None
        except:
            existing_date = None

        if existing_date == run_date:
            sheet.update(values=final_batch, range_name='A2:J10')
            print(f"✅ Success! Data for {run_date} overwritten to prevent duplication.")
        else:
            sheet.insert_rows(final_batch, 2, value_input_option='USER_ENTERED')
            print(f"✅ Success! New Daily History block inserted for {run_date}.")

        print("\nInitiating Intraday Data Pipeline...")
        try:
            intraday_sheet = client.open("Daily Market Data").worksheet("Intraday")
            intraday_rows = [["Datetime", "Symbol", "Price", "Volume"]]
            
            with concurrent.futures.ThreadPoolExecutor(max_workers=len(tickers)) as executor:
                future_to_ticker = {executor.submit(fetch_intraday_data, ticker): ticker for ticker in tickers}
                for future in concurrent.futures.as_completed(future_to_ticker):
                    rows, error_msg = future.result()
                    if rows: intraday_rows.extend(rows)
                        
            if len(intraday_rows) > 1:
                intraday_sheet.clear()
                intraday_sheet.update(values=intraday_rows, range_name='A1')
                print("✅ Success! Intraday Database updated.")
        except Exception as e:
            print(f"❌ Intraday error: {e}")

        print("\nInitiating Macro Historical Data Pipeline...")
        try:
            macro_sheet = client.open("Daily Market Data").worksheet("MacroHistory")
            macro_rows = [["Datetime", "Symbol", "Timeframe", "Price", "Volume"]]
            
            with concurrent.futures.ThreadPoolExecutor(max_workers=len(tickers)) as executor:
                future_to_ticker = {executor.submit(fetch_macro_data, ticker): ticker for ticker in tickers}
                for future in concurrent.futures.as_completed(future_to_ticker):
                    rows, error_msg = future.result()
                    if rows: macro_rows.extend(rows)
                    else: print(error_msg)
                        
            if len(macro_rows) > 1:
                print("Clearing historical macro data...")
                macro_sheet.clear()
                print(f"Pushing {len(macro_rows)} new macro coordinates...")
                macro_sheet.update(values=macro_rows, range_name='A1')
                print("✅ Success! Macro Database updated.")
            else:
                print("[!] Warning: No macro data collected.")
        except Exception as e:
            print(f"❌ Macro error. Details: {e}")

    except Exception as e:
        print("\n" + "="*60 + "\nCRITICAL SCRIPT FAILURE\n" + "="*60)
        print(traceback.format_exc())
        print("="*60)

if __name__ == "__main__":
    main()