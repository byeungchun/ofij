#!/bin/bash

# Absolute path to where your script is located
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON_SCRIPT="$SCRIPT_DIR/newshead_retriever.py"

# Set the log file destination (update if you changed path in your Python script!)
LOG_DIR="$HOME/data/news"
LOG_FILE="$LOG_DIR/newshead_retriever.out"
PID_FILE="$LOG_DIR/news_daemon.pid"

# Make sure log dir exists
mkdir -p "$LOG_DIR"

# Check for existing PID
if [ -f "$PID_FILE" ]; then
    PID=$(cat "$PID_FILE")
    if ps -p $PID > /dev/null 2>&1; then
        echo "Script already running with PID $PID."
        exit 1
    else
        echo "Removing stale PID file."
        rm -f "$PID_FILE"
    fi
fi

# Run the script with nohup
echo "Launching newshead_retriever.py..."
nohup python3 "$PYTHON_SCRIPT" >> "$LOG_FILE" 2>&1 &

# Optionally inform about log file
echo "Logs will be written to: $LOG_FILE"
echo "PID file: $PID_FILE"