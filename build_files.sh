#!/bin/bash
set -e
echo "Starting build process..."
python3 --version

# Create virtualenv in /tmp so it does not inflate the Lambda package size
BUILD_VENV="/tmp/.build_venv"
rm -rf "$BUILD_VENV"
python3 -m venv "$BUILD_VENV"
source "$BUILD_VENV/bin/activate"

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

# Clean up build virtualenv immediately
rm -rf "$BUILD_VENV"

echo "Static files build finished successfully!"
