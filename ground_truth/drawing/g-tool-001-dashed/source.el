{ The g-tl-001-new trend lines set to Tool_Dashed and Blue.
  Keyed on Time, not CurrentBar, so MaxBarsBack cannot shift it.
  Orders are fixed-label next-bar market orders at 10:00 and 14:00. }
Vars: TLID(-1);
If Mod(Time, 100) = 0 and Time >= 1000 Then Begin
	TLID = TL_New(Date[29], Time[29], Low[29], Date, Time, High);
	TL_SetStyle(TLID, Tool_Dashed);
	TL_SetColor(TLID, Blue);
End;
If Time = 1000 Then Buy ("c001_LE") next bar at market;
If Time = 1400 Then Sell ("c001_LX") next bar at market;
