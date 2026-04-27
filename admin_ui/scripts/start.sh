#!/bin/bash

# Blue Stream Admin UI Startup Script

echo "🚀 Starting Blue Stream Admin UI..."

# Check if node_modules exists
if [ ! -d "node_modules" ]; then
    echo "📦 Installing dependencies..."
    npm install
fi

# Check if .env exists, create from .env.example if not
if [ ! -f ".env" ]; then
    echo "⚙️ Creating .env from .env.example..."
    cp .env.example .env
    echo "Please update .env with your actual configuration values"
fi

# Run in development mode
echo "🌟 Starting development server..."
echo "Admin UI will be available at: http://localhost:3000"
echo "API should be running at: http://localhost:5000"
echo ""
npm start
