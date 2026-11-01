# Gold Opening Range Breakout (ORB) for NinjaTrader 8

A beginner's guide to `GoldORB.cs`: what it does, how to install it, and how to test it safely on simulated money.

> Futures use leverage. One Micro Gold contract (MGC) controls 10 ounces of gold, and every $1 move in gold is $10 per contract, gained or lost. You can lose more than you deposit. Test on the simulated account (Sim101) for weeks before considering real money. This is not financial advice.

---

## 1. The strategy in plain English

**The idea:** the first minutes after a market opens set the tone for the day. Lots of orders arrive at the open, and price bounces between a high and a low. When price finally breaks out of that box, it often keeps going in that direction for a while. Often, not always.

**What the strategy does every weekday:**

1. **Draw the box.** From **8:20 to 8:35am New York time** it records the highest and lowest gold price. 8:20am is when the main COMEX gold session opens. That high and low are the **opening range**, and the strategy draws them on your chart as two gold lines.
2. **Check the box is worth trading.**
   - If the box is **tiny** (under 20 ticks = $2 of gold movement), gold is too quiet. **Skip the day.**
   - If the box is **huge**, a losing trade would cost more than your limit ($200 by default). **Skip the day.** The lines turn gray on skipped days.
3. **Wait for a breakout.** It watches 1-minute bars. A bar that **closes** above the box means **buy**, and a bar that closes below means **sell short**. Short selling profits if gold falls. Waiting for a close, not just a touch, filters out quick fake-outs.
4. **Protect the trade immediately.** Every trade is entered together with:
   - a **stop-loss** at the other side of the box, which is the most you can lose on the trade, and
   - a **profit target** at 1.5 times that distance. For example, risk $100 to try to make $150.
5. **Stop for the day.** It takes **one trade a day**, opens no new trades after **10:30am**, and closes anything still open at **noon**.

**Why these rules?** A breakout trader is right maybe 40–55% of the time. The strategy can still make money because winners (1.5×) are bigger than losers (1×). The skip rules and one-trade limit keep a bad day from becoming a disaster.

**An example day:**
- From 8:20 to 8:35, gold trades between 2,000.0 and 2,004.0. The box is 4.0 points = 40 ticks = $40 risk per micro contract.
- At 8:52, a 1-minute bar closes at 2,004.3, above the box. The strategy **buys 1 MGC** at about 2,004.3.
- The stop is at 2,000.0, a risk of 4.3 points = $43. The target is 2,004.3 + 1.5 × 4.3 = 2,010.75, rounded to 2,010.8, a gain of about $65.
- Then gold either hits the target (+$65), hits the stop (−$43), or is still between them at noon, when it's closed at whatever the price is.

(Prices are made up to keep the arithmetic simple.)

---

## 2. What you need

- **A Windows PC.** NinjaTrader 8 is Windows-only. On a Mac, you can run Windows inside Parallels Desktop.
- **NinjaTrader 8** (free to download), and a **free NinjaTrader account** for simulated trading and market data.
- The file **`GoldORB.cs`** from this folder.

---

## 3. Install NinjaTrader 8

1. Go to **ninjatrader.com**, click **Get Started / Free Download**, and create a free account.
2. Download and install **NinjaTrader 8 desktop**.
3. Open it and log in with your NinjaTrader account. The main window is called the **Control Center**.
4. Connect to data: **Connections** menu → choose the NinjaTrader connection offered (the simulation/demo data connection). The bottom-left corner turns green when connected.

---

## 4. Install the strategy

1. Download `GoldORB.cs`. On the GitHub page for the file, click **Download raw file**.
2. Copy it into this folder (paste the path into File Explorer's address bar):
   ```
   %USERPROFILE%\Documents\NinjaTrader 8\bin\Custom\Strategies
   ```
3. In NinjaTrader's Control Center: **New → NinjaScript Editor**.
4. In the editor's list on the right, open **Strategies**, double-click **GoldORB**, then press **F5** to compile.
5. Look at the bottom of the editor:
   - **No errors**: you'll hear a short sound and see nothing in the error list. Installed.
   - **Errors listed**: copy the error lines and send them to me. NinjaTrader's compiler is the final check; I couldn't run it while writing this.

---

## 5. Test 1: Backtest on past data (Strategy Analyzer)

A backtest replays the rules over months of past prices to see how they would have done.

1. Control Center: **New → Strategy Analyzer**.
2. In the left panel, set:
   - **Strategy:** GoldORB
   - **Instrument:** click it and type `MGC`. Pick the front contract, for example `MGC 12-26` (gold contracts expire every two months).
   - **Data series → Type:** Minute, **Value:** 1
   - **Time frame:** start date about **6–12 months ago**, end date today
   - **Trading hours:** leave as the instrument default
   - **Order fill resolution:** **High**, Type Tick, Value 1. This makes stop and target fills more realistic.
   - **Slippage:** **1**, so the test assumes slightly worse prices than perfect.
   - **Commission:** turn it **on**. Real trades cost a fee each way.
   - Leave the GoldORB settings at their defaults for the first run.
3. Click **Run**. NinjaTrader downloads past data the first time, which can take a few minutes.
4. Read the results:

| Tab / number | What it means | What you'd like to see |
|---|---|---|
| **Total # of trades** | How many trades | **At least 50**. Fewer means the result is mostly luck |
| **Total net profit** | Money made after commission | Positive |
| **Profit factor** | Money from winners ÷ money lost on losers | Above **1.2**. 1.0 means break-even |
| **Percent profitable** | Share of winning trades | 40–55% is normal for this strategy |
| **Max. drawdown** | Worst drop from a high point | An amount you could stomach losing in real life |
| **Trades** tab | Every trade with date, entry, exit and profit | Spot-check a few |
| **Chart** tab | Trades drawn on the chart, with the gold range lines | Check the rules behave as described |

**Be skeptical of great results.** A backtest is the best case. If you change settings again and again until the numbers look amazing, you've fitted the past, and it usually won't repeat.

---

## 6. Test 2: Market Replay (practice a past day like it's live)

This lets you watch the strategy trade a past morning, sped up.

1. Control Center: **Tools → Historical Data** → **Load** tab → expand **Market Replay** → pick **MGC**, choose a few recent weekdays, and click **Download**.
2. **Connections → Playback Connection**. A playback control bar appears.
3. Open a chart: **New → Chart** → `MGC 12-26` (or the current contract), **1 Minute**.
4. In the playback bar, set the date to one you downloaded, starting about **8:00am New York time**.
5. Right-click the chart → **Strategies** → add **GoldORB** → **Account: Playback101** → check **Enabled** → **OK**.
6. Press **Play** and increase the speed. Watch the range lines appear at 8:35 and see if and how it trades.
7. **New → NinjaScript Output** shows the strategy's notes, for example "opening range ... watching for a close above ..." or "BUY 1 near ...".

---

## 7. Test 3: Live simulation (real-time prices, fake money)

Run this for **several weeks** before even thinking about real money.

1. Disconnect playback and connect your normal NinjaTrader connection.
2. Open a **1-minute MGC chart** for the current contract.
3. Right-click the chart → **Strategies** → **GoldORB** → **Account: Sim101** → **Enabled** → **OK**.
4. Start it **before 8:20am New York time** on a weekday. Leave NinjaTrader running until noon.
5. Track results in Control Center → **Accounts** tab (Sim101) and the **Executions** tab.

**Before real money, all of these should be true:**
- At least 50 backtest trades, profitable after commission
- Several weeks of Sim101 results that look like the backtest. If live sim is much worse, find out why first.
- You can explain every trade it took using the rules in section 1
- The worst losing streak in the backtest wouldn't hurt you financially or emotionally

---

## 8. Settings you can change

Right-click the chart → **Strategies** → select GoldORB → change the values → **OK**.

| Setting | Default | Plain English |
|---|---|---|
| Contracts | 1 | Contracts per trade. Stay at 1 micro (MGC) while learning |
| Max risk per trade ($) | 200 | Skip any trade that would lose more than this at its stop |
| Reward : risk | 1.5 | Target distance as a multiple of stop distance |
| Stop at middle of range | Off | On = tighter stop (smaller losses, but stopped out more often) |
| Range start (HHMM, New York) | 820 | 8:20am ET. Try 930 to use the stock-market open instead |
| Range length (minutes) | 15 | How long the box is measured |
| Minimum range (ticks) | 20 | Skip quiet days. MGC: 1 tick = $0.10 move = $1 per contract |
| Breakout buffer (ticks) | 1 | How far past the box a bar must close to count |
| Last entry (HHMM) | 1030 | No new trades from 10:30am |
| Exit time (HHMM) | 1200 | Close any open trade at noon |
| Max trades per day | 1 | Number of trades per day |

**Change one setting at a time** and re-run the backtest. Don't choose the setting with the best past result. Prefer settings that do reasonably well across many values.

---

## 9. Words you'll see

- **Futures contract:** an agreement to buy or sell something (here, gold) at a future date. Traders close their trades before then and just keep the profit or loss.
- **MGC / GC:** Micro Gold (10 oz) and full-size Gold (100 oz). Start with MGC. GC is 10 times the risk.
- **Tick:** the smallest price step. For gold it's $0.10, which is worth $1 on MGC and $10 on GC.
- **Long / short:** long profits when the price rises; short profits when it falls.
- **Stop-loss / profit target:** orders that close the trade automatically at a set loss or a set gain.
- **Sim101 / Playback101:** NinjaTrader's practice accounts with fake money.
- **Drawdown:** how far the account fell from its highest point before recovering.
