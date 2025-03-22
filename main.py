#!/usr/bin/env python3
"""
SDS011 Air Quality Monitoring System with AI Assistant "Puff"
A comprehensive system for monitoring air quality using the SDS011 sensor,
featuring a modern web interface and voice-activated AI assistant.
"""

import os
import json
import time
import logging
import sqlite3
import threading
from datetime import datetime
from flask import Flask, jsonify, request, send_from_directory
import serial
from flask_sock import Sock

# Global Configuration
DB_FILENAME = "sensor_data.db"
SENSOR_PORT = "/dev/ttyUSB0"  # Adjust based on your system
BAUD_RATE = 9600
READ_TIMEOUT = 2
SENSOR_READ_INTERVAL = 5  # seconds between readings

# Initialize Flask app and WebSocket
app = Flask(__name__)
sock = Sock(app)

# Global WebSocket clients list
ws_clients = set()

# Initialize logger
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('air_quality.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

def init_db():
    """Initialize the SQLite database."""
    try:
        conn = sqlite3.connect(DB_FILENAME)
        cursor = conn.cursor()
        
        # Create sensor readings table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS sensor_readings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                pm25 REAL NOT NULL,
                pm10 REAL NOT NULL,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        # Create settings table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS settings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                pm25_warning REAL DEFAULT 12.0,
                pm25_critical REAL DEFAULT 35.0,
                pm10_warning REAL DEFAULT 54.0,
                pm10_critical REAL DEFAULT 154.0,
                pm25_calibration REAL DEFAULT 1.0,
                pm10_calibration REAL DEFAULT 1.0,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        # Insert default settings if none exist
        cursor.execute('SELECT COUNT(*) FROM settings')
        if cursor.fetchone()[0] == 0:
            cursor.execute('''
                INSERT INTO settings (
                    pm25_warning, pm25_critical, 
                    pm10_warning, pm10_critical,
                    pm25_calibration, pm10_calibration
                ) VALUES (12.0, 35.0, 54.0, 154.0, 1.0, 1.0)
            ''')
        
        conn.commit()
        conn.close()
        logger.info("Database initialized successfully")
    except Exception as e:
        logger.error(f"Database initialization error: {str(e)}")
        raise

def scan_for_sensor():
    """Scan available serial ports for the SDS011 sensor."""
    import glob
    import sys
    
    if sys.platform.startswith('win'):
        ports = ['COM%s' % (i + 1) for i in range(256)]
    else:
        ports = glob.glob('/dev/tty[A-Za-z]*')
    
    logger.info("Scanning for SDS011 sensor...")
    
    for port in ports:
        try:
            ser = serial.Serial(
                port=port,
                baudrate=BAUD_RATE,
                timeout=1  # Short timeout for scanning
            )
            
            # Try to read data - SDS011 should send 10-byte packets
            data = ser.read(10)
            if len(data) == 10 and data[0] == 0xAA and data[1] == 0xC0:
                logger.info(f"Found SDS011 sensor on port {port}")
                ser.timeout = READ_TIMEOUT  # Reset to normal timeout
                return ser
            
            ser.close()
        except (OSError, serial.SerialException):
            continue
    
    raise Exception("No SDS011 sensor found. Please check the connection.")

def setup_sensor():
    """Initialize and return a serial connection to the SDS011 sensor."""
    max_retries = 3
    retry_delay = 2  # seconds
    
    for attempt in range(max_retries):
        try:
            return scan_for_sensor()
        except Exception as e:
            if attempt < max_retries - 1:
                logger.warning(f"Attempt {attempt + 1} failed: {str(e)}. Retrying in {retry_delay} seconds...")
                time.sleep(retry_delay)
            else:
                logger.error(f"Failed to connect to sensor after {max_retries} attempts: {str(e)}")
                raise

def read_sensor_data(ser):
    """Read and parse data from the SDS011 sensor."""
    try:
        # SDS011 data packet is 10 bytes long
        data = ser.read(10)
        
        if len(data) == 10 and data[0] == 0xAA and data[1] == 0xC0:
            pm25 = float(data[2] + data[3] * 256) / 10.0
            pm10 = float(data[4] + data[5] * 256) / 10.0
            return pm25, pm10
        return None, None
    except Exception as e:
        logger.error(f"Error reading sensor data: {str(e)}")
        return None, None

def sensor_loop():
    """Main loop for reading sensor data and storing it in the database."""
    try:
        ser = setup_sensor()
        while True:
            pm25, pm10 = read_sensor_data(ser)
            if pm25 is not None and pm10 is not None:
                # Apply calibration factors
                settings = get_settings()
                if settings:
                    pm25 *= settings['pm25_calibration']
                    pm10 *= settings['pm10_calibration']
                
                insert_reading(pm25, pm10)
                broadcast_update()
                logger.debug(f"Recorded reading - PM2.5: {pm25}, PM10: {pm10}")
            time.sleep(SENSOR_READ_INTERVAL)
    except Exception as e:
        logger.error(f"Sensor loop error: {str(e)}")
        time.sleep(5)  # Wait before retrying

def insert_reading(pm25, pm10, timestamp=None):
    """Insert a new sensor reading into the database."""
    if timestamp is None:
        timestamp = datetime.now()
    
    try:
        conn = sqlite3.connect(DB_FILENAME)
        cursor = conn.cursor()
        cursor.execute(
            'INSERT INTO sensor_readings (pm25, pm10, timestamp) VALUES (?, ?, ?)',
            (pm25, pm10, timestamp)
        )
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error(f"Error inserting sensor reading: {str(e)}")
        raise

def query_current():
    """Get the most recent sensor reading."""
    try:
        conn = sqlite3.connect(DB_FILENAME)
        cursor = conn.cursor()
        cursor.execute('''
            SELECT pm25, pm10, timestamp 
            FROM sensor_readings 
            ORDER BY timestamp DESC 
            LIMIT 1
        ''')
        result = cursor.fetchone()
        conn.close()
        
        if result:
            return {
                'pm25': result[0],
                'pm10': result[1],
                'timestamp': result[2]
            }
        return None
    except Exception as e:
        logger.error(f"Error querying current reading: {str(e)}")
        raise

def query_history(timeframe='24h'):
    """Get historical sensor readings based on timeframe."""
    try:
        conn = sqlite3.connect(DB_FILENAME)
        cursor = conn.cursor()
        
        if timeframe == '24h':
            time_filter = "datetime('now', '-1 day')"
        elif timeframe == '7d':
            time_filter = "datetime('now', '-7 days')"
        elif timeframe == '30d':
            time_filter = "datetime('now', '-30 days')"
        else:
            time_filter = "datetime('now', '-1 day')"
        
        cursor.execute(f'''
            SELECT pm25, pm10, timestamp 
            FROM sensor_readings 
            WHERE timestamp > {time_filter}
            ORDER BY timestamp ASC
        ''')
        
        results = cursor.fetchall()
        conn.close()
        
        return {
            'pm25_values': [r[0] for r in results],
            'pm10_values': [r[1] for r in results],
            'timestamps': [r[2] for r in results]
        }
    except Exception as e:
        logger.error(f"Error querying historical data: {str(e)}")
        raise

def get_settings():
    """Get current settings from the database."""
    try:
        conn = sqlite3.connect(DB_FILENAME)
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM settings ORDER BY id DESC LIMIT 1')
        result = cursor.fetchone()
        conn.close()

        if result:
            return {
                'pm25_warning': result[1],
                'pm25_critical': result[2],
                'pm10_warning': result[3],
                'pm10_critical': result[4],
                'pm25_calibration': result[5],
                'pm10_calibration': result[6]
            }
        return None
    except Exception as e:
        logger.error(f"Error getting settings: {str(e)}")
        raise

def update_settings(settings):
    """Update settings in the database."""
    try:
        conn = sqlite3.connect(DB_FILENAME)
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO settings (
                pm25_warning, pm25_critical,
                pm10_warning, pm10_critical,
                pm25_calibration, pm10_calibration
            ) VALUES (?, ?, ?, ?, ?, ?)
        ''', (
            settings['pm25_warning'],
            settings['pm25_critical'],
            settings['pm10_warning'],
            settings['pm10_critical'],
            settings['pm25_calibration'],
            settings['pm10_calibration']
        ))
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error(f"Error updating settings: {str(e)}")
        raise

@sock.route('/ws')
def ws_handler(ws):
    """Handle WebSocket connections."""
    ws_clients.add(ws)
    try:
        while True:
            ws.receive()  # Keep connection alive
    except:
        ws_clients.remove(ws)

def broadcast_update():
    """Broadcast updates to all connected clients."""
    message = json.dumps({'type': 'update'})
    dead_clients = set()
    
    for client in ws_clients:
        try:
            client.send(message)
        except:
            dead_clients.add(client)
    
    for client in dead_clients:
        ws_clients.remove(client)

# Flask Routes
@app.route('/')
def index():
    """Serve the main dashboard page."""
    return send_from_directory('static', 'index.html')

@app.route('/<path:path>')
def serve_static(path):
    """Serve static files."""
    return send_from_directory('static', path)

@app.route('/api/current')
def api_current():
    """Return the current sensor reading."""
    try:
        reading = query_current()
        if reading:
            return jsonify(reading)
        return jsonify({'error': 'No sensor data available'}), 404
    except Exception as e:
        logger.error(f"Error in /api/current: {str(e)}")
        return jsonify({'error': 'Internal server error'}), 500

@app.route('/api/history')
def api_history():
    """Return historical sensor data."""
    try:
        timeframe = request.args.get('timeframe', '24h')
        data = query_history(timeframe)
        return jsonify(data)
    except Exception as e:
        logger.error(f"Error in /api/history: {str(e)}")
        return jsonify({'error': 'Internal server error'}), 500

@app.route('/api/settings', methods=['GET', 'POST'])
def api_settings():
    """Handle settings retrieval and updates."""
    try:
        if request.method == 'GET':
            settings = get_settings()
            if settings:
                return jsonify(settings)
            return jsonify({'error': 'No settings found'}), 404
            
        elif request.method == 'POST':
            data = request.get_json()
            if not data:
                return jsonify({'error': 'No data provided'}), 400
                
            required_fields = [
                'pm25_warning', 'pm25_critical',
                'pm10_warning', 'pm10_critical',
                'pm25_calibration', 'pm10_calibration'
            ]
            
            if not all(field in data for field in required_fields):
                return jsonify({'error': 'Missing required fields'}), 400
                
            update_settings(data)
            return jsonify({'message': 'Settings updated successfully'})
            
    except Exception as e:
        logger.error(f"Error in /api/settings: {str(e)}")
        return jsonify({'error': 'Internal server error'}), 500

@app.errorhandler(404)
def not_found_error(error):
    """Handle 404 errors."""
    return jsonify({'error': 'Resource not found'}), 404

@app.errorhandler(500)
def internal_error(error):
    """Handle 500 errors."""
    return jsonify({'error': 'Internal server error'}), 500

def main():
    """Initialize the application and start the required threads."""
    try:
        # Initialize database
        init_db()
        
        # Create static directory if it doesn't exist
        os.makedirs('static', exist_ok=True)
        
        # Start sensor reading thread
        sensor_thread = threading.Thread(target=sensor_loop, daemon=True)
        sensor_thread.start()
        
        # Start Flask server
        app.run(host='0.0.0.0', port=8000)
        
    except Exception as e:
        logger.error(f"Application startup error: {str(e)}")
        raise

if __name__ == '__main__':
    main()
