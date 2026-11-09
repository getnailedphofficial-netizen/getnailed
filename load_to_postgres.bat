@echo off
set /p DB_URL="Enter your PostgreSQL DATABASE_URL: "
if "%DB_URL%"=="" (
    echo No DATABASE_URL entered. Exiting.
    pause
    exit /b
)
set "DATABASE_URL=%DB_URL%"
echo Applying migrations to PostgreSQL...
".\.venv\Scripts\python.exe" manage.py migrate
echo Loading existing data (users, services, appointments, sales)...
".\.venv\Scripts\python.exe" manage.py loaddata datadump.json
echo Database setup completed successfully!
pause
