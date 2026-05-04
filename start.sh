#!/usr/bin/env bash

echo "Starting bot..."
python bot.py &

echo "Starting dashboard..."
uvicorn app:app --host 0.0.0.0 --port 10000