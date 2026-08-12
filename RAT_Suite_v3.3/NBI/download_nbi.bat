@echo off
REM Download National Bridge Inventory from FHWA

echo.
echo ======================================================================
echo Downloading National Bridge Inventory (NBI) and 
echo National Tunnel Inventory from FHWA...
echo This will take a few minutes (the NBI file is ~150 MB)
echo ======================================================================
echo.

REM Create NBI folder if it doesn't exist
if not exist "NBI" mkdir NBI

REM Download current NBI file
echo Downloading 2025AllRecordsDelimitedAllStates.txt...
powershell -Command "(New-Object Net.WebClient).DownloadFile('https://www.fhwa.dot.gov/bridge/nbi/2025AllRecordsDelimitedAllStates.txt', 'NBI/2025AllRecordsDelimitedAllStates.txt')"

REM Download NTI file
echo Downloading 2025NTI.xml...
powershell -Command "(New-Object Net.WebClient).DownloadFile('https://www.fhwa.dot.gov/bridge/nti/2025NTI.xml', 'NBI/2025NTI.xml')"

echo.
echo ======================================================================
echo Download complete! Files saved to NBI/ folder
echo ======================================================================
echo.
pause