#!/bin/bash
cd "$(dirname "$0")"
export PATH="/opt/homebrew/bin:/usr/local/bin:/Library/Frameworks/Python.framework/Versions/Current/bin:$PATH"
python3 pack_for_mac.py
open "$HOME/Desktop"
read -p "Press Enter to close this window..."
