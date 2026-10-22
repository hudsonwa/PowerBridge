{ The g-tl-001-new trend lines anchored on Data2 closes; orders run on Data1.
  Keyed on Time, not CurrentBar, so MaxBarsBack cannot shift it.
  Orders are fixed-label next-bar market orders at 10:00 and 14:00. }
Vars: TLID(-1), C2(0);
C2 = Close of Data2;
If Mod(Time, 100) = 0 and Time >= 1000 Then
	TLID = TL_New(Date[29], Time[29], C2[29], Date, Time, C2);
If Time = 1000 Then Buy ("c001_LE") next bar at market;
If Time = 1400 Then Sell ("c001_LX") next bar at market;
