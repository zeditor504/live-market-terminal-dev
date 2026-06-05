import gspread
from google.oauth2.service_account import Credentials
import yfinance as yf
import pandas as pd
from datetime import datetime
import sys
import traceback
import concurrent.futures

def fetch_ticker_data(ticker):
    """
    Isolated worker function to fetch and process ticker data.
    Designed to run asynchronously for extreme speed optimization.
    """
    try:
        stock = yf.Ticker(ticker)
        
        # EXTREME OPTIMIZATION 1: Disable Yahoo's default dividend adjustments 
        # that corrupt historical highs, but explicitly retain the split action data.
        hist = stock.history(period="1y", auto_adjust=False, actions=True)
        
        # Drop any corrupted rows
        hist = hist.dropna(subset=['Close', 'High'])
        
        if len(hist) < 2:
            return None, f"  [!] Warning: Not enough historical data found for {ticker}."
            
        # EXTREME OPTIMIZATION 2: Custom Stock Split Retroactive Calculator
        # This ensures stock splits (like NVDA 10-for-1) mathematically adjust the past history, 
        # but cash dividends DO NOT shrink the true literal 52-week high.
        if 'Stock Splits' in hist.columns:
            splits = hist['Stock Splits'].replace(0.0, 1.0)
            # Calculate cumulative split ratios strictly for dates preceding the split
            cum_future_splits = splits.iloc[::-1].cumprod().iloc[::-1].shift(-1).fillna(1.0)
            hist['True_Close'] = hist['Close'] / cum_future_splits
            hist['True_High'] = hist['High'] / cum_future_splits
        else:
            hist['True_Close'] = hist['Close']
            hist['True_High'] = hist['High']
            
        # EXTREME OPTIMIZATION 3: Strict 2-Decimal Math Isolation
        # Forces Python to use the exact rounded UI values for downstream calculations
        current_price = round(float(hist['True_Close'].iloc[-1]), 2)
        previous_price = round(float(hist['True_Close'].iloc[-2]), 2)
        
        high_52w = round(float(hist['True_High'].max()), 2)
        high_date = hist['True_High'].idxmax().strftime('%m/%d/%Y')
        
        # Calculations now perfectly match human manual math down to the penny
        dollar_change = round(current_price - previous_price, 2)
        
        if previous_price > 0:
            percent_change = (dollar_change / previous_price) * 100
        else:
            percent_change = 0.0
            
        cost_of_25 = round(current_price * 25, 2)
        
        # Expected Profit calculation mirrors exactly how a human calculates it
        profit_25 = round((high_52w - current_price) * 25, 2)
        
        if current_price > 0:
            upside_raw = (high_52w - current_price) / current_price
        else:
            upside_raw = 0.0
            
        # Construct the unified row payload
        row_data = [
            "", ticker, current_price, percent_change, dollar_change, 
            high_52w, high_date, cost_of_25, profit_25, upside_raw
        ]
        
        return row_data, None
        
    except Exception as e:
        return None, f"  [X] Data processing exception encountered for {ticker}: {e}"

def main():
    try:
        print("Authenticating with Google Cloud...")
        scopes = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]

        # Ensure valid credentials.json is maintained in the active execution directory
        credentials = Credentials.from_service_account_file("credentials.json", scopes=scopes)
        client = gspread.authorize(credentials)

        print("Connecting to Google Sheet...")
        sheet = client.open("Daily Market Data").sheet1

        tickers = ['TSLA', 'NVDA', 'AAPL', 'MSFT', 'AMZN', 'GOOG', 'META']
        data_rows = []

        # Generate execution timestamp for batch payload identification (Upgraded to %Y for 4-digit year)
        run_date = datetime.now().strftime('%m/%d/%Y')
        print(f"Fetching live market data for {len(tickers)} tickers asynchronously...\n")

        # EXTREME OPTIMIZATION 4: Multi-Threaded Asynchronous Extraction
        # Blasts all API requests simultaneously to slash execution time.
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

        # Check if batch completely failed before processing
        if not data_rows:
            print("\n[!] CRITICAL: No data was successfully fetched for any tickers. Aborting Google Sheets update.")
            return

        print("\nFormatting data payload...")
        
        # Construct operational DataFrame utilizing standardized target schema
        df = pd.DataFrame(data_rows, columns=[
            'Date of Data Refresh', 'Stock Symbol', 'Closing Price', 'Daily Change (%)', 'Daily Change ($)',
            '52-Week High', 'Date High Reached', 'Cost of 25 shares', 'Expected Profit', 'Upside Potential'
        ])

        # Sort dataset against primary performance metrics in descending sequence
        df = df.sort_values(by='Upside Potential', ascending=False)

        # Defensive formatting functions (Now utilizing thousands-separators)
        def format_currency(x): 
            return f"${float(x):,.2f}" if pd.notna(x) else "---"
            
        def format_pct(x): 
            return f"{float(x) * 100:.2f}%" if pd.notna(x) else "---"

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

        # Apply formatting safely across columns
        df['Closing Price'] = df['Closing Price'].apply(format_currency)
        df['52-Week High'] = df['52-Week High'].apply(format_currency)
        df['Cost of 25 shares'] = df['Cost of 25 shares'].apply(format_currency)
        df['Expected Profit'] = df['Expected Profit'].apply(format_currency)
        df['Upside Potential'] = df['Upside Potential'].apply(format_pct)

        # Enforce string formatting with apostrophes and specific Unicode arrows
        df['Daily Change ($)'] = df['Daily Change ($)'].apply(format_dollar_change)
        df['Daily Change (%)'] = df['Daily Change (%)'].apply(format_arrow_pct)

        final_values = df.values.tolist()

        # Compile API insertion payload containing operational buffers
        separator_row = [f"{run_date}", "", "", "", "", "", "", "", "", ""]
        blank_row = ["", "", "", "", "", "", "", "", "", ""]

        final_batch = [separator_row] + final_values + [blank_row]

        print("Pushing data to Google Sheets...")

        # Execute insertion operation at chronological vertex (Row 2)
        sheet.insert_rows(final_batch, 2, value_input_option='USER_ENTERED')
        print("✅ Success! Your Google Sheet has been updated.")

    except Exception as e:
        # Master error catcher prevents silent terminal exits
        print("\n" + "="*60)
        print("CRITICAL SCRIPT FAILURE")
        print("="*60)
        print(traceback.format_exc())
        print("="*60)

if __name__ == "__main__":
    main()
