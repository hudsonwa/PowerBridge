{ A trend line at every on-the-hour bar from 10:00, Low[29] to High.
  Keyed on Time, not CurrentBar, so MaxBarsBack cannot shift it.
  Orders are fixed-label next-bar market orders at 10:00 and 14:00. }
Vars: TLID(-1), N(0);
If Mod(Time, 100) = 0 and Time >= 1000 Then Begin
	TLID = TL_New(Date[29], Time[29], Low[29], Date, Time, High);
	N = N + 1;
End;
If Time = 1000 Then Buy ("c001_LE") next bar at market;
If Time = 1400 Then Sell ("c001_LX") next bar at market;
