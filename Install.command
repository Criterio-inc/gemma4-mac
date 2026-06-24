#!/usr/bin/env bash
# Double-click this file in Finder to install gemma4-mac.
# It just runs install.sh and keeps the window open so you can read the result.
cd "$(dirname "$0")" || exit 1

echo "Installing Gemma for Mac — this can take a few minutes the first time."
echo "If macOS asks for your password, type it and press Return (it stays hidden)."
echo

bash ./install.sh
status=$?

echo
if [[ $status -eq 0 ]]; then
  echo "✅ Done! You can close this window. To start Gemma, double-click Gemma.command."
else
  echo "⚠️  Something went wrong (see the messages above). You can close this window."
fi
echo
echo "Press Return to close…"
read -r _
