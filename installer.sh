#!/bin/bash

# Exit on any error
set -e

echo "Starting installation of Air Quality Monitor dependencies..."

# Install Python dependencies
echo "Installing Python packages..."
pip install --break-system-packages -r requirements.txt

echo "Dependencies installed successfully!"