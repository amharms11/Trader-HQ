#region Using declarations
using System;
using System.ComponentModel;
using System.ComponentModel.DataAnnotations;
using System.Windows.Media;
using NinjaTrader.Cbi;
using NinjaTrader.Data;
using NinjaTrader.Gui;
using NinjaTrader.NinjaScript;
using NinjaTrader.NinjaScript.DrawingTools;
#endregion

// Gold Opening Range Breakout (ORB) for NinjaTrader 8.
//
// In plain English:
//   1. Every morning, watch gold for the first 15 minutes after 8:20am New York time
//      (when the main COMEX gold session opens). The highest and lowest prices in that
//      window are the "opening range".
//   2. If a 1-minute bar later CLOSES above the range, buy. If one closes below it, sell short.
//   3. Every trade gets a stop-loss (the other side of the range) and a profit target
//      (1.5x the risk) the moment it is entered.
//   4. At most one trade a day. No new trades after 10:30am. Anything still open at noon is closed.
//   5. Days whose range is too small (noise) or too big (too much risk) are skipped.
//
// All times are New York (Eastern) time, whatever time zone your PC uses.
// Built for Micro Gold futures (MGC) on a 1-minute chart. Test it on Sim101 first.

namespace NinjaTrader.NinjaScript.Strategies
{
    public class GoldORB : Strategy
    {
        private const string LongSignal = "ORB Long";
        private const string ShortSignal = "ORB Short";

        private TimeZoneInfo eastern;
        private DateTime tradingDay = DateTime.MinValue;
        private DateTime rangeStartChartTime;
        private double rangeHigh;
        private double rangeLow;
        private bool rangeStarted;
        private bool rangeComplete;
        private bool skipToday;
        private int tradesToday;
        private bool warnedChart;

        protected override void OnStateChange()
        {
            if (State == State.SetDefaults)
            {
                Name = "GoldORB";
                Description = "Gold opening range breakout: trades a 1-minute close outside the first "
                            + "15 minutes after 8:20am ET, with a stop-loss and profit target on every trade.";
                Calculate = Calculate.OnBarClose;
                EntriesPerDirection = 1;
                EntryHandling = EntryHandling.AllEntries;
                IsExitOnSessionCloseStrategy = true;
                ExitOnSessionCloseSeconds = 30;
                IsFillLimitOnTouch = false;
                MaximumBarsLookBack = MaximumBarsLookBack.TwoHundredFiftySix;
                OrderFillResolution = OrderFillResolution.Standard;
                Slippage = 0;
                StartBehavior = StartBehavior.WaitUntilFlat;
                TimeInForce = TimeInForce.Gtc;
                TraceOrders = false;
                RealtimeErrorHandling = RealtimeErrorHandling.StopCancelClose;
                StopTargetHandling = StopTargetHandling.PerEntryExecution;
                BarsRequiredToTrade = 1;
                IsInstantiatedOnEachOptimizationIteration = true;

                Contracts = 1;
                MaxRiskDollars = 200;
                RewardRatio = 1.5;
                StopAtMiddle = false;
                RangeStartHHMM = 820;
                RangeMinutes = 15;
                MinRangeTicks = 20;
                BreakoutBufferTicks = 1;
                LastEntryHHMM = 1030;
                ExitHHMM = 1200;
                MaxTradesPerDay = 1;
            }
            else if (State == State.Configure)
            {
                eastern = TimeZoneInfo.FindSystemTimeZoneById("Eastern Standard Time");
            }
        }

        protected override void OnBarUpdate()
        {
            if (BarsInProgress != 0 || CurrentBar < BarsRequiredToTrade)
                return;

            if (BarsPeriod.BarsPeriodType != BarsPeriodType.Minute || RangeMinutes % BarsPeriod.Value != 0)
            {
                if (!warnedChart)
                    Print("GoldORB: please use a 1-minute chart. The strategy is waiting and will not trade.");
                warnedChart = true;
                return;
            }

            // NinjaTrader time stamps a bar when it closes, in your PC's chosen time zone.
            DateTime barEnd = TimeZoneInfo.ConvertTime(Time[0], NinjaTrader.Core.Globals.GeneralOptions.TimeZoneInfo, eastern);
            int endMin = barEnd.Hour * 60 + barEnd.Minute;
            int startMin = endMin - BarsPeriod.Value;
            int rangeStart = ToMinutes(RangeStartHHMM);
            int rangeEnd = rangeStart + RangeMinutes;

            // New day: forget yesterday's range.
            if (barEnd.Date != tradingDay)
            {
                tradingDay = barEnd.Date;
                rangeHigh = double.MinValue;
                rangeLow = double.MaxValue;
                rangeStarted = false;
                rangeComplete = false;
                skipToday = false;
                tradesToday = 0;
            }

            // 1. Build the opening range from the bars inside the window.
            if (!rangeComplete && startMin >= rangeStart && endMin <= rangeEnd)
            {
                if (!rangeStarted)
                    rangeStartChartTime = Time[0].AddMinutes(-BarsPeriod.Value);
                rangeStarted = true;
                rangeHigh = Math.Max(rangeHigh, High[0]);
                rangeLow = Math.Min(rangeLow, Low[0]);
            }
            if (!rangeComplete && endMin >= rangeEnd)
            {
                rangeComplete = true;
                FinishRange(endMin);
            }

            // 2. Close anything still open at the exit time.
            if (endMin >= ToMinutes(ExitHHMM))
            {
                if (Position.MarketPosition == MarketPosition.Long)
                    ExitLong("Time exit", LongSignal);
                else if (Position.MarketPosition == MarketPosition.Short)
                    ExitShort("Time exit", ShortSignal);
            }

            // 3. Look for a breakout.
            if (!rangeComplete || skipToday
                || Position.MarketPosition != MarketPosition.Flat
                || tradesToday >= MaxTradesPerDay
                || endMin >= ToMinutes(LastEntryHHMM))
                return;

            double buffer = BreakoutBufferTicks * TickSize;
            if (Close[0] > rangeHigh + buffer)
                TryEnter(true);
            else if (Close[0] < rangeLow - buffer)
                TryEnter(false);
        }

        private void FinishRange(int endMin)
        {
            if (!rangeStarted)
            {
                skipToday = true;
                Print(string.Format("{0:yyyy-MM-dd} GoldORB: no data during the opening range (holiday?). No trades today.", tradingDay));
                return;
            }

            double rangeTicks = Math.Round((rangeHigh - rangeLow) / TickSize);
            double stopTicks = StopAtMiddle ? rangeTicks / 2 : rangeTicks;
            double riskDollars = stopTicks * TickSize * Instrument.MasterInstrument.PointValue * Contracts;
            string note;

            if (rangeTicks < MinRangeTicks)
            {
                skipToday = true;
                note = string.Format("range too narrow ({0} ticks, minimum {1}), skipping today", rangeTicks, MinRangeTicks);
            }
            else if (riskDollars > MaxRiskDollars)
            {
                skipToday = true;
                note = string.Format("range too wide (a stop-out would lose about {0:C0}, limit {1:C0}), skipping today", riskDollars, MaxRiskDollars);
            }
            else
            {
                note = string.Format("watching for a close above {0} or below {1}", rangeHigh, rangeLow);
            }
            Print(string.Format("{0:yyyy-MM-dd} GoldORB: opening range {1} - {2}: {3}.", tradingDay, rangeLow, rangeHigh, note));

            // Draw the range on the chart: gold lines if tradable, gray if skipped.
            Brush color = skipToday ? Brushes.Gray : Brushes.Goldenrod;
            DateTime lineEnd = Time[0].AddMinutes(Math.Max(ToMinutes(ExitHHMM) - endMin, 1));
            string day = tradingDay.ToString("yyyyMMdd");
            Draw.Line(this, "ORBHigh" + day, false, rangeStartChartTime, rangeHigh, lineEnd, rangeHigh, color, DashStyleHelper.Solid, 2);
            Draw.Line(this, "ORBLow" + day, false, rangeStartChartTime, rangeLow, lineEnd, rangeLow, color, DashStyleHelper.Solid, 2);
        }

        private void TryEnter(bool isLong)
        {
            double entry = Close[0];
            double stop = StopAtMiddle ? (rangeHigh + rangeLow) / 2 : (isLong ? rangeLow : rangeHigh);
            stop = Instrument.MasterInstrument.RoundToTickSize(stop);
            double riskPoints = Math.Abs(entry - stop);
            if (riskPoints < TickSize)
                return;

            double riskDollars = riskPoints * Instrument.MasterInstrument.PointValue * Contracts;
            if (riskDollars > MaxRiskDollars)
            {
                // Price has run too far from the range; wait in case it comes back closer.
                Print(string.Format("{0} GoldORB: breakout skipped, a stop-out would lose about {1:C0} (limit {2:C0}).", Time[0], riskDollars, MaxRiskDollars));
                return;
            }

            double target = isLong ? entry + riskPoints * RewardRatio : entry - riskPoints * RewardRatio;
            target = Instrument.MasterInstrument.RoundToTickSize(target);
            string signal = isLong ? LongSignal : ShortSignal;

            // The stop and target must be set BEFORE the entry so they go in with it.
            SetStopLoss(signal, CalculationMode.Price, stop, false);
            SetProfitTarget(signal, CalculationMode.Price, target);
            if (isLong)
                EnterLong(Contracts, signal);
            else
                EnterShort(Contracts, signal);
            tradesToday++;

            Print(string.Format("{0} GoldORB: {1} {2} near {3}, stop {4}, target {5} (risk about {6:C0}).",
                Time[0], isLong ? "BUY" : "SELL SHORT", Contracts, entry, stop, target, riskDollars));
        }

        private static int ToMinutes(int hhmm)
        {
            return (hhmm / 100) * 60 + hhmm % 100;
        }

        #region Properties
        [NinjaScriptProperty]
        [Range(1, 10)]
        [Display(Name = "Contracts", Description = "How many contracts per trade. Start with 1 micro (MGC).", Order = 1, GroupName = "1. Size and risk")]
        public int Contracts { get; set; }

        [NinjaScriptProperty]
        [Range(10, 100000)]
        [Display(Name = "Max risk per trade ($)", Description = "Skip any trade that would lose more than this if the stop is hit.", Order = 2, GroupName = "1. Size and risk")]
        public double MaxRiskDollars { get; set; }

        [NinjaScriptProperty]
        [Range(0.5, 10)]
        [Display(Name = "Reward : risk", Description = "Profit target as a multiple of the stop distance. 1.5 = aim to win $150 for every $100 risked.", Order = 3, GroupName = "1. Size and risk")]
        public double RewardRatio { get; set; }

        [NinjaScriptProperty]
        [Display(Name = "Stop at middle of range", Description = "Off: stop at the other side of the range. On: tighter stop at the middle.", Order = 4, GroupName = "1. Size and risk")]
        public bool StopAtMiddle { get; set; }

        [NinjaScriptProperty]
        [Range(0, 2359)]
        [Display(Name = "Range start (HHMM, New York)", Description = "820 = 8:20am ET, the COMEX gold day-session open. 930 = stock market open.", Order = 1, GroupName = "2. Opening range")]
        public int RangeStartHHMM { get; set; }

        [NinjaScriptProperty]
        [Range(1, 120)]
        [Display(Name = "Range length (minutes)", Order = 2, GroupName = "2. Opening range")]
        public int RangeMinutes { get; set; }

        [NinjaScriptProperty]
        [Range(0, 10000)]
        [Display(Name = "Minimum range (ticks)", Description = "Skip days with a smaller range: too little movement to trade. MGC: 1 tick = $0.10 = $1 per contract.", Order = 3, GroupName = "2. Opening range")]
        public int MinRangeTicks { get; set; }

        [NinjaScriptProperty]
        [Range(0, 100)]
        [Display(Name = "Breakout buffer (ticks)", Description = "How far past the range a 1-minute bar must close to count as a breakout.", Order = 4, GroupName = "2. Opening range")]
        public int BreakoutBufferTicks { get; set; }

        [NinjaScriptProperty]
        [Range(0, 2359)]
        [Display(Name = "Last entry (HHMM, New York)", Description = "No new trades at or after this time.", Order = 1, GroupName = "3. Timing")]
        public int LastEntryHHMM { get; set; }

        [NinjaScriptProperty]
        [Range(0, 2359)]
        [Display(Name = "Exit time (HHMM, New York)", Description = "Close any open trade at this time.", Order = 2, GroupName = "3. Timing")]
        public int ExitHHMM { get; set; }

        [NinjaScriptProperty]
        [Range(1, 5)]
        [Display(Name = "Max trades per day", Order = 3, GroupName = "3. Timing")]
        public int MaxTradesPerDay { get; set; }
        #endregion
    }
}
