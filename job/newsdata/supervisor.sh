#!/bin/bash

# nohup ./supervisor.sh >> $HOME/data/news/supervisor.log 2>&1 &
source "$HOME/Env/ofij/bin/activate"

# Absolute path to where run_newshead_retriever.sh is located
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LAUNCHER_SCRIPT="$SCRIPT_DIR/run_newshead_retriever.sh"

# This script runs endless loop
while true; do
    echo "[$(date)] Starting run_newshead_retriever.sh..."
    # Run launcher in background, capture its PID
    bash "$LAUNCHER_SCRIPT" &
    LAUNCHER_PID=$!

    # Wait for launcher to set its child PID (may need a few seconds)
    LOG_DIR="$HOME/data/news"
    PID_FILE="$LOG_DIR/news_daemon.pid"
    for i in {1..10}; do
        if [ -f "$PID_FILE" ]; then
            CHILD_PID=$(cat "$PID_FILE")
            if ps -p $CHILD_PID > /dev/null 2>&1; then
                echo "[$(date)] Detected running child script with PID $CHILD_PID."
                break
            fi
        fi
        sleep 2
    done

    # Let the python script run for 20 hours (72000 seconds)
    SLEEP_SECONDS=$((20 * 60 * 60))
    echo "[$(date)] Letting script run for $SLEEP_SECONDS seconds..."
    sleep $SLEEP_SECONDS

    # Attempt to kill the child process
    if [ -f "$PID_FILE" ]; then
        CHILD_PID=$(cat "$PID_FILE")
        if ps -p $CHILD_PID > /dev/null 2>&1; then
            echo "[$(date)] Killing child process $CHILD_PID after 20 hours."
            kill $CHILD_PID
            # Wait up to 10 seconds for process to terminate
            for i in {1..10}; do
                if ! ps -p $CHILD_PID > /dev/null 2>&1; then
                    break
                fi
                sleep 1
            done
            # If still alive, force kill
            if ps -p $CHILD_PID > /dev/null 2>&1; then
                echo "[$(date)] Forcing kill to child process $CHILD_PID."
                kill -9 $CHILD_PID
            fi
        fi
        rm -f "$PID_FILE"
    fi

    echo "[$(date)] Waiting for 5 minutes before restart..."
    sleep 300
done