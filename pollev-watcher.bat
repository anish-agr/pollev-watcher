@echo off
title PollEv Watcher
cd /d "%~dp0"
echo PollEv Watcher - starts a run on GitHub, so this computer can turn off after.
echo.
echo   1  Watch your class
echo   2  Watch a different username
echo   3  Send a test alert to every channel
echo   4  Run the fake-poll test (no PagerDuty or email)
echo.
set "choice=1"
set /p "choice=Pick 1-4 (Enter = 1): "
if "%choice%"=="3" goto test
if "%choice%"=="4" goto simulate

set "user="
if "%choice%"=="2" set /p "user=PollEv username: "
set "minutes=60"
set /p "minutes=How many minutes? (Enter = 60): "
gh workflow run watch.yml -f minutes=%minutes% -f user=%user%
goto done

:test
gh workflow run watch.yml -f test_alerts=true -f minutes=1
goto done

:simulate
gh workflow run watch.yml -f simulate=15 -f minutes=3

:done
echo.
echo Started. You should get a push in about a minute.
gh repo view --json url -q ".url + \"/actions\""
echo.
pause
