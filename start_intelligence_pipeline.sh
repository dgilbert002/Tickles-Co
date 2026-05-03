#!/bin/bash
# Launcher script for the intelligence pipeline services
# Usage: ./start_intelligence_pipeline.sh

cd /opt/tickles

export $(grep -v '^#' .env | xargs)

LOG_DIR="/opt/tickles/logs"
mkdir -p "$LOG_DIR"

echo "=== Starting Intelligence Pipeline Services ==="
echo "Log directory: $LOG_DIR"
echo ""

# 1. Discord Collector
echo "[1/4] Starting Discord Collector..."
nohup python3 -m shared.collectors.discord.discord_collector > "$LOG_DIR/discord_collector.log" 2>&1 &
echo $! > "$LOG_DIR/discord_collector.pid"
echo "  PID: $(cat $LOG_DIR/discord_collector.pid)"

# 2. Interpretation Service
echo "[2/4] Starting Interpretation Service..."
nohup python3 -m shared.intelligence.interpretation_service > "$LOG_DIR/interpretation_service.log" 2>&1 &
echo $! > "$LOG_DIR/interpretation_service.pid"
echo "  PID: $(cat $LOG_DIR/interpretation_service.pid)"

# 3. Position Monitor
echo "[3/4] Starting Position Monitor..."
nohup python3 -m shared.intelligence.position_monitor > "$LOG_DIR/position_monitor.log" 2>&1 &
echo $! > "$LOG_DIR/position_monitor.pid"
echo "  PID: $(cat $LOG_DIR/position_monitor.pid)"

# 4. ChartHacker Guru
echo "[4/4] Starting ChartHacker Guru..."
nohup python3 -m shared.intelligence.chart_hacker_guru > "$LOG_DIR/chart_hacker_guru.log" 2>&1 &
echo $! > "$LOG_DIR/chart_hacker_guru.pid"
echo "  PID: $(cat $LOG_DIR/chart_hacker_guru.pid)"

echo ""
echo "=== All services started ==="
echo ""
echo "Monitor logs:"
echo "  tail -f $LOG_DIR/discord_collector.log"
echo "  tail -f $LOG_DIR/interpretation_service.log"
echo "  tail -f $LOG_DIR/position_monitor.log"
echo "  tail -f $LOG_DIR/chart_hacker_guru.log"
echo ""
echo "Stop all: kill $(cat $LOG_DIR/*.pid | tr '\n' ' ')"
