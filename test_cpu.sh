#!/bin/bash
while true; do true; done &
PID=$!
sleep 2
python3 monitor.py | grep high_cpu
kill $PID
