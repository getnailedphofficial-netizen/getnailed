#!/bin/bash
echo "Installing dependencies..."
if command -v uv &> /dev/null; then
    uv pip install --system -r requirements.txt
else
    python3 -m pip install --break-system-packages -r requirements.txt || pip install --break-system-packages -r requirements.txt
fi

echo "Collecting static files..."
python3 manage.py collectstatic --noinput --clear
echo "Build complete."
