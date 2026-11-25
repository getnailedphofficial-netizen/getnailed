@echo off
echo ===================================================
echo Pushing Get Nailed to GitHub for Vercel Deployment
echo ===================================================
echo.
cd /d "%~dp0"
git remote set-url origin https://getnailedphofficial-netizen@github.com/getnailedphofficial-netizen/getnailed.git
echo Running git push origin main...
echo.
echo If a browser window opens, please click "Sign in with your browser"
echo and authorize with your account: getnailedphofficial-netizen
echo.
git push origin main
if %ERRORLEVEL% EQU 0 (
    echo.
    echo ===================================================
    echo SUCCESS! Pushed to GitHub.
    echo Vercel will now automatically build and deploy!
    echo ===================================================
) else (
    echo.
    echo Error during push. Please check your GitHub account authorization above.
)
echo.
pause
