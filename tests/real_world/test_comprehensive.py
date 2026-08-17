# Copyright 2026 Joshua Hudson
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""
Comprehensive real-world PL transpiler tests.
Each test transpiles a PL script, compiles the Python output, and executes
it against synthetic bar data to verify runtime correctness.
"""
import sys, os, pytest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

def fresh_transpile(pl_source):
    """Transpile with fresh module imports to avoid stale state."""
    for mod in list(sys.modules.keys()):
        if mod.startswith('pl_transpiler'):
            del sys.modules[mod]
    from pl_transpiler import transpile
    return transpile(pl_source)

def make_bars(n=100):
    """Generate n synthetic OHLCV bars."""
    import random
    random.seed(42)
    o, h, l, c, v = [], [], [], [], []
    price = 100.0
    for i in range(n):
        open_ = price + random.uniform(-1, 1)
        high_ = open_ + random.uniform(0, 3)
        low_ = open_ - random.uniform(0, 3)
        close_ = low_ + random.uniform(0, high_ - low_)
        vol_ = random.randint(1000, 50000)
        o.append(open_); h.append(high_); l.append(low_); c.append(close_); v.append(vol_)
        price = close_
    return o, h, l, c, v

def exec_strategy(py_code, extra_kwargs=None):
    """Execute transpiled strategy code against synthetic bars."""
    o, h, l, c, v = make_bars(100)
    dates = list(range(1250101, 1250101 + 100))
    times = [930 + i for i in range(100)]

    # Build exec namespace with runtime functions
    ns = {}
    exec("from pl_transpiler.runtime.pl_runtime import *", ns)
    exec("from pl_transpiler.builtins import *", ns)
    exec("import math", ns)
    exec(py_code, ns)

    kwargs = {
        'market_position': 0,
        'time_s': 93000,
        'entry_price': 0,
        'exit_price': 0,
        'position_profit': 0,
        'bars_since_entry': 0,
        'bars_since_exit': 0,
        'entry_date': 0,
        'entry_time': 0,
        'exit_date': 0,
        'exit_time': 0,
        'contract_profit': 0,
        'max_position_profit': 0,
        'max_position_loss': 0,
        'open_position_profit': 0,
        'gross_profit': 0,
        'gross_loss': 0,
        'net_profit': 0,
        'max_contracts': 0,
        'current_contracts': 0,
        'max_entries': 1,
        'entry_name': '',
        'exit_name': '',
        'alert_enabled': True,
        'check_alert': True,
        '_first_bar': True,
        'current_date': 1250301,
        'current_time': 1430,
        # MC extensions
        'lastbaronchartex': False,
        'base_data_number': 1,
        'current_data_number': 1,
        # Period data
        'opend': 100.0, 'highd': 105.0, 'lowd': 95.0, 'closed': 102.0, 'volumed': 10000,
        'openw': 99.0, 'highw': 108.0, 'loww': 92.0, 'closew': 104.0,
        'openm': 98.0, 'highm': 112.0, 'lowm': 88.0, 'closem': 106.0,
        # Order extras
        'avgentryprice': 0, 'currententries': 0, 'currentshares': 0,
        'totaltrades': 0, 'numwintrades': 0, 'numlostrades': 0,
        'percentprofit': 0, 'largestwintrade': 0, 'largestlostrade': 0,
    }
    if extra_kwargs:
        kwargs.update(extra_kwargs)

    result = ns['strategy'](o, h, l, c, v, dates, times, **kwargs)
    return result


# =====================================================================
# SCRIPT 1: Full Technical Analysis Suite
# Tests: Average, XAverage, WeightedAverage, RSI, MACD*, Momentum,
#   ROC, CCI, ATR, StandardDev, Highest, Lowest, HighestBar, LowestBar,
#   Summation, Variance, Correlation, LinearReg*, Stochastics,
#   TrueRange, CrossesAbove/Below, math functions, data series
# =====================================================================
SCRIPT_1_TECHNICAL = """
Inputs: FastLen(9), SlowLen(18), RSILen(14), BBLen(20), BBMult(2.0);
Variables: FastMA(0), SlowMA(0), ExpMA(0), WtMA(0), RSIVal(0);
Variables: MACDVal(0), MACDSig(0), MACDHist(0), MomVal(0), ROCVal(0);
Variables: CCIVal(0), ATRVal(0), StdDevVal(0), TRVal(0);
Variables: HiVal(0), LoVal(0), HiBar(0), LoBar(0);
Variables: SumVal(0), VarVal(0), CorrVal(0);
Variables: LinRegVal(0), LinRegSlp(0);
Variables: FastKVal(0), FastDVal(0), SlowKVal(0), SlowDVal(0);
Variables: MidBand(0), UpperBand(0), LowerBand(0);
Variables: AbsVal(0), SqrtVal(0), SqVal(0), LogVal(0), ExpVal(0);
Variables: CeilVal(0), FloorVal(0), RndVal(0), IntVal(0), FracVal(0);
Variables: SignVal(0), PowVal(0), MaxV(0), MinV(0);

{ Moving averages }
FastMA = Average(Close, FastLen);
SlowMA = Average(Close, SlowLen);
ExpMA = XAverage(Close, FastLen);
WtMA = WeightedAverage(Close, FastLen);

{ Oscillators }
RSIVal = RSI(Close, RSILen);
MACDVal = MACDValue(FastLen, SlowLen, Close);
MACDSig = MACDSignal(FastLen, SlowLen, 9, Close);
MACDHist = MACDDiff(FastLen, SlowLen, 9, Close);
MomVal = Momentum(Close, 12);
ROCVal = ROC(Close, 12);
CCIVal = CCI(14);
ATRVal = AvgTrueRange(14);

{ Statistics }
StdDevVal = StandardDev(Close, BBLen);
SumVal = Summation(Close, 14);
VarVal = Variance(Close, 14);
CorrVal = Correlation(Close, Volume, 14);
LinRegVal = LinearRegValue(Close, 20);
LinRegSlp = LinearRegSlope(Close, 20);

{ Highest/Lowest }
HiVal = Highest(High, 20);
LoVal = Lowest(Low, 20);
HiBar = HighestBar(High, 20);
LoBar = LowestBar(Low, 20);

{ Stochastics }
FastKVal = FastK(14);
FastDVal = FastD(14, 3);
SlowKVal = SlowK(14, 3);
SlowDVal = SlowD(14, 3, 3);

{ Bollinger Bands }
MidBand = Average(Close, BBLen);
UpperBand = MidBand + BBMult * StdDevVal;
LowerBand = MidBand - BBMult * StdDevVal;

{ Math functions }
AbsVal = AbsValue(-5.5);
SqrtVal = Sqrt(16);
SqVal = Square(4);
LogVal = Log(2.718);
ExpVal = ExpValue(1);
CeilVal = Ceiling(3.2);
FloorVal = Floor(3.8);
RndVal = Round(3.5);
IntVal = IntPortion(3.7);
FracVal = FracPortion(3.7);
SignVal = Sign(-5);
PowVal = Power(2, 8);
MaxV = MaxList(1, 5, 3);
MinV = MinList(1, 5, 3);

{ Crossover signals }
If FastMA CrossesAbove SlowMA Then
    Buy ("TA_LE") Next Bar At Market;
If FastMA CrossesBelow SlowMA Then
    Sell ("TA_LX") Next Bar At Market;

{ Plots }
Plot1(FastMA, "FastMA");
Plot2(SlowMA, "SlowMA");
Plot3(RSIVal, "RSI");
Plot4(MACDVal, "MACD");
"""

def test_script1_technical_analysis():
    py = fresh_transpile(SCRIPT_1_TECHNICAL)
    compile(py, '<test1>', 'exec')
    result = exec_strategy(py)
    assert 'plots' in result
    assert 'orders' in result
    assert 'plot1' in result['plots']
    assert 'plot3' in result['plots']


# =====================================================================
# SCRIPT 2: Order Management & Risk
# Tests: Buy, Sell, SellShort, BuyToCover, SetStopLoss, SetProfitTarget,
#   SetDollarTrailing, SetPercentTrailing, SetExitOnClose, SetBreakEven,
#   MarketPosition, EntryPrice, PositionProfit, BarsSinceEntry/Exit,
#   EntryDate/Time, ExitDate/Time, GrossProfit, NetProfit, etc.
# =====================================================================
SCRIPT_2_ORDERS = """
Inputs: StopAmt(500), TargetAmt(1000), TrailAmt(300);
Variables: FastMA(0), SlowMA(0), RSIVal(0);
Variables: EP(0), PP(0), BSE(0), BSX(0), GP(0), GL(0), NP(0);
Variables: MP(0), CC(0), MC(0), ME(0);

FastMA = Average(Close, 9);
SlowMA = Average(Close, 18);
RSIVal = RSI(Close, 14);
MP = MarketPosition;
EP = EntryPrice;
PP = PositionProfit;
BSE = BarsSinceEntry;
BSX = BarsSinceExit;
GP = GrossProfit;
GL = GrossLoss;
NP = NetProfit;
CC = CurrentContracts;
MC = MaxContracts;
ME = MaxEntries;

{ Entry logic }
If FastMA CrossesAbove SlowMA And RSIVal > 50 Then Begin
    Buy ("LONG") Next Bar At Market;
    SetStopLoss(StopAmt);
    SetProfitTarget(TargetAmt);
    SetDollarTrailing(TrailAmt);
End;

If FastMA CrossesBelow SlowMA And RSIVal < 50 Then Begin
    SellShort ("SHORT") Next Bar At Market;
    SetStopLoss(StopAmt);
    SetProfitTarget(TargetAmt);
    SetPercentTrailing(2.0);
End;

{ Exit logic }
If MarketPosition = 1 And BSE > 20 Then
    Sell ("TIME_LX") Next Bar At Market;
If MarketPosition = -1 And BSE > 20 Then
    BuyToCover ("TIME_SX") Next Bar At Market;

SetExitOnClose;
SetBreakEven(200);

Plot1(FastMA, "Fast");
Plot2(SlowMA, "Slow");
"""

def test_script2_orders():
    py = fresh_transpile(SCRIPT_2_ORDERS)
    compile(py, '<test2>', 'exec')
    result = exec_strategy(py)
    assert 'risk' in result
    assert result['risk'].get('exit_on_close') == True
    assert result['risk'].get('breakeven') == 200


# =====================================================================
# SCRIPT 3: Control Flow, Loops, Arrays, Strings, Once
# Tests: If/Then/Else, Begin/End, For/To/DownTo, While, Switch/Case,
#   Once, Break, Continue, Arrays, String functions, Print, Alert,
#   Commentary, Abort logic
# =====================================================================
SCRIPT_3_CONTROLFLOW = """
Inputs: Periods(10), Threshold(2.0);
Variables: i(0), j(0), SumClose(0), AvgCalc(0), Found(False);
Variables: MsgStr(""), TempStr("");
Variables: MaxVal(0), MinVal(0);
Arrays: PriceArr[50](0), FlagArr[10](False);

{ Once block }
Once Begin
    MaxVal = High;
    MinVal = Low;
End;

{ Track running high/low }
If High > MaxVal Then MaxVal = High;
If Low < MinVal Then MinVal = Low;

{ For loop with downto - shift array }
For i = Periods DownTo 2 Begin
    PriceArr[i] = PriceArr[i - 1];
End;
PriceArr[1] = Close;

{ For loop - calculate sum }
SumClose = 0;
For i = 1 To Periods Begin
    SumClose = SumClose + PriceArr[i];
End;
AvgCalc = SumClose / Periods;

{ While loop - search }
i = 1;
Found = False;
While i <= Periods And Not Found Begin
    If PriceArr[i] > AvgCalc + Threshold Then Begin
        Found = True;
        j = i;
    End;
    i = i + 1;
End;

{ Switch/case }
Switch (DayOfWeek) Begin
    Case 1:
        MsgStr = "Monday";
    Case 2:
        MsgStr = "Tuesday";
    Case 3:
        MsgStr = "Wednesday";
    Default:
        MsgStr = "Other";
End;

{ String operations }
TempStr = NumToStr(Close, 2);
MsgStr = "Price: " + TempStr;
If StrLen(MsgStr) > 5 Then
    MsgStr = LeftStr(MsgStr, 10);

{ Nested if/else }
If Close > AvgCalc Then Begin
    If Volume > Average(Volume, 20) Then Begin
        Buy ("BREAKOUT") Next Bar At Market;
        Print("Breakout signal at " + NumToStr(Close, 2));
    End Else Begin
        Plot1(1, "Signal");
    End;
End Else Begin
    If Close < AvgCalc - Threshold Then Begin
        SellShort ("BREAKDOWN") Next Bar At Market;
    End;
End;

{ Alert }
If Found Then
    Alert("Price exceeded threshold");

{ Commentary }
Commentary("Avg: " + NumToStr(AvgCalc, 2));

Plot2(AvgCalc, "AvgCalc");
Plot3(MaxVal, "MaxVal");
Plot4(MinVal, "MinVal");
"""

def test_script3_controlflow():
    py = fresh_transpile(SCRIPT_3_CONTROLFLOW)
    compile(py, '<test3>', 'exec')
    result = exec_strategy(py)
    assert 'plots' in result
    assert 'plot3' in result['plots']  # MaxVal
    assert 'plot4' in result['plots']  # MinVal


# =====================================================================
# SCRIPT 4: Drawing, Colors, Styles, Plots, Output
# Tests: TL_New, TL_Set*, TL_Get*, Text_New, Text_Set*, Arw_New,
#   SetPlotColor, SetPlotWidth, SetPlotStyle, SetPlotBGColor, NoPlot,
#   Color constants, Style constants, PlotPB, Plot1-8, PrintLog,
#   AlertEnabled, CheckAlert
# =====================================================================
SCRIPT_4_DRAWING = """
Variables: TLId(0), TxtId(0), ArwId(0);
Variables: FastMA(0), SlowMA(0), Value1(0);

FastMA = Average(Close, 9);
SlowMA = Average(Close, 18);
Value1 = Close - Open;

{ Trendline }
TLId = TL_New(Date[5], Time[5], Low[5], Date, Time, High);
TL_SetColor(TLId, Red);
TL_SetStyle(TLId, Tool_Solid);
TL_SetWidth(TLId, 2);
TL_SetExtRight(TLId, True);
TL_SetExtLeft(TLId, False);

{ Text }
TxtId = Text_New(Date, Time, High, "Label");
Text_SetString(TxtId, "Signal");
Text_SetColor(TxtId, Blue);
Text_SetFontName(TxtId, "Arial");
Text_SetFontSize(TxtId, 12);

{ Arrow }
ArwId = Arw_New(Date, Time, Low, True);

{ Plots with formatting }
Plot1(FastMA, "Fast");
Plot2(SlowMA, "Slow");
Plot3(Value1, "Range");
Plot4(Close, "Close");
Plot5(High, "High");
Plot6(Low, "Low");
Plot7(Volume, "Vol");
Plot8(Open, "Open");

SetPlotColor(1, Green);
SetPlotColor(2, Red);
SetPlotWidth(1, 2);
SetPlotStyle(1, Tool_Solid);
SetPlotBGColor(1, Yellow);

{ Conditional plot suppression }
If Value1 < 0 Then
    NoPlot(3);

{ PlotPB }
PlotPB(Open, High, Low, Close);

{ Alert/check }
If AlertEnabled Then Begin
    If CheckAlert Then
        Alert("Signal");
End;

{ Print }
Print("FastMA=", NumToStr(FastMA, 2));
PrintLog("Logged: " + NumToStr(Close, 4));
"""

def test_script4_drawing():
    py = fresh_transpile(SCRIPT_4_DRAWING)
    compile(py, '<test4>', 'exec')
    result = exec_strategy(py)
    assert 'plot1' in result['plots']
    assert 'plot5' in result['plots']
    assert 'plot8' in result['plots']


# =====================================================================
# SCRIPT 5: Data Series, MC Extensions, Time Filters, Period Data
# Tests: All data series (OHLCV, Date, Time, BarNumber, etc.),
#   Time_S, BarType_Ex, LastBarOnChartEx, OpenD/HighD/LowD/CloseD,
#   OpenW/HighW/LowW/CloseW, OpenM/HighM/LowM/CloseM,
#   DayOfWeek, DayOfMonth, Month, Year, BarStatus, SessionNumber,
#   CurrentDate, CurrentTime, date/time functions
# =====================================================================
SCRIPT_5_DATA = """
Inputs: StartTime(93000), EndTime(160000);
Variables: InSession(False), Value1(0), Value2(0), Value3(0);
Variables: DOpen(0), DHigh(0), DLow(0), DClose(0), DVol(0);
Variables: WOpen(0), WHigh(0), WLow(0), WClose(0);
Variables: MOpen(0), MHigh(0), MLow(0), MClose(0);
Variables: BarNum(0), CurBar(0), BType(0), BInt(0), BStat(0);
Variables: DOW(0), DOM(0), Mon(0), Yr(0), SessNum(0);
Variables: CurDate(0), CurTime(0), DateStr(""), TimeStr("");
Variables: TS(0);

{ Core data series }
Value1 = Open;
Value1 = High;
Value1 = Low;
Value1 = Close;
Value1 = Volume;
Value1 = Date;
Value1 = Time;
Value1 = Ticks;
Value1 = UpTicks;
Value1 = DownTicks;
Value1 = OpenInt;

{ Bar info }
BarNum = BarNumber;
CurBar = CurrentBar;
BType = BarType;
BInt = BarInterval;
BStat = BarStatus;
SessNum = SessionNumber;

{ Calendar }
DOW = DayOfWeek;
DOM = DayOfMonth;
Mon = Month;
Yr = Year;

{ Time filter with Time_S }
TS = Time_S;
InSession = Time_S >= StartTime And Time_S < EndTime;

{ Period data }
DOpen = OpenD(0);
DHigh = HighD(0);
DLow = LowD(0);
DClose = CloseD(0);
DVol = VolumeD(0);
WOpen = OpenW(0);
WHigh = HighW(0);
WLow = LowW(0);
WClose = CloseW(0);
MOpen = OpenM(0);
MHigh = HighM(0);
MLow = LowM(0);
MClose = CloseM(0);

{ Date/time functions }
CurDate = CurrentDate;
CurTime = CurrentTime;
DateStr = DateToString(Date);
TimeStr = TimeToString(Time);

{ MC extensions }
If LastBarOnChart Then
    Value2 = 1;

{ Conditional on session }
If InSession Then Begin
    Value3 = Average(Close, 20);
    Plot1(Value3, "SessionMA");
End Else Begin
    Plot1(0, "SessionMA");
End;

Plot2(DOpen, "DOpen");
Plot3(DHigh, "DHigh");
"""

def test_script5_data_series():
    py = fresh_transpile(SCRIPT_5_DATA)
    compile(py, '<test5>', 'exec')
    result = exec_strategy(py)
    assert 'plots' in result


# =====================================================================
# SCRIPT 6: Advanced Functions, Array Ops, Trig, Sessions, Symbols
# Tests: ArcTangent, Cosine, Sine, Tangent, SquareRoot, Random,
#   Array_SetMaxIndex, Array_GetMaxIndex, Array_Sum, Array_Sort,
#   Array_Highest, Array_Lowest, SumList, AvgList, NthMaxList, NthMinList,
#   File/IO stubs, Symbol info, Session times, Portfolio,
#   PMM functions, Q_ quote functions, Mod operator
# =====================================================================
SCRIPT_6_ADVANCED = """
Variables: Value1(0), Value2(0), Value3(0), Value4(0);
Variables: S1(""), S2("");
Variables: i(0);
Arrays: DataArr[100](0);

{ Trig functions }
Value1 = ArcTangent(1);
Value2 = Cosine(0);
Value3 = Sine(0);
Value4 = Tangent(0);
Value1 = SquareRoot(25);

{ Mod operator }
Value1 = 10 Mod 3;

{ Array operations }
For i = 1 To 50 Begin
    DataArr[i] = Close[i];
End;

Array_SetMaxIndex(DataArr, 100);
Value1 = Array_GetMaxIndex(DataArr);
Value2 = Array_Sum(DataArr, 1, 50);

{ MinList, MaxList, with nested calls }
Value1 = MaxList(High, High[1], High[2]);
Value2 = MinList(Low, Low[1], Low[2]);

{ String ops }
S1 = "Hello World";
Value1 = StrLen(S1);
S2 = UpperStr(S1);
S2 = LowerStr(S1);
S2 = LeftStr(S1, 5);
S2 = RightStr(S1, 5);
S2 = MidStr(S1, 6, 5);
Value1 = StrFind(S1, "World");

{ NumToStr / StrToNum }
S1 = NumToStr(3.14159, 4);
Value1 = StrToNum("42.5");

{ Symbol info stubs }
Value1 = BigPointValue(1);
Value1 = MinMove(1);
Value1 = PriceScale(1);
S1 = SymbolName(1);
S2 = GetSymbolName(1);

{ Session stubs }
Value1 = Sess1StartTime(1);
Value1 = Sess1EndTime(1);

{ Complex expression }
If Close > Average(Close, 20) And Volume > Average(Volume, 20) Then Begin
    If RSI(Close, 14) > 50 Or MACDValue(12, 26, Close) > 0 Then Begin
        Value1 = ATR(14) * 2;
        Buy ("ADV_LE") Next Bar At Market;
        SetStopLoss(Value1);
    End;
End;

Plot1(Close, "Close");
"""

def test_script6_advanced():
    py = fresh_transpile(SCRIPT_6_ADVANCED)
    compile(py, '<test6>', 'exec')
    result = exec_strategy(py)
    assert 'orders' in result


# =====================================================================
# SCRIPT 7: Edge Cases & Stress Test
# Tests: Deeply nested blocks, multiple consecutive orders,
#   assignments to array elements, bar references with expressions,
#   string concatenation chains, boolean logic chains, empty blocks,
#   semicolons everywhere, numeric edge cases
# =====================================================================
SCRIPT_7_EDGE = """
Inputs: Len1(5), Len2(10), Len3(20);
Variables: A(0), B(0), C(0), D(0), Cond1(False), Cond2(False);
Variables: Msg("");
Arrays: Arr1[20](0), Arr2[20](0);

{ Deeply nested if/else }
If Close > Open Then Begin
    If High > High[1] Then Begin
        If Low > Low[1] Then Begin
            If Volume > Volume[1] Then Begin
                A = 1;
            End Else Begin
                A = 2;
            End;
        End Else Begin
            A = 3;
        End;
    End Else Begin
        A = 4;
    End;
End Else Begin
    A = 5;
End;

{ Complex boolean chains }
Cond1 = Close > Open And High > High[1] And Volume > Average(Volume, 20);
Cond2 = Close < Open Or Low < Low[1] Or RSI(Close, 14) < 30;

If Cond1 And Not Cond2 Then
    B = 1
Else If Cond2 And Not Cond1 Then
    B = 2
Else
    B = 0;

{ For loop with bar reference expression index }
For A = 1 To 10 Begin
    Arr1[A] = Close[A];
    Arr2[A] = High[A] - Low[A];
End;

{ Multiple orders in sequence }
If Cond1 Then Begin
    Buy ("SIG1") Next Bar At Market;
    SetStopLoss(500);
    SetProfitTarget(1000);
    SetDollarTrailing(300);
    SetBreakEven(200);
End;

{ String concatenation chain }
Msg = "O=" + NumToStr(Open, 2) + " H=" + NumToStr(High, 2) + " L=" + NumToStr(Low, 2) + " C=" + NumToStr(Close, 2);
Print(Msg);

{ Negative numbers and zero }
A = -1;
B = 0;
C = -0.5;
D = AbsValue(C);

{ Arithmetic with parentheses }
A = (Close - Open) / (High - Low + 0.0001);
B = (High + Low + Close) / 3;

{ While with break }
A = 0;
While A < 100 Begin
    A = A + 1;
    If A > 50 Then Break;
End;

Plot1(A, "A");
Plot2(B, "B");
"""

def test_script7_edge_cases():
    py = fresh_transpile(SCRIPT_7_EDGE)
    compile(py, '<test7>', 'exec')
    result = exec_strategy(py)
    assert 'plots' in result
    assert 'plot1' in result['plots']


if __name__ == '__main__':
    pytest.main([__file__, '-v', '--tb=long'])
