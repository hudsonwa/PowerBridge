{ Text_New at the 12:00 High with default style.
  Keyed on Time, not CurrentBar, so MaxBarsBack cannot shift it.
  Orders are fixed-label next-bar market orders at 10:00 and 14:00. }
Vars: XID(-1);
If Time = 1200 Then XID = Text_New(Date, Time, High, "H" + NumToStr(High, 2));
If Time = 1000 Then Buy ("c001_LE") next bar at market;
If Time = 1400 Then Sell ("c001_LX") next bar at market;
