{ The g-tl-001-new trend lines, recoloured by hour with colour words and colour functions.
  Keyed on Time, not CurrentBar, so MaxBarsBack cannot shift it.
  Orders are fixed-label next-bar market orders at 10:00 and 14:00. }
Vars: TLID(-1);
If Mod(Time, 100) = 0 and Time >= 1000 Then Begin
	TLID = TL_New(Date[29], Time[29], Low[29], Date, Time, High);
	If Time = 1100 Then TL_SetColor(TLID, DarkBrown);
	If Time = 1200 Then TL_SetColor(TLID, LegacyColorToRGB(14));
	If Time = 1300 Then TL_SetColor(TLID, RGB(10, 20, 30));
	If Time = 1400 Then TL_SetColor(TLID, GradientColor(5, 0, 10, Red, Blue));
End;
If Time = 1000 Then Buy ("c001_LE") next bar at market;
If Time = 1400 Then Sell ("c001_LX") next bar at market;
