@echo off
REM Downloads the National Bridge Inventory (NBI) and National Tunnel
REM Inventory (NTI) from FHWA into the folder this file is in. The RAT
REM Suite reads every .txt, .csv and .xml file in its NBI folder, so keep
REM only one year's files here.

REM ======================================================================
REM CONFIGURATION
REM ======================================================================
REM Year of the FHWA release to download.
set YEAR=2025
REM ======================================================================

set DEST=%~dp0

echo.
echo ======================================================================
echo Downloading the %YEAR% National Bridge Inventory and
echo National Tunnel Inventory from FHWA to:
echo %DEST%
echo The NBI file is about 150 MB and may take a few minutes.
echo ======================================================================
echo.

echo Downloading %YEAR%AllRecordsDelimitedAllStates.txt...
powershell -Command "(New-Object Net.WebClient).DownloadFile('https://www.fhwa.dot.gov/bridge/nbi/%YEAR%AllRecordsDelimitedAllStates.txt', '%DEST%%YEAR%AllRecordsDelimitedAllStates.txt')"
if errorlevel 1 echo Download failed. Check the year and your connection.

echo Downloading %YEAR%NTI.xml...
powershell -Command "(New-Object Net.WebClient).DownloadFile('https://www.fhwa.dot.gov/bridge/nti/%YEAR%NTI.xml', '%DEST%%YEAR%NTI.xml')"
if errorlevel 1 echo Download failed. Check the year and your connection.

echo.
echo ======================================================================
echo Done. Remove files from earlier years from this folder.
echo ======================================================================
echo.
pause