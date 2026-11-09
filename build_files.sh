#!/bin/bash
set -e
echo "Starting build process..."
python3 --version

# Create virtualenv to isolate build dependencies and satisfy PEP 668
python3 -m venv .build_venv
source .build_venv/bin/activate

if command -v uv &> /dev/null; then
    echo "Installing requirements with uv..."
    uv pip install -r requirements.txt
else
    echo "Installing requirements with pip..."
    pip install --upgrade pip
    pip install -r requirements.txt
fi

echo "Collecting static files..."
python3 manage.py collectstatic --noinput --clear

echo "Static files build finished successfully!"
