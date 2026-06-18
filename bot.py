import gspread
from google.oauth2.service_account import Credentials
import yfinance as yf
import pandas as pd
from datetime import datetime
import sys
import traceback
import concurrent.futures

def fetch_ticker_data(ticker):
    try:
        stock = yf.Ticker(ticker)
        hist = stock.history(period="1y", auto_adjust=False, actions=True)
        hist = hist.dropna(subset=['Close', 'High'])
        
        if len(hist) < 2:
            return None, f"  [!] Warning: Not enough historical data found for {ticker}."
            
        if 'Stock Splits' in hist.columns:
            splits = hist['Stock Splits'].replace(0.0, 1.0)
            cum_future_splits = splits.iloc[::-1].cumprod().iloc[::-1].shift(-1).fillna(1.0)
            hist['True_Close'] = hist['Close'] / cum_future_splits
            hist['True_High'] = hist['High'] / cum_future_splits
        else:
            hist['True_Close'] = hist['Close']
            hist['True_High'] = hist['High']
            
        current_price = round(float(hist['True_Close'].iloc[-1]), 2)
        previous_price = round(float(hist['True_Close'].iloc[-2]), 2)
        
        high_52w = round(float(hist['True_High'].max()), 2)
        high_date = hist['True_High'].idxmax().strftime('%m/%d/%Y')
        
        dollar_change = round(current_price - previous_price, 2)
        
        if previous_price > 0:
            percent_change = (dollar_change / previous_price) * 100
        else:
            percent_change = 0.0
            
        cost_of_25 = round(current_price * 25, 2)
        profit_25 = round((high_52w - current_price) * 25, 2)
        
        if current_price > 0:
            upside_raw = (high_52w - current_price) / current_price
        else:
            upside_raw = 0.0
            
        row_data = [
            "", ticker, current_price, percent_change, dollar_change, 
            high_52w, high_date, cost_of_25, profit_25, upside_raw
        ]
        
        return row_data, None
        
    except Exception as e:
        return None, f"  [X] Data processing exception encountered for {ticker}: {e}"

def fetch_intraday_data(ticker):
    try:
        stock = yf.Ticker(ticker)
        hist = stock.history(period="1d", interval="1m", auto_adjust=False)
        hist = hist.dropna(subset=['Close'])
        
        rows = []
        for timestamp, row in hist.iterrows():
            dt_str = timestamp.strftime('%Y-%m-%d %H:%M:%S')
            price = round(float(row['Close']), 2)
            rows.append([dt_str, ticker, price])
            
        return rows, None
        
    except Exception as e:
        return None, f"  [X] Intraday data failure for {ticker}: {e}"

def fetch_macro_data(ticker):
    """
    Isolated worker to fetch historical arrays for Week, Month, YTD, and Max.
    Aggressively downsamples older data to prevent canvas rendering lag.
    """
    macro_configs = {
        "5D": {"period": "5d", "interval": "15m"},
        "1M": {"period": "1mo", "interval": "1h"},
        "YTD": {"period": "ytd", "interval": "1d"},
        "ALL": {"period": "max", "interval": "1wk"}
    }

    all_rows = []
    try:
        stock = yf.Ticker(ticker)
        for timeframe, config in macro_configs.items():
            # FIXED: auto_adjust=True guarantees perfect historical split/dividend math mapping
            hist = stock.history(period=config["period"], interval=config["interval"], auto_adjust=True)
            hist = hist.dropna(subset=['Close'])

            for timestamp, row in hist.iterrows():
                dt_str = timestamp.strftime('%Y-%m-%d %H:%M:%S')
                price = round(float(row['Close']), 2)
                all_rows.append([dt_str, ticker, timeframe, price])

        return all_rows, None
        
    except Exception as e:
        return None, f"  [X] Macro data failure for {ticker}: {e}"

def main():
    try:
        print("Authenticating with Google Cloud...")
        scopes = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
        credentials = Credentials.from_service_account_file("credentials.json", scopes=scopes)
        client = gspread.authorize(credentials)

        print("Connecting to Google Sheet...")
        sheet = client.open("Daily Market Data").sheet1
        tickers = ['TSLA', 'NVDA', 'AAPL', 'MSFT', 'AMZN', 'GOOG', 'META']
        data_rows = []
        run_date = datetime.now().strftime('%m/%d/%Y')
        
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
        sheet.insert_rows(final_batch, 2, value_input_option='USER_ENTERED')
        print("✅ Success! Daily History updated.")

        # ==========================================
        # INTRADAY 1-MINUTE DATA PIPELINE
        # ==========================================
        print("\nInitiating Intraday Data Pipeline...")
        try:
            intraday_sheet = client.open("Daily Market Data").worksheet("Intraday")
            intraday_rows = [["Datetime", "Symbol", "Price"]]
            
            with concurrent.futures.ThreadPoolExecutor(max_workers=len(tickers)) as executor:
                future_to_ticker = {executor.submit(fetch_intraday_data, ticker): ticker for ticker in tickers}
                for future in concurrent.futures.as_completed(future_to_ticker):
                    rows, error_msg = future.result()
                    if rows: intraday_rows.extend(rows)
                        
            if len(intraday_rows) > 1:
                intraday_sheet.clear()
                intraday_sheet.update(range_name='A1', values=intraday_rows)
                print("✅ Success! Intraday Database updated.")
        except Exception as e:
            print(f"❌ Intraday error: {e}")

        # ==========================================
        # MACRO HISTORY DATA PIPELINE
        # ==========================================
        print("\nInitiating Macro Historical Data Pipeline...")
        try:
            macro_sheet = client.open("Daily Market Data").worksheet("MacroHistory")
            macro_rows = [["Datetime", "Symbol", "Timeframe", "Price"]]
            
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
                macro_sheet.update(range_name='A1', values=macro_rows)
                print("✅ Success! Macro Database updated.")
            else:
                print("[!] Warning: No macro data collected.")
        except Exception as e:
            print(f"❌ Macro error. Did you create the 'MacroHistory' tab? Details: {e}")

    except Exception as e:
        print("\n" + "="*60 + "\nCRITICAL SCRIPT FAILURE\n" + "="*60)
        print(traceback.format_exc())
        print("="*60)

if __name__ == "__main__":
    main()