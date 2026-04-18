@echo off
echo ========================================
echo Starting CapitalTwo Server...
echo ========================================
cd /d "%~dp0"
pnpm exec tsx watch server/_core/index.ts



