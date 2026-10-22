{ Arw_New with default style: an up arrow at the 10:00 Low, a down arrow at the 14:00 High.
  Keyed on Time, not CurrentBar, so MaxBarsBack cannot shift it.
  Orders are fixed-label next-bar market orders at 10:00 and 14:00. }
Vars: AID(-1);
If Time = 1000 Then AID = Arw_New(Date, Time, Low, false);
If Time = 1400 Then AID = Arw_New(Date, Time, High, true);
If Time = 1000 Then Buy ("c001_LE") next bar at market;
If Time = 1400 Then Sell ("c001_LX") next bar at market;
