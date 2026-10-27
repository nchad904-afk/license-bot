"""
BabaYaga Trading Bot - License Management Bot v2.0
Full Admin Control via Telegram + HTTP API for Bot Verification
Deployed on Render.com Free Tier
"""

import os
import json
import sqlite3
import random
import string
import logging
import threading
import asyncio
from flask import Flask, request, jsonify
from datetime import datetime, timedelta
from typing import Tuple, List, Optional, Dict
from functools import wraps

# PostgreSQL support
try:
    import psycopg2
    from psycopg2.extras import RealDictCursor
    HAS_POSTGRES = True
except ImportError:
    HAS_POSTGRES = False

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, 
    CommandHandler, 
    MessageHandler, 
    CallbackQueryHandler,
    filters,
    ContextTypes
)

# Setup logging
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════════════════════
# CONFIGURATION
# ═══════════════════════════════════════════════════════════════════════════════

BOT_TOKEN = os.environ.get("TOKEN", "8447286153:AAG0Y_eURHTVVGH68He8IJkXVIEjB_-mY-w")
try:
    ADMIN_ID = int(os.environ.get("ADMIN_ID", "6234558719"))
except:
    ADMIN_ID = 6234558719

# Database Configuration - Neon/Postgres preferred, SQLite fallback
# Set DATABASE_URL on Render to a Neon connection string, e.g.:
# postgresql://USER:PASS@HOST.neon.tech/neondb?sslmode=require
DATABASE_URL = os.environ.get("DATABASE_URL")
DB_FILE = "licenses.db"
USE_POSTGRES = bool(DATABASE_URL and HAS_POSTGRES)


def _normalize_database_url(url: str) -> str:
    """Normalize postgres URL for psycopg2 / Neon / Render."""
    if not url:
        return url
    url = url.strip()
    # Some hosts still issue postgres:// — psycopg2 expects postgresql://
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]
    return url


def _friendly_db_error(exc: Exception) -> str:
    """Turn low-level DB errors into actionable admin messages."""
    msg = str(exc)
    lower = msg.lower()
    if "could not translate host name" in lower or "name or service not known" in lower:
        return (
            "DATABASE_URL host unreachable. "
            "Create a free Neon Postgres DB and set DATABASE_URL on Render "
            "(postgresql://...@....neon.tech/neondb?sslmode=require), then redeploy."
        )
    if "connection refused" in lower or "timeout" in lower or "timed out" in lower:
        return (
            "Database connection timed out/refused. "
            "Check Neon project is active and DATABASE_URL is correct on Render."
        )
    if "password authentication failed" in lower or "authentication failed" in lower:
        return "Database auth failed. Check Neon username/password in DATABASE_URL."
    if "ssl" in lower:
        return f"Database SSL error. Ensure URL has sslmode=require. Detail: {msg[:120]}"
    return f"Database error: {msg[:200]}"

# License Packages Configuration
LICENSE_PACKAGES = {
    "TRIAL": {"duration_days": 7, "price": "FREE", "price_bdt": "০"},
    "BASIC": {"duration_days": 30, "price": "$100", "price_bdt": "১২,০০০৳"},
    "PRO": {"duration_days": 90, "price": "$250", "price_bdt": "৩০,০০০৳"},
    "VIP": {"duration_days": 365, "price": "$1000", "price_bdt": "১,২০,০০০৳"},
    "LIFETIME": {"duration_days": 36500, "price": "$2500", "price_bdt": "৩,০০,০০০৳"}
}

# Support Contact (for customers to buy)
SUPPORT_TELEGRAM = "@Babayagatradingsupport_bot"
SUPPORT_LINK = "https://t.me/Babayagatradingsupport_bot"

# Store customer chat IDs for support replies
customer_chats = {}  # {user_id: {"name": name, "username": username, "last_msg_id": msg_id}}

# ═══════════════════════════════════════════════════════════════════════════════
# DATABASE FUNCTIONS
# ═══════════════════════════════════════════════════════════════════════════════

class PostgresCursorWrapper:
    """Wrapper to make PostgreSQL cursor behave like SQLite cursor"""
    def __init__(self, cursor):
        self._cursor = cursor
        self._columns = []
    
    def execute(self, query, params=None):
        # Convert SQLite ? placeholders to PostgreSQL %s
        query = query.replace('?', '%s')
        if params:
            self._cursor.execute(query, params)
        else:
            self._cursor.execute(query)
        if self._cursor.description:
            self._columns = [desc[0] for desc in self._cursor.description]
    
    def fetchone(self):
        row = self._cursor.fetchone()
        if row and self._columns:
            return dict(zip(self._columns, row))
        return row
    
    def fetchall(self):
        rows = self._cursor.fetchall()
        if rows and self._columns:
            return [dict(zip(self._columns, row)) for row in rows]
        return rows
    
    @property
    def lastrowid(self):
        return self._cursor.fetchone()[0] if hasattr(self._cursor, 'fetchone') else None

class PostgresConnectionWrapper:
    """Wrapper to make PostgreSQL connection behave like SQLite connection"""
    def __init__(self, conn):
        self._conn = conn
    
    def cursor(self):
        return PostgresCursorWrapper(self._conn.cursor())
    
    def commit(self):
        self._conn.commit()
    
    def close(self):
        self._conn.close()
    
    def rollback(self):
        self._conn.rollback()

def get_db():
    """Get database connection - Neon/PostgreSQL or SQLite."""
    if USE_POSTGRES:
        url = _normalize_database_url(DATABASE_URL)
        connect_kwargs = {"connect_timeout": 10}
        # Neon/Render require SSL; skip if already in the URL
        if "sslmode=" not in url.lower():
            connect_kwargs["sslmode"] = "require"
        try:
            conn = psycopg2.connect(url, **connect_kwargs)
            return PostgresConnectionWrapper(conn)
        except Exception as e:
            logger.error(f"Database connection failed: {_friendly_db_error(e)}")
            raise
    else:
        conn = sqlite3.connect(DB_FILE)
        conn.row_factory = sqlite3.Row
        return conn


def check_db_health() -> Dict:
    """Lightweight DB health probe for /api/health and status page."""
    backend = "postgres" if USE_POSTGRES else "sqlite"
    try:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("SELECT 1")
        cursor.fetchone()
        conn.close()
        return {
            "ok": True,
            "db": "up",
            "backend": backend,
            "message": "Database connected",
        }
    except Exception as e:
        return {
            "ok": False,
            "db": "down",
            "backend": backend,
            "message": _friendly_db_error(e),
        }

def init_database():
    """Initialize database tables"""
    conn = get_db()
    cursor = conn.cursor()
    
    if USE_POSTGRES:
        # PostgreSQL tables
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS licenses (
                id SERIAL PRIMARY KEY,
                license_key TEXT UNIQUE NOT NULL,
                customer_name TEXT NOT NULL,
                customer_telegram TEXT,
                package TEXT NOT NULL,
                created_date TEXT NOT NULL,
                expiry_date TEXT NOT NULL,
                is_active INTEGER DEFAULT 1,
                max_devices INTEGER DEFAULT 2,
                current_devices INTEGER DEFAULT 0,
                last_verified TEXT,
                verify_count INTEGER DEFAULT 0,
                notes TEXT,
                created_by TEXT DEFAULT 'ADMIN'
            )
        ''')
        
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS devices (
                id SERIAL PRIMARY KEY,
                license_key TEXT NOT NULL,
                device_id TEXT NOT NULL,
                device_name TEXT,
                os_info TEXT,
                ip_address TEXT,
                first_seen TEXT,
                last_seen TEXT,
                is_blocked INTEGER DEFAULT 0,
                UNIQUE(license_key, device_id)
            )
        ''')
        
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS verify_logs (
                id SERIAL PRIMARY KEY,
                license_key TEXT NOT NULL,
                verify_time TEXT NOT NULL,
                device_id TEXT,
                ip_address TEXT,
                status TEXT,
                source TEXT DEFAULT 'API'
            )
        ''')
        
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS support_messages (
                id SERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL,
                user_name TEXT,
                username TEXT,
                message TEXT NOT NULL,
                message_type TEXT DEFAULT 'text',
                direction TEXT DEFAULT 'incoming',
                timestamp TEXT NOT NULL,
                is_read INTEGER DEFAULT 0,
                replied INTEGER DEFAULT 0
            )
        ''')
        logger.info("✅ PostgreSQL Database initialized - Data is PERMANENT!")
    else:
        # SQLite tables (fallback)
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS licenses (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                license_key TEXT UNIQUE NOT NULL,
                customer_name TEXT NOT NULL,
                customer_telegram TEXT,
                package TEXT NOT NULL,
                created_date TEXT NOT NULL,
                expiry_date TEXT NOT NULL,
                is_active INTEGER DEFAULT 1,
                max_devices INTEGER DEFAULT 2,
                current_devices INTEGER DEFAULT 0,
                last_verified TEXT,
                verify_count INTEGER DEFAULT 0,
                notes TEXT,
                created_by TEXT DEFAULT 'ADMIN'
            )
        ''')
        
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS devices (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                license_key TEXT NOT NULL,
                device_id TEXT NOT NULL,
                device_name TEXT,
                os_info TEXT,
                ip_address TEXT,
                first_seen TEXT,
                last_seen TEXT,
                is_blocked INTEGER DEFAULT 0,
                UNIQUE(license_key, device_id)
            )
        ''')
        
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS verify_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                license_key TEXT NOT NULL,
                verify_time TEXT NOT NULL,
                device_id TEXT,
                ip_address TEXT,
                status TEXT,
                source TEXT DEFAULT 'API'
            )
        ''')
        
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS support_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                user_name TEXT,
                username TEXT,
                message TEXT NOT NULL,
                message_type TEXT DEFAULT 'text',
                direction TEXT DEFAULT 'incoming',
                timestamp TEXT NOT NULL,
                is_read INTEGER DEFAULT 0,
                replied INTEGER DEFAULT 0
            )
        ''')
        logger.info("⚠️ SQLite Database initialized - Data may be lost on restart!")
    
    conn.commit()
    conn.close()

def generate_license_key(customer_name: str, package: str) -> str:
    """Generate unique license key"""
    name = ''.join(c for c in customer_name.upper() if c.isalnum())[:10]
    year = datetime.now().year
    random_part = ''.join(random.choices(string.ascii_uppercase + string.digits, k=6))
    return f"{name}-BABAYAGA-{package.upper()}-{year}-{random_part}"

# ═══════════════════════════════════════════════════════════════════════════════
# LICENSE CRUD OPERATIONS
# ═══════════════════════════════════════════════════════════════════════════════

def add_license(customer_name: str, package: str, telegram_id: str = None, notes: str = None) -> Tuple[bool, str, str]:
    """Create new license"""
    package = package.upper()
    if package not in LICENSE_PACKAGES:
        return False, "", f"❌ Invalid package. Choose: {', '.join(LICENSE_PACKAGES.keys())}"
    
    pkg = LICENSE_PACKAGES[package]
    license_key = generate_license_key(customer_name, package)
    created = datetime.now()
    expiry = created + timedelta(days=pkg["duration_days"])
    
    try:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO licenses (license_key, customer_name, customer_telegram, package, 
                                  created_date, expiry_date, notes)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        ''', (license_key, customer_name.upper(), telegram_id, package,
              created.strftime("%Y-%m-%d %H:%M:%S"), 
              expiry.strftime("%Y-%m-%d %H:%M:%S"), notes))
        conn.commit()
        conn.close()
        return True, license_key, expiry.strftime('%Y-%m-%d')
    except Exception as e:
        logger.error(f"Add license error: {e}")
        return False, "", str(e)

def get_license_info(license_key: str) -> Optional[Dict]:
    """Get full license details"""
    try:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM licenses WHERE license_key = ?', (license_key.upper().strip(),))
        row = cursor.fetchone()
        conn.close()
        
        if row:
            return dict(row)
        return None
    except Exception as e:
        logger.error(f"Get license error: {e}")
        return None

def verify_license_key(license_key: str, device_id: str = None, ip_address: str = None) -> Tuple[str, str, Dict]:
    """
    Verify license and return status
    Returns: (status, message, data)
    """
    try:
        conn = get_db()
        cursor = conn.cursor()
        
        license_key = license_key.upper().strip()
        cursor.execute('SELECT * FROM licenses WHERE license_key = ?', (license_key,))
        row = cursor.fetchone()
        
        if not row:
            conn.close()
            return "INVALID", "License key not found in database.", {}
        
        license_data = dict(row)
        expiry = datetime.strptime(license_data['expiry_date'], "%Y-%m-%d %H:%M:%S")
        days_left = (expiry - datetime.now()).days
        
        # Check if deactivated
        if not license_data['is_active']:
            log_verification(license_key, device_id, ip_address, "DEACTIVATED")
            conn.close()
            return "INVALID", "License has been DEACTIVATED by admin.\nContact support for help.", {}
        
        # Check if expired
        if datetime.now() > expiry:
            log_verification(license_key, device_id, ip_address, "EXPIRED")
            conn.close()
            return "EXPIRED", f"License EXPIRED on {expiry.strftime('%Y-%m-%d')}!\nPlease renew your license.", {
                "customer": license_data['customer_name'],
                "package": license_data['package'],
                "expired_on": expiry.strftime('%Y-%m-%d')
            }
        
        # Update verification stats
        cursor.execute('''
            UPDATE licenses SET 
                last_verified = ?,
                verify_count = verify_count + 1
            WHERE license_key = ?
        ''', (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), license_key))
        conn.commit()
        
        # Log verification
        log_verification(license_key, device_id, ip_address, "VALID")
        
        # Track device if provided
        if device_id:
            track_device(license_key, device_id, ip_address)
        
        conn.close()
        
        return "VALID", "License is ACTIVE and VALID!", {
            "customer": license_data['customer_name'],
            "package": license_data['package'],
            "days_left": days_left,
            "expiry_date": expiry.strftime('%Y-%m-%d'),
            "verify_count": license_data['verify_count'] + 1
        }
        
    except Exception as e:
        friendly = _friendly_db_error(e)
        logger.error(f"Verify error: {friendly}")
        return "ERROR", friendly, {}

def log_verification(license_key: str, device_id: str, ip_address: str, status: str):
    """Log verification attempt"""
    try:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO verify_logs (license_key, verify_time, device_id, ip_address, status)
            VALUES (?, ?, ?, ?, ?)
        ''', (license_key, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), device_id, ip_address, status))
        conn.commit()
        conn.close()
    except:
        pass

def track_device(license_key: str, device_id: str, ip_address: str = None):
    """Track device usage"""
    try:
        conn = get_db()
        cursor = conn.cursor()
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        
        cursor.execute('''
            INSERT INTO devices (license_key, device_id, ip_address, first_seen, last_seen)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(license_key, device_id) DO UPDATE SET
                last_seen = ?,
                ip_address = ?
        ''', (license_key, device_id, ip_address, now, now, now, ip_address))
        
        conn.commit()
        conn.close()
    except:
        pass

def list_licenses(filter_type: str = "all", limit: int = 50) -> List[Dict]:
    """List licenses with optional filter"""
    try:
        conn = get_db()
        cursor = conn.cursor()
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        
        if filter_type == "active":
            cursor.execute('''
                SELECT * FROM licenses 
                WHERE is_active = 1 AND expiry_date > ?
                ORDER BY created_date DESC LIMIT ?
            ''', (now, limit))
        elif filter_type == "expired":
            cursor.execute('''
                SELECT * FROM licenses 
                WHERE expiry_date < ?
                ORDER BY expiry_date DESC LIMIT ?
            ''', (now, limit))
        elif filter_type == "deactivated":
            cursor.execute('''
                SELECT * FROM licenses 
                WHERE is_active = 0
                ORDER BY created_date DESC LIMIT ?
            ''', (limit,))
        else:
            cursor.execute('SELECT * FROM licenses ORDER BY created_date DESC LIMIT ?', (limit,))
        
        rows = cursor.fetchall()
        conn.close()
        return [dict(row) for row in rows]
    except Exception as e:
        logger.error(f"List error: {e}")
        return []

def search_licenses(query: str) -> List[Dict]:
    """Search licenses by name or key"""
    try:
        conn = get_db()
        cursor = conn.cursor()
        search = f"%{query.upper()}%"
        cursor.execute('''
            SELECT * FROM licenses 
            WHERE customer_name LIKE ? OR license_key LIKE ?
            ORDER BY created_date DESC LIMIT 20
        ''', (search, search))
        rows = cursor.fetchall()
        conn.close()
        return [dict(row) for row in rows]
    except:
        return []

def deactivate_license(license_key: str) -> Tuple[bool, str]:
    """Deactivate a license"""
    try:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('UPDATE licenses SET is_active = 0 WHERE license_key = ?', 
                      (license_key.upper().strip(),))
        if cursor.rowcount == 0:
            conn.close()
            return False, "❌ License not found."
        conn.commit()
        conn.close()
        return True, "✅ License DEACTIVATED successfully!"
    except Exception as e:
        return False, str(e)

def reactivate_license(license_key: str) -> Tuple[bool, str]:
    """Reactivate a license"""
    try:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('UPDATE licenses SET is_active = 1 WHERE license_key = ?', 
                      (license_key.upper().strip(),))
        if cursor.rowcount == 0:
            conn.close()
            return False, "❌ License not found."
        conn.commit()
        conn.close()
        return True, "✅ License REACTIVATED successfully!"
    except Exception as e:
        return False, str(e)

def extend_license(license_key: str, days: int) -> Tuple[bool, str]:
    """Extend license expiry"""
    try:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('SELECT expiry_date FROM licenses WHERE license_key = ?', 
                      (license_key.upper().strip(),))
        result = cursor.fetchone()
        if not result:
            conn.close()
            return False, "❌ License not found."
        
        current = datetime.strptime(result[0], "%Y-%m-%d %H:%M:%S")
        # If expired, extend from today
        if current < datetime.now():
            current = datetime.now()
        new_expiry = current + timedelta(days=days)
        
        cursor.execute('UPDATE licenses SET expiry_date = ?, is_active = 1 WHERE license_key = ?',
                      (new_expiry.strftime("%Y-%m-%d %H:%M:%S"), license_key.upper().strip()))
        conn.commit()
        conn.close()
        return True, f"✅ Extended by {days} days!\nNew expiry: {new_expiry.strftime('%Y-%m-%d')}"
    except Exception as e:
        return False, str(e)

def delete_license(license_key: str) -> Tuple[bool, str]:
    """Delete a license permanently"""
    try:
        conn = get_db()
        cursor = conn.cursor()
        
        # Also delete related devices and logs
        cursor.execute('DELETE FROM devices WHERE license_key = ?', (license_key.upper().strip(),))
        cursor.execute('DELETE FROM verify_logs WHERE license_key = ?', (license_key.upper().strip(),))
        cursor.execute('DELETE FROM licenses WHERE license_key = ?', (license_key.upper().strip(),))
        
        if cursor.rowcount == 0:
            conn.close()
            return False, "❌ License not found."
        conn.commit()
        conn.close()
        return True, "✅ License DELETED permanently!"
    except Exception as e:
        return False, str(e)

def add_note(license_key: str, note: str) -> Tuple[bool, str]:
    """Add note to license"""
    try:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('UPDATE licenses SET notes = ? WHERE license_key = ?',
                      (note, license_key.upper().strip()))
        if cursor.rowcount == 0:
            conn.close()
            return False, "❌ License not found."
        conn.commit()
        conn.close()
        return True, "✅ Note added!"
    except Exception as e:
        return False, str(e)

def get_statistics() -> Dict:
    """Get license statistics"""
    try:
        conn = get_db()
        cursor = conn.cursor()
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        
        cursor.execute('SELECT COUNT(*) FROM licenses')
        total = cursor.fetchone()[0]
        
        cursor.execute('SELECT COUNT(*) FROM licenses WHERE is_active = 1 AND expiry_date > ?', (now,))
        active = cursor.fetchone()[0]
        
        cursor.execute('SELECT COUNT(*) FROM licenses WHERE expiry_date < ?', (now,))
        expired = cursor.fetchone()[0]
        
        cursor.execute('SELECT COUNT(*) FROM licenses WHERE is_active = 0')
        deactivated = cursor.fetchone()[0]
        
        cursor.execute('SELECT COUNT(*) FROM devices')
        devices = cursor.fetchone()[0]
        
        cursor.execute('SELECT COUNT(*) FROM verify_logs WHERE DATE(verify_time) = DATE("now")')
        today_verifications = cursor.fetchone()[0]
        
        # Package breakdown
        cursor.execute('''
            SELECT package, COUNT(*) as count FROM licenses 
            WHERE is_active = 1 AND expiry_date > ?
            GROUP BY package
        ''', (now,))
        packages = {row[0]: row[1] for row in cursor.fetchall()}
        
        conn.close()
        return {
            "total": total,
            "active": active,
            "expired": expired,
            "deactivated": deactivated,
            "devices": devices,
            "today_verifications": today_verifications,
            "packages": packages
        }
    except Exception as e:
        logger.error(f"Stats error: {e}")
        return {}

def get_license_devices(license_key: str) -> List[Dict]:
    """Get devices using a license"""
    try:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM devices WHERE license_key = ? ORDER BY last_seen DESC',
                      (license_key.upper().strip(),))
        rows = cursor.fetchall()
        conn.close()
        return [dict(row) for row in rows]
    except:
        return []

def get_recent_verifications(license_key: str = None, limit: int = 10) -> List[Dict]:
    """Get recent verifications"""
    try:
        conn = get_db()
        cursor = conn.cursor()
        if license_key:
            cursor.execute('''
                SELECT * FROM verify_logs WHERE license_key = ? 
                ORDER BY verify_time DESC LIMIT ?
            ''', (license_key.upper().strip(), limit))
        else:
            cursor.execute('SELECT * FROM verify_logs ORDER BY verify_time DESC LIMIT ?', (limit,))
        rows = cursor.fetchall()
        conn.close()
        return [dict(row) for row in rows]
    except:
        return []

def get_expiring_soon(days: int = 7) -> List[Dict]:
    """Get licenses expiring soon"""
    try:
        conn = get_db()
        cursor = conn.cursor()
        now = datetime.now()
        future = (now + timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
        now_str = now.strftime("%Y-%m-%d %H:%M:%S")
        
        cursor.execute('''
            SELECT * FROM licenses 
            WHERE is_active = 1 AND expiry_date > ? AND expiry_date < ?
            ORDER BY expiry_date ASC
        ''', (now_str, future))
        rows = cursor.fetchall()
        conn.close()
        return [dict(row) for row in rows]
    except:
        return []

# ═══════════════════════════════════════════════════════════════════════════════
# SUPPORT MESSAGE FUNCTIONS
# ═══════════════════════════════════════════════════════════════════════════════

def save_support_message(user_id: int, user_name: str, username: str, message: str, 
                         msg_type: str = 'text', direction: str = 'incoming') -> int:
    """Save a support message to database"""
    try:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO support_messages (user_id, user_name, username, message, message_type, direction, timestamp)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        ''', (user_id, user_name, username, message, msg_type, direction, 
              datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
        msg_id = cursor.lastrowid
        conn.commit()
        conn.close()
        
        # Update customer_chats dict
        customer_chats[user_id] = {
            "name": user_name,
            "username": username,
            "last_msg_id": msg_id
        }
        return msg_id
    except Exception as e:
        logger.error(f"Save support message error: {e}")
        return 0

def get_unread_messages() -> List[Dict]:
    """Get unread support messages"""
    try:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('''
            SELECT * FROM support_messages 
            WHERE direction = 'incoming' AND is_read = 0
            ORDER BY timestamp DESC LIMIT 50
        ''')
        rows = cursor.fetchall()
        conn.close()
        return [dict(row) for row in rows]
    except:
        return []

def get_support_conversations() -> List[Dict]:
    """Get list of unique customers with support messages"""
    try:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('''
            SELECT user_id, user_name, username, 
                   MAX(timestamp) as last_message,
                   COUNT(*) as total_messages,
                   SUM(CASE WHEN direction='incoming' AND is_read=0 THEN 1 ELSE 0 END) as unread
            FROM support_messages 
            GROUP BY user_id
            ORDER BY last_message DESC
            LIMIT 30
        ''')
        rows = cursor.fetchall()
        conn.close()
        return [dict(row) for row in rows]
    except:
        return []

def get_chat_history(user_id: int, limit: int = 20) -> List[Dict]:
    """Get chat history with a specific customer"""
    try:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('''
            SELECT * FROM support_messages 
            WHERE user_id = ?
            ORDER BY timestamp DESC
            LIMIT ?
        ''', (user_id, limit))
        rows = cursor.fetchall()
        conn.close()
        return [dict(row) for row in rows][::-1]  # Reverse to show oldest first
    except:
        return []

def mark_messages_read(user_id: int):
    """Mark all messages from a user as read"""
    try:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('''
            UPDATE support_messages SET is_read = 1 
            WHERE user_id = ? AND direction = 'incoming'
        ''', (user_id,))
        conn.commit()
        conn.close()
    except:
        pass

def get_customer_by_id(user_id: int) -> Optional[Dict]:
    """Get customer info by user_id"""
    if user_id in customer_chats:
        return customer_chats[user_id]
    
    try:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('''
            SELECT user_id, user_name, username 
            FROM support_messages 
            WHERE user_id = ? 
            ORDER BY timestamp DESC LIMIT 1
        ''', (user_id,))
        row = cursor.fetchone()
        conn.close()
        if row:
            return {"name": row['user_name'], "username": row['username']}
    except:
        pass
    return None

# ═══════════════════════════════════════════════════════════════════════════════
# TELEGRAM BOT HANDLERS
# ═══════════════════════════════════════════════════════════════════════════════

def is_admin(user_id: int) -> bool:
    """Check if user is admin"""
    return user_id == ADMIN_ID

# ─────────────────────────────────────────────────────────────────────────────
# START COMMAND - Different for Admin vs Customer
# ─────────────────────────────────────────────────────────────────────────────

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start command - shows admin panel or customer menu"""
    user = update.effective_user
    
    if is_admin(user.id):
        # ═══════════════ ADMIN PANEL ═══════════════
        stats = get_statistics()
        msg = f"""
🔐 <b>BabaYaga License Control Panel</b>

👋 Welcome, <b>Admin {user.first_name}</b>!

📊 <b>Quick Stats:</b>
├ Total Licenses: <b>{stats.get('total', 0)}</b>
├ Active: <b>{stats.get('active', 0)}</b> ✅
├ Expired: <b>{stats.get('expired', 0)}</b> ⏰
├ Deactivated: <b>{stats.get('deactivated', 0)}</b> 🚫
└ Today's Verifications: <b>{stats.get('today_verifications', 0)}</b>

👇 Select an option or use commands:
"""
        keyboard = [
            [
                InlineKeyboardButton("➕ Create License", callback_data="admin_create"),
                InlineKeyboardButton("📋 All Licenses", callback_data="admin_list_all")
            ],
            [
                InlineKeyboardButton("✅ Active", callback_data="admin_list_active"),
                InlineKeyboardButton("⏰ Expired", callback_data="admin_list_expired"),
                InlineKeyboardButton("🚫 Disabled", callback_data="admin_list_deactivated")
            ],
            [
                InlineKeyboardButton("🔍 Search", callback_data="admin_search"),
                InlineKeyboardButton("📊 Full Stats", callback_data="admin_stats")
            ],
            [
                InlineKeyboardButton("⚠️ Expiring Soon", callback_data="admin_expiring"),
                InlineKeyboardButton("📝 Recent Activity", callback_data="admin_recent")
            ],
            [
                InlineKeyboardButton("📬 Support Inbox", callback_data="support_inbox"),
                InlineKeyboardButton("❓ Help", callback_data="admin_help")
            ]
        ]
        await update.message.reply_text(msg, parse_mode='HTML', 
                                        reply_markup=InlineKeyboardMarkup(keyboard))
    else:
        # ═══════════════ CUSTOMER MENU ═══════════════
        msg = f"""
🤖 <b>BabaYaga Trading Bot</b>
━━━━━━━━━━━━━━━━━━━━━━━━━━━

🇧🇩 স্বাগতম / 🇬🇧 Welcome, <b>{user.first_name}</b>! 👋

🚀 <b>EN:</b> Premium Auto Trading Bot for MT5
🚀 <b>বাং:</b> MT5 এর জন্য প্রিমিয়াম অটো ট্রেডিং বট

━━━━━━━━━━━━━━━━━━━━━━━━━━━
✨ <b>Features / বৈশিষ্ট্য:</b>

• 🔄 Auto Trading on MT5
   (MT5 এ অটোমেটিক ট্রেডিং)

• 📊 RSI + Candle Pattern Strategy
   (RSI + ক্যান্ডেল প্যাটার্ন স্ট্র্যাটেজি)

• 🛡️ Smart Risk Management
   (স্মার্ট রিস্ক ম্যানেজমেন্ট)

• 📱 Telegram Notifications
   (টেলিগ্রাম নোটিফিকেশন)

━━━━━━━━━━━━━━━━━━━━━━━━━━━
📌 <b>EN:</b> Select an option below
📌 <b>বাং:</b> নিচের অপশন থেকে সিলেক্ট করুন
"""
        keyboard = [
            [InlineKeyboardButton("💰 License Prices / দাম", callback_data="cust_prices")],
            [InlineKeyboardButton("🛒 Buy License / কিনুন", callback_data="cust_buy")],
            [InlineKeyboardButton("🔑 Check License / চেক করুন", callback_data="cust_verify")],
            [InlineKeyboardButton("� Help & Support / সাহায্য", callback_data="cust_help")]
        ]
        await update.message.reply_text(msg, parse_mode='HTML', 
                                        reply_markup=InlineKeyboardMarkup(keyboard))

# ─────────────────────────────────────────────────────────────────────────────
# CUSTOMER QUICK COMMANDS (Menu button commands)
# ─────────────────────────────────────────────────────────────────────────────

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Help command - shows help/support info"""
    user = update.effective_user
    
    if is_admin(user.id):
        # Admin gets different help
        await update.message.reply_text(
            "💡 <b>Admin Commands:</b>\n\n"
            "/add name package - Create license\n"
            "/info key - License details\n"
            "/inbox - Support messages\n"
            "/reply id msg - Reply to customer\n"
            "/stats - Statistics\n\n"
            "Type /start for full menu.",
            parse_mode='HTML'
        )
        return
    
    msg = f"""
💬 <b>Help & Support / সাহায্য ও সাপোর্ট</b>
━━━━━━━━━━━━━━━━━━━━━━━━━━━

👋 Hi <b>{user.first_name}</b>!

🇬🇧 <b>EN:</b> Need help? Just type your message below 
and send it! Our support team will reply soon.

🇧🇩 <b>বাং:</b> সাহায্য দরকার? নিচে আপনার message 
লিখে পাঠান! আমাদের support team শীঘ্রই reply দিবে।

━━━━━━━━━━━━━━━━━━━━━━━━━━━
📝 <b>You can ask about / জিজ্ঞাসা করতে পারেন:</b>

• License purchase / লাইসেন্স কেনা
• Payment methods / পেমেন্ট পদ্ধতি
• Bot features / বট ফিচার্স
• Technical issues / টেকনিক্যাল সমস্যা

━━━━━━━━━━━━━━━━━━━━━━━━━━━
✍️ <b>Type your message now!</b>
✍️ <b>এখনই আপনার message লিখুন!</b>
"""
    keyboard = [[InlineKeyboardButton("🏠 Main Menu / মেনু", callback_data="cust_back")]]
    await update.message.reply_text(msg, parse_mode='HTML', reply_markup=InlineKeyboardMarkup(keyboard))

async def prices_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Prices command - shows license prices"""
    msg = """
💰 <b>License Packages & Prices</b>
💰 <b>লাইসেন্স প্যাকেজ ও দাম</b>
━━━━━━━━━━━━━━━━━━━━━━━━━━━

📦 <b>TRIAL</b> - 7 Days / ৭ দিন
   └ Price/দাম: <b>FREE / ফ্রি</b> (Limited/সীমিত)

📦 <b>BASIC</b> - 30 Days / ৩০ দিন
   └ Price/দাম: <b>$100 / ১২,০০০৳</b>

📦 <b>PRO</b> - 90 Days / ৯০ দিন ⭐ Popular
   └ Price/দাম: <b>$250 / ৩০,০০০৳</b>

📦 <b>VIP</b> - 1 Year / ১ বছর
   └ Price/দাম: <b>$1000 / ১,২০,০০০৳</b>

📦 <b>LIFETIME</b> - Forever / চিরকাল 👑
   └ Price/দাম: <b>$2500 / ৩,০০,০০০৳</b>

━━━━━━━━━━━━━━━━━━━━━━━━━━━
✅ <b>All packages include:</b>
• Full Bot Access • 24/7 Support
• Free Updates • Multi-Device (2 PCs)
"""
    keyboard = [
        [InlineKeyboardButton("🛒 Buy Now / এখনই কিনুন", callback_data="cust_buy")],
        [InlineKeyboardButton("🏠 Main Menu / মেনু", callback_data="cust_back")]
    ]
    await update.message.reply_text(msg, parse_mode='HTML', reply_markup=InlineKeyboardMarkup(keyboard))

async def buy_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Buy command - shows how to buy"""
    msg = f"""
🛒 <b>Buy License / লাইসেন্স কিনুন</b>
━━━━━━━━━━━━━━━━━━━━━━━━━━━

📞 <b>Support:</b> {SUPPORT_TELEGRAM}

🇬🇧 <b>EN:</b> Contact our support team to buy license
🇧🇩 <b>বাং:</b> লাইসেন্স কিনতে আমাদের সাপোর্ট টিমে যোগাযোগ করুন

━━━━━━━━━━━━━━━━━━━━━━━━━━━
📝 <b>How to Buy / কিভাবে কিনবেন:</b>

1️⃣ Type your message below
   (নিচে message লিখুন)

2️⃣ Tell your name & desired package
   (আপনার নাম ও প্যাকেজ বলুন)

3️⃣ Complete payment
   (পেমেন্ট সম্পূর্ণ করুন)

4️⃣ Receive your License Key instantly!
   (সাথে সাথে License Key পেয়ে যাবেন!)

━━━━━━━━━━━━━━━━━━━━━━━━━━━
💳 <b>Payment Methods:</b>
Bkash • Nagad • Rocket • USDT • Bank

━━━━━━━━━━━━━━━━━━━━━━━━━━━
✍️ <b>Type "I want to buy" to start!</b>
✍️ <b>"আমি কিনতে চাই" লিখে পাঠান!</b>
"""
    keyboard = [
        [InlineKeyboardButton("💰 View Prices / দাম দেখুন", callback_data="cust_prices")],
        [InlineKeyboardButton("🏠 Main Menu / মেনু", callback_data="cust_back")]
    ]
    await update.message.reply_text(msg, parse_mode='HTML', reply_markup=InlineKeyboardMarkup(keyboard))

# ─────────────────────────────────────────────────────────────────────────────
# ADMIN CALLBACK HANDLERS
# ─────────────────────────────────────────────────────────────────────────────

async def admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle admin button callbacks"""
    query = update.callback_query
    await query.answer()
    
    if not is_admin(query.from_user.id):
        await query.edit_message_text("⛔ Admin access required!")
        return
    
    data = query.data
    
    # ═══════════════ CREATE LICENSE ═══════════════
    if data == "admin_create":
        msg = """
➕ <b>Create New License</b>

Use command:
<code>/add CustomerName Package</code>

<b>Packages:</b>
• TRIAL - 7 days (Free)
• BASIC - 30 days ($100)
• PRO - 90 days ($250)
• VIP - 1 year ($1000)
• LIFETIME - Forever ($2500)

<b>Examples:</b>
<code>/add RAKIB PRO</code>
<code>/add KARIM VIP</code>
"""
        keyboard = [[InlineKeyboardButton("🔙 Back", callback_data="admin_back")]]
        await query.edit_message_text(msg, parse_mode='HTML', 
                                     reply_markup=InlineKeyboardMarkup(keyboard))
    
    # ═══════════════ LIST LICENSES ═══════════════
    elif data.startswith("admin_list_"):
        filter_type = data.replace("admin_list_", "")
        licenses = list_licenses(filter_type)
        
        if not licenses:
            msg = f"📋 No {filter_type} licenses found."
        else:
            title_map = {
                "all": "📋 All Licenses",
                "active": "✅ Active Licenses", 
                "expired": "⏰ Expired Licenses",
                "deactivated": "🚫 Deactivated Licenses"
            }
            msg = f"<b>{title_map.get(filter_type, 'Licenses')}</b>\n\n"
            
            for i, lic in enumerate(licenses[:15], 1):
                exp = datetime.strptime(lic['expiry_date'], "%Y-%m-%d %H:%M:%S")
                days = (exp - datetime.now()).days
                
                if not lic['is_active']:
                    status = "🚫"
                elif days < 0:
                    status = "⏰"
                elif days <= 7:
                    status = "⚠️"
                else:
                    status = "✅"
                
                msg += f"{i}. {status} <b>{lic['customer_name']}</b>\n"
                msg += f"   📦 {lic['package']} | "
                msg += f"{'Expired' if days < 0 else f'{days}d left'}\n"
                msg += f"   <code>{lic['license_key'][:25]}...</code>\n\n"
        
        keyboard = [
            [
                InlineKeyboardButton("✅ Active", callback_data="admin_list_active"),
                InlineKeyboardButton("⏰ Expired", callback_data="admin_list_expired")
            ],
            [InlineKeyboardButton("🔙 Back", callback_data="admin_back")]
        ]
        await query.edit_message_text(msg, parse_mode='HTML', 
                                     reply_markup=InlineKeyboardMarkup(keyboard))
    
    # ═══════════════ SEARCH ═══════════════
    elif data == "admin_search":
        msg = """
🔍 <b>Search License</b>

Use command:
<code>/search CustomerName</code>
or
<code>/search LicenseKey</code>

<b>Examples:</b>
<code>/search RAKIB</code>
<code>/search BABAYAGA-PRO</code>
"""
        keyboard = [[InlineKeyboardButton("🔙 Back", callback_data="admin_back")]]
        await query.edit_message_text(msg, parse_mode='HTML', 
                                     reply_markup=InlineKeyboardMarkup(keyboard))
    
    # ═══════════════ FULL STATISTICS ═══════════════
    elif data == "admin_stats":
        stats = get_statistics()
        packages = stats.get('packages', {})
        
        pkg_text = ""
        for pkg, count in packages.items():
            pkg_text += f"   • {pkg}: {count}\n"
        
        msg = f"""
📊 <b>Full Statistics</b>

<b>License Overview:</b>
├ Total Created: <b>{stats.get('total', 0)}</b>
├ Currently Active: <b>{stats.get('active', 0)}</b> ✅
├ Expired: <b>{stats.get('expired', 0)}</b> ⏰
└ Deactivated: <b>{stats.get('deactivated', 0)}</b> 🚫

<b>Active by Package:</b>
{pkg_text if pkg_text else '   No active licenses'}

<b>Usage:</b>
├ Total Devices: <b>{stats.get('devices', 0)}</b>
└ Today's Verifications: <b>{stats.get('today_verifications', 0)}</b>

📅 Updated: {datetime.now().strftime('%Y-%m-%d %H:%M')}
"""
        keyboard = [[InlineKeyboardButton("🔙 Back", callback_data="admin_back")]]
        await query.edit_message_text(msg, parse_mode='HTML', 
                                     reply_markup=InlineKeyboardMarkup(keyboard))
    
    # ═══════════════ EXPIRING SOON ═══════════════
    elif data == "admin_expiring":
        expiring = get_expiring_soon(14)  # Next 14 days
        
        if not expiring:
            msg = "✅ No licenses expiring in the next 14 days!"
        else:
            msg = "⚠️ <b>Licenses Expiring Soon (14 days)</b>\n\n"
            for lic in expiring:
                exp = datetime.strptime(lic['expiry_date'], "%Y-%m-%d %H:%M:%S")
                days = (exp - datetime.now()).days
                msg += f"• <b>{lic['customer_name']}</b> ({lic['package']})\n"
                msg += f"  Expires in <b>{days}</b> days ({exp.strftime('%Y-%m-%d')})\n\n"
        
        keyboard = [[InlineKeyboardButton("🔙 Back", callback_data="admin_back")]]
        await query.edit_message_text(msg, parse_mode='HTML', 
                                     reply_markup=InlineKeyboardMarkup(keyboard))
    
    # ═══════════════ RECENT ACTIVITY ═══════════════
    elif data == "admin_recent":
        recent = get_recent_verifications(limit=15)
        
        if not recent:
            msg = "📝 No recent verifications."
        else:
            msg = "📝 <b>Recent Verifications</b>\n\n"
            for log in recent:
                time = log['verify_time'][11:16]  # HH:MM
                date = log['verify_time'][:10]
                key_short = log['license_key'][:15]
                status_emoji = "✅" if log['status'] == "VALID" else "❌"
                msg += f"{status_emoji} {date} {time}\n"
                msg += f"   <code>{key_short}...</code>\n\n"
        
        keyboard = [[InlineKeyboardButton("🔙 Back", callback_data="admin_back")]]
        await query.edit_message_text(msg, parse_mode='HTML', 
                                     reply_markup=InlineKeyboardMarkup(keyboard))
    
    # ═══════════════ HELP ═══════════════
    elif data == "admin_help":
        msg = """
❓ <b>Admin Commands</b>

<b>📝 License Management:</b>
/add name package - Create license
/info key - Full license details
/check key - Quick verify
/search query - Search by name/key

<b>🔧 License Control:</b>
/deactivate key - Disable license
/reactivate key - Enable license
/extend key days - Extend expiry
/delete key - Delete permanently
/note key text - Add note

<b>📊 Reports:</b>
/list - All licenses
/active - Active only
/expired - Expired only
/stats - Statistics
/expiring - Expiring soon

<b>🔍 Device & Activity:</b>
/devices key - View devices
/logs key - Verification logs

<b>� Support Messages:</b>
/inbox - View support inbox
/reply user_id msg - Reply to customer
/chat user_id - Enter chat mode
/endchat - Exit chat mode

<b>�📦 Packages:</b>
TRIAL (7d) | BASIC (30d) | PRO (90d)
VIP (365d) | LIFETIME (Forever)
"""
        keyboard = [[InlineKeyboardButton("🔙 Back", callback_data="admin_back")]]
        await query.edit_message_text(msg, parse_mode='HTML', 
                                     reply_markup=InlineKeyboardMarkup(keyboard))
    
    # ═══════════════ BACK TO MAIN ═══════════════
    elif data == "admin_back":
        stats = get_statistics()
        msg = f"""
🔐 <b>BabaYaga License Control Panel</b>

👋 Welcome, <b>Admin</b>!

📊 <b>Quick Stats:</b>
├ Total Licenses: <b>{stats.get('total', 0)}</b>
├ Active: <b>{stats.get('active', 0)}</b> ✅
├ Expired: <b>{stats.get('expired', 0)}</b> ⏰
├ Deactivated: <b>{stats.get('deactivated', 0)}</b> 🚫
└ Today's Verifications: <b>{stats.get('today_verifications', 0)}</b>

👇 Select an option:
"""
        keyboard = [
            [
                InlineKeyboardButton("➕ Create License", callback_data="admin_create"),
                InlineKeyboardButton("📋 All Licenses", callback_data="admin_list_all")
            ],
            [
                InlineKeyboardButton("✅ Active", callback_data="admin_list_active"),
                InlineKeyboardButton("⏰ Expired", callback_data="admin_list_expired"),
                InlineKeyboardButton("🚫 Disabled", callback_data="admin_list_deactivated")
            ],
            [
                InlineKeyboardButton("🔍 Search", callback_data="admin_search"),
                InlineKeyboardButton("📊 Full Stats", callback_data="admin_stats")
            ],
            [
                InlineKeyboardButton("⚠️ Expiring Soon", callback_data="admin_expiring"),
                InlineKeyboardButton("📝 Recent Activity", callback_data="admin_recent")
            ],
            [
                InlineKeyboardButton("📬 Support Inbox", callback_data="support_inbox"),
                InlineKeyboardButton("❓ Help", callback_data="admin_help")
            ]
        ]
        await query.edit_message_text(msg, parse_mode='HTML', 
                                     reply_markup=InlineKeyboardMarkup(keyboard))

# ─────────────────────────────────────────────────────────────────────────────
# CUSTOMER CALLBACK HANDLERS  
# ─────────────────────────────────────────────────────────────────────────────

async def customer_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle customer button callbacks"""
    query = update.callback_query
    await query.answer()
    
    data = query.data
    
    # ═══════════════ PRICES ═══════════════
    if data == "cust_prices":
        msg = """
💰 <b>License Packages & Prices</b>
💰 <b>লাইসেন্স প্যাকেজ ও দাম</b>
━━━━━━━━━━━━━━━━━━━━━━━━━━━

📦 <b>TRIAL</b> - 7 Days / ৭ দিন
   └ Price/দাম: <b>FREE / ফ্রি</b> (Limited/সীমিত)

📦 <b>BASIC</b> - 30 Days / ৩০ দিন
   └ Price/দাম: <b>$100 / ১২,০০০৳</b>

📦 <b>PRO</b> - 90 Days / ৯০ দিন ⭐ Popular
   └ Price/দাম: <b>$250 / ৩০,০০০৳</b>

📦 <b>VIP</b> - 1 Year / ১ বছর
   └ Price/দাম: <b>$1000 / ১,২০,০০০৳</b>

📦 <b>LIFETIME</b> - Forever / চিরকাল 👑
   └ Price/দাম: <b>$2500 / ৩,০০,০০০৳</b>

━━━━━━━━━━━━━━━━━━━━━━━━━━━
✅ <b>All packages include / সব প্যাকেজে আছে:</b>

• Full Bot Access (সম্পূর্ণ বট অ্যাক্সেস)
• All Trading Pairs (সব ট্রেডিং পেয়ার)
• 24/7 Support (২৪/৭ সাপোর্ট)
• Free Updates (ফ্রি আপডেট)
• Multi-Device - 2 PCs (২টি পিসি)
"""
        keyboard = [
            [InlineKeyboardButton("🛒 Buy Now / এখনই কিনুন", callback_data="cust_buy")],
            [InlineKeyboardButton("🔙 Back / পিছনে", callback_data="cust_back")]
        ]
        await query.edit_message_text(msg, parse_mode='HTML', 
                                     reply_markup=InlineKeyboardMarkup(keyboard))
    
    # ═══════════════ BUY ═══════════════
    elif data == "cust_buy":
        msg = f"""
🛒 <b>Buy License / লাইসেন্স কিনুন</b>
━━━━━━━━━━━━━━━━━━━━━━━━━━━

📞 <b>Support:</b> {SUPPORT_TELEGRAM}

🇬🇧 <b>EN:</b> Contact our support team to buy license
🇧🇩 <b>বাং:</b> লাইসেন্স কিনতে আমাদের সাপোর্ট টিমে যোগাযোগ করুন

━━━━━━━━━━━━━━━━━━━━━━━━━━━
📝 <b>How to Buy / কিভাবে কিনবেন:</b>

1️⃣ Click "Contact Support" button
   ("Contact Support" বাটনে ক্লিক করুন)

2️⃣ Tell your name & desired package
   (আপনার নাম ও প্যাকেজ বলুন)

3️⃣ Complete payment
   (পেমেন্ট সম্পূর্ণ করুন)

4️⃣ Receive your License Key instantly!
   (সাথে সাথে License Key পেয়ে যাবেন!)

━━━━━━━━━━━━━━━━━━━━━━━━━━━
💳 <b>Payment Methods / পেমেন্ট পদ্ধতি:</b>

• 📱 Bkash (বিকাশ)
• 📱 Nagad (নগদ)
• 📱 Rocket (রকেট)
• 💰 USDT (Crypto)
• 🏦 Bank Transfer

━━━━━━━━━━━━━━━━━━━━━━━━━━━
⚡ <b>Instant Delivery after Payment!</b>
⚡ <b>পেমেন্টের পর সাথে সাথে ডেলিভারি!</b>
"""
        keyboard = [
            [InlineKeyboardButton("📞 Contact Support / যোগাযোগ", url=SUPPORT_LINK)],
            [InlineKeyboardButton("💰 View Prices / দাম দেখুন", callback_data="cust_prices")],
            [InlineKeyboardButton("🔙 Back / পিছনে", callback_data="cust_back")]
        ]
        await query.edit_message_text(msg, parse_mode='HTML', 
                                     reply_markup=InlineKeyboardMarkup(keyboard))
    
    # ═══════════════ VERIFY ═══════════════
    elif data == "cust_verify":
        msg = """
🔑 <b>Check Your License / লাইসেন্স চেক করুন</b>
━━━━━━━━━━━━━━━━━━━━━━━━━━━

🇬🇧 <b>EN:</b> Paste your License Key below and send!
🇧🇩 <b>বাং:</b> আপনার License Key নিচে paste করে Send করুন!

━━━━━━━━━━━━━━━━━━━━━━━━━━━
📋 <b>Format / ফরম্যাট:</b>
<code>NAME-BABAYAGA-PACKAGE-YEAR-CODE</code>

📋 <b>Example / উদাহরণ:</b>
<code>RAKIB-BABAYAGA-PRO-2026-KP9X2M</code>

━━━━━━━━━━━━━━━━━━━━━━━━━━━
⬇️ <b>Send your Key now!</b>
⬇️ <b>এখনই আপনার Key পাঠান!</b>
"""
        keyboard = [[InlineKeyboardButton("🔙 Back / পিছনে", callback_data="cust_back")]]
        await query.edit_message_text(msg, parse_mode='HTML', 
                                     reply_markup=InlineKeyboardMarkup(keyboard))
    
    # ═══════════════ HELP / SUPPORT ═══════════════
    elif data == "cust_help":
        user = query.from_user
        msg = f"""
💬 <b>Help & Support / সাহায্য ও সাপোর্ট</b>
━━━━━━━━━━━━━━━━━━━━━━━━━━━

👋 Hi <b>{user.first_name}</b>!

🇬🇧 <b>EN:</b> Need help? Just type your message below 
and send it! Our support team will reply soon.

🇧🇩 <b>বাং:</b> সাহায্য দরকার? নিচে আপনার message 
লিখে পাঠান! আমাদের support team শীঘ্রই reply দিবে।

━━━━━━━━━━━━━━━━━━━━━━━━━━━
📝 <b>You can ask about / জিজ্ঞাসা করতে পারেন:</b>

• License purchase / লাইসেন্স কেনা
• Payment methods / পেমেন্ট পদ্ধতি
• Bot features / বট ফিচার্স
• Technical issues / টেকনিক্যাল সমস্যা
• Any other questions / অন্য যেকোনো প্রশ্ন

━━━━━━━━━━━━━━━━━━━━━━━━━━━
⏱️ <b>Response Time / উত্তর সময়:</b>
• Usually 1-2 hours / সাধারণত ১-২ ঘণ্টা
• Max 24 hours / সর্বোচ্চ ২৪ ঘণ্টা

━━━━━━━━━━━━━━━━━━━━━━━━━━━
✍️ <b>Type your message now!</b>
✍️ <b>এখনই আপনার message লিখুন!</b>
"""
        keyboard = [[InlineKeyboardButton("🔙 Back / পিছনে", callback_data="cust_back")]]
        await query.edit_message_text(msg, parse_mode='HTML', 
                                     reply_markup=InlineKeyboardMarkup(keyboard))
    
    # ═══════════════ BACK ═══════════════
    elif data == "cust_back":
        user = query.from_user
        msg = f"""
🤖 <b>BabaYaga Trading Bot</b>
━━━━━━━━━━━━━━━━━━━━━━━━━━━

🇧🇩 স্বাগতম / 🇬🇧 Welcome, <b>{user.first_name}</b>! 👋

🚀 <b>EN:</b> Premium Auto Trading Bot for MT5
🚀 <b>বাং:</b> MT5 এর জন্য প্রিমিয়াম অটো ট্রেডিং বট

━━━━━━━━━━━━━━━━━━━━━━━━━━━
📌 <b>EN:</b> You need a License Key to use the bot
📌 <b>বাং:</b> বট ব্যবহার করতে License Key প্রয়োজন

📌 <b>EN:</b> Select an option below
📌 <b>বাং:</b> নিচের অপশন থেকে সিলেক্ট করুন
"""
        keyboard = [
            [InlineKeyboardButton("💰 Prices / দাম", callback_data="cust_prices")],
            [InlineKeyboardButton("🛒 Buy / কিনুন", callback_data="cust_buy")],
            [InlineKeyboardButton("🔑 Check License / চেক", callback_data="cust_verify")],
            [InlineKeyboardButton("💬 Help & Support / সাহায্য", callback_data="cust_help")]
        ]
        await query.edit_message_text(msg, parse_mode='HTML', 
                                     reply_markup=InlineKeyboardMarkup(keyboard))

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Route callbacks to appropriate handler"""
    query = update.callback_query
    
    if query.data.startswith("admin_"):
        await admin_callback(update, context)
    elif query.data.startswith("cust_"):
        await customer_callback(update, context)

# ─────────────────────────────────────────────────────────────────────────────
# ADMIN COMMAND HANDLERS
# ─────────────────────────────────────────────────────────────────────────────

async def add_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Create new license"""
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Admin only command.")
        return
    
    if len(context.args) < 2:
        msg = """
<b>Usage:</b> /add <code>name package</code>

<b>Packages:</b> TRIAL, BASIC, PRO, VIP, LIFETIME

<b>Examples:</b>
<code>/add RAKIB PRO</code>
<code>/add KARIM BASIC</code>
"""
        await update.message.reply_text(msg, parse_mode='HTML')
        return
    
    name = context.args[0]
    package = context.args[1].upper()
    
    success, key, expiry = add_license(name, package)
    
    if success:
        pkg_info = LICENSE_PACKAGES.get(package, {})
        msg = f"""
✅ <b>LICENSE CREATED!</b>

👤 <b>Customer:</b> {name.upper()}
📦 <b>Package:</b> {package}
💰 <b>Price:</b> {pkg_info.get('price', 'N/A')}
📅 <b>Duration:</b> {pkg_info.get('duration_days', 'N/A')} days
⏰ <b>Expires:</b> {expiry}

🔑 <b>License Key:</b>
<code>{key}</code>

📋 Copy করে Customer কে দিন!
"""
    else:
        msg = f"❌ Failed: {expiry}"
    
    await update.message.reply_text(msg, parse_mode='HTML')

async def info_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Get full license info"""
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Admin only command.")
        return
    
    if len(context.args) < 1:
        await update.message.reply_text("Usage: /info <license_key>")
        return
    
    key = context.args[0]
    lic = get_license_info(key)
    
    if not lic:
        await update.message.reply_text("❌ License not found.")
        return
    
    exp = datetime.strptime(lic['expiry_date'], "%Y-%m-%d %H:%M:%S")
    created = datetime.strptime(lic['created_date'], "%Y-%m-%d %H:%M:%S")
    days_left = (exp - datetime.now()).days
    
    status = "✅ ACTIVE" if lic['is_active'] and days_left >= 0 else "🚫 DEACTIVATED" if not lic['is_active'] else "⏰ EXPIRED"
    
    devices = get_license_devices(key)
    device_text = ""
    for d in devices[:3]:
        device_text += f"   • {d.get('device_id', 'Unknown')[:20]}...\n"
    
    msg = f"""
🔍 <b>LICENSE DETAILS</b>

<b>🔑 Key:</b>
<code>{lic['license_key']}</code>

<b>Status:</b> {status}

━━━━━━━━━━━━━━━━━━━━━
<b>👤 Customer:</b> {lic['customer_name']}
<b>📦 Package:</b> {lic['package']}
<b>📅 Created:</b> {created.strftime('%Y-%m-%d')}
<b>⏰ Expires:</b> {exp.strftime('%Y-%m-%d')}
<b>📆 Days Left:</b> {days_left if days_left >= 0 else 'EXPIRED'}

━━━━━━━━━━━━━━━━━━━━━
<b>📊 Usage Stats:</b>
├ Verifications: {lic['verify_count']}
├ Last Check: {lic['last_verified'] or 'Never'}
└ Devices: {len(devices)}/{lic['max_devices']}
"""
    
    # Add device info if exists
    if device_text:
        msg += f"\n<b>💻 Devices:</b>\n{device_text}"
    if lic['notes']:
        msg += f"\n<b>📝 Notes:</b> {lic['notes']}"
    
    # Action buttons
    keyboard = []
    if lic['is_active']:
        keyboard.append([InlineKeyboardButton("🚫 Deactivate", callback_data=f"action_deactivate_{lic['id']}")])
    else:
        keyboard.append([InlineKeyboardButton("✅ Reactivate", callback_data=f"action_reactivate_{lic['id']}")])
    
    await update.message.reply_text(msg, parse_mode='HTML')

async def check_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Quick verify license"""
    if len(context.args) < 1:
        await update.message.reply_text("Usage: /check <license_key>")
        return
    
    status, message, data = verify_license_key(context.args[0])
    
    emoji = "✅" if status == "VALID" else "⏰" if status == "EXPIRED" else "❌"
    
    msg = f"{emoji} <b>{status}</b>\n\n{message}"
    
    if data:
        if 'days_left' in data:
            msg += f"\n\n👤 {data.get('customer', 'N/A')}\n📦 {data.get('package', 'N/A')}\n📅 {data.get('days_left', 0)} days remaining"
    
    await update.message.reply_text(msg, parse_mode='HTML')

async def search_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Search licenses"""
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Admin only command.")
        return
    
    if len(context.args) < 1:
        await update.message.reply_text("Usage: /search <name or key>")
        return
    
    query = ' '.join(context.args)
    results = search_licenses(query)
    
    if not results:
        await update.message.reply_text(f"❌ No results for '{query}'")
        return
    
    msg = f"🔍 <b>Search Results for '{query}':</b>\n\n"
    
    for i, lic in enumerate(results[:10], 1):
        exp = datetime.strptime(lic['expiry_date'], "%Y-%m-%d %H:%M:%S")
        days = (exp - datetime.now()).days
        status = "✅" if lic['is_active'] and days >= 0 else "🚫" if not lic['is_active'] else "⏰"
        
        msg += f"{i}. {status} <b>{lic['customer_name']}</b>\n"
        msg += f"   📦 {lic['package']} | {days}d left\n"
        msg += f"   <code>{lic['license_key']}</code>\n\n"
    
    await update.message.reply_text(msg, parse_mode='HTML')

async def list_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """List all licenses"""
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Admin only command.")
        return
    
    licenses = list_licenses("all")
    
    if not licenses:
        await update.message.reply_text("📋 No licenses found.")
        return
    
    msg = "📋 <b>All Licenses:</b>\n\n"
    
    for i, lic in enumerate(licenses[:20], 1):
        exp = datetime.strptime(lic['expiry_date'], "%Y-%m-%d %H:%M:%S")
        days = (exp - datetime.now()).days
        
        if not lic['is_active']:
            status = "🚫"
        elif days < 0:
            status = "⏰"
        elif days <= 7:
            status = "⚠️"
        else:
            status = "✅"
        
        msg += f"{i}. {status} <b>{lic['customer_name']}</b> | {lic['package']}\n"
    
    await update.message.reply_text(msg, parse_mode='HTML')

async def active_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """List active licenses"""
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Admin only command.")
        return
    
    licenses = list_licenses("active")
    
    if not licenses:
        await update.message.reply_text("✅ No active licenses found.")
        return
    
    msg = "✅ <b>Active Licenses:</b>\n\n"
    
    for i, lic in enumerate(licenses[:20], 1):
        exp = datetime.strptime(lic['expiry_date'], "%Y-%m-%d %H:%M:%S")
        days = (exp - datetime.now()).days
        msg += f"{i}. <b>{lic['customer_name']}</b> | {lic['package']} | {days}d\n"
    
    await update.message.reply_text(msg, parse_mode='HTML')

async def expired_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """List expired licenses"""
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Admin only command.")
        return
    
    licenses = list_licenses("expired")
    
    if not licenses:
        await update.message.reply_text("⏰ No expired licenses.")
        return
    
    msg = "⏰ <b>Expired Licenses:</b>\n\n"
    
    for i, lic in enumerate(licenses[:20], 1):
        msg += f"{i}. <b>{lic['customer_name']}</b> | {lic['package']}\n"
    
    await update.message.reply_text(msg, parse_mode='HTML')

async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show statistics"""
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Admin only command.")
        return
    
    stats = get_statistics()
    packages = stats.get('packages', {})
    
    pkg_text = ""
    for pkg, count in packages.items():
        pkg_text += f"• {pkg}: {count}\n"
    
    msg = f"""
📊 <b>License Statistics</b>

<b>Overview:</b>
├ Total: {stats.get('total', 0)}
├ Active: {stats.get('active', 0)} ✅
├ Expired: {stats.get('expired', 0)} ⏰
└ Deactivated: {stats.get('deactivated', 0)} 🚫

<b>By Package:</b>
{pkg_text if pkg_text else 'No active licenses'}

<b>Usage:</b>
├ Devices: {stats.get('devices', 0)}
└ Today Checks: {stats.get('today_verifications', 0)}
"""
    await update.message.reply_text(msg, parse_mode='HTML')

async def deactivate_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Deactivate license"""
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Admin only command.")
        return
    
    if len(context.args) < 1:
        await update.message.reply_text("Usage: /deactivate <key>")
        return
    
    success, msg = deactivate_license(context.args[0])
    await update.message.reply_text(msg)

async def reactivate_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Reactivate license"""
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Admin only command.")
        return
    
    if len(context.args) < 1:
        await update.message.reply_text("Usage: /reactivate <key>")
        return
    
    success, msg = reactivate_license(context.args[0])
    await update.message.reply_text(msg)

async def extend_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Extend license"""
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Admin only command.")
        return
    
    if len(context.args) < 2:
        await update.message.reply_text("Usage: /extend <key> <days>")
        return
    
    try:
        days = int(context.args[1])
    except:
        await update.message.reply_text("❌ Days must be a number.")
        return
    
    success, msg = extend_license(context.args[0], days)
    await update.message.reply_text(msg)

async def delete_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Delete license"""
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Admin only command.")
        return
    
    if len(context.args) < 1:
        await update.message.reply_text("Usage: /delete <key>")
        return
    
    success, msg = delete_license(context.args[0])
    await update.message.reply_text(msg)

async def note_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Add note to license"""
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Admin only command.")
        return
    
    if len(context.args) < 2:
        await update.message.reply_text("Usage: /note <key> <note text>")
        return
    
    key = context.args[0]
    note = ' '.join(context.args[1:])
    
    success, msg = add_note(key, note)
    await update.message.reply_text(msg)

async def devices_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """View devices for a license"""
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Admin only command.")
        return
    
    if len(context.args) < 1:
        await update.message.reply_text("Usage: /devices <key>")
        return
    
    devices = get_license_devices(context.args[0])
    
    if not devices:
        await update.message.reply_text("💻 No devices found for this license.")
        return
    
    msg = "💻 <b>Registered Devices:</b>\n\n"
    
    for i, d in enumerate(devices, 1):
        msg += f"{i}. <code>{d['device_id'][:30]}...</code>\n"
        msg += f"   Last: {d['last_seen']}\n"
        if d['ip_address']:
            msg += f"   IP: {d['ip_address']}\n"
        msg += "\n"
    
    await update.message.reply_text(msg, parse_mode='HTML')

async def logs_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """View verification logs"""
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Admin only command.")
        return
    
    key = context.args[0] if context.args else None
    logs = get_recent_verifications(key, 15)
    
    if not logs:
        await update.message.reply_text("📝 No verification logs found.")
        return
    
    msg = "📝 <b>Verification Logs:</b>\n\n"
    
    for log in logs:
        emoji = "✅" if log['status'] == "VALID" else "❌"
        msg += f"{emoji} {log['verify_time'][:16]}\n"
        msg += f"   <code>{log['license_key'][:20]}...</code>\n\n"
    
    await update.message.reply_text(msg, parse_mode='HTML')

async def expiring_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show expiring licenses"""
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Admin only command.")
        return
    
    days = int(context.args[0]) if context.args else 14
    expiring = get_expiring_soon(days)
    
    if not expiring:
        await update.message.reply_text(f"✅ No licenses expiring in the next {days} days!")
        return
    
    msg = f"⚠️ <b>Expiring in {days} days:</b>\n\n"
    
    for lic in expiring:
        exp = datetime.strptime(lic['expiry_date'], "%Y-%m-%d %H:%M:%S")
        d = (exp - datetime.now()).days
        msg += f"• <b>{lic['customer_name']}</b> ({lic['package']})\n"
        msg += f"  {d} days left ({exp.strftime('%Y-%m-%d')})\n\n"
    
    await update.message.reply_text(msg, parse_mode='HTML')

# ─────────────────────────────────────────────────────────────────────────────
# MESSAGE HANDLER (License Key Verification + Support Messages)
# ─────────────────────────────────────────────────────────────────────────────

# Store the bot application globally for admin replies
bot_app = None

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle text messages - verify license keys or forward support messages"""
    text = update.message.text.strip()
    user = update.effective_user
    
    # ═══════════════ ADMIN REPLY MODE ═══════════════
    # Check if admin is replying to a customer
    if is_admin(user.id) and context.user_data.get('reply_to_user'):
        target_user_id = context.user_data['reply_to_user']
        customer_info = context.user_data.get('reply_to_info', {})
        
        try:
            # Send reply to customer
            await context.bot.send_message(
                chat_id=target_user_id,
                text=f"📩 <b>Support Reply:</b>\n\n{text}",
                parse_mode='HTML'
            )
            
            # Save to database
            save_support_message(
                target_user_id, 
                customer_info.get('name', 'Customer'),
                customer_info.get('username', ''),
                text, 'text', 'outgoing'
            )
            
            # Clear reply mode
            context.user_data['reply_to_user'] = None
            context.user_data['reply_to_info'] = None
            
            await update.message.reply_text(
                f"✅ Message sent to <b>{customer_info.get('name', 'Customer')}</b>!",
                parse_mode='HTML'
            )
        except Exception as e:
            await update.message.reply_text(f"❌ Failed to send: {e}")
        return
    
    # ═══════════════ LICENSE KEY CHECK ═══════════════
    if "BABAYAGA" in text.upper() and "-" in text and len(text) > 20:
        # It's a license key - verify it
        status, message, data = verify_license_key(text)
        
        emoji = "✅" if status == "VALID" else "⏰" if status == "EXPIRED" else "❌"
        
        response = f"{emoji} <b>{status}</b>\n\n{message}"
        
        if data and status == "VALID":
            response += f"\n\n👤 Customer: <b>{data.get('customer', 'N/A')}</b>"
            response += f"\n📦 Package: <b>{data.get('package', 'N/A')}</b>"
            response += f"\n📅 Days Left: <b>{data.get('days_left', 0)}</b>"
            response += f"\n⏰ Expires: <b>{data.get('expiry_date', 'N/A')}</b>"
        
        if not is_admin(user.id):
            keyboard = [[InlineKeyboardButton("🔙 Menu", callback_data="cust_back")]]
            if status != "VALID":
                keyboard.insert(0, [InlineKeyboardButton("🛒 Buy License", callback_data="cust_buy")])
            await update.message.reply_text(response, parse_mode='HTML', 
                                           reply_markup=InlineKeyboardMarkup(keyboard))
        else:
            await update.message.reply_text(response, parse_mode='HTML')
        return
    
    # ═══════════════ CUSTOMER SUPPORT MESSAGE ═══════════════
    if not is_admin(user.id):
        # Save customer message
        msg_id = save_support_message(
            user.id,
            user.first_name or 'Unknown',
            user.username or '',
            text,
            'text',
            'incoming'
        )
        
        # Forward to admin
        user_link = f"@{user.username}" if user.username else f"ID: {user.id}"
        admin_msg = f"""
📩 <b>NEW SUPPORT MESSAGE!</b>

👤 <b>From:</b> {user.first_name or 'Unknown'} ({user_link})
🆔 <b>User ID:</b> <code>{user.id}</code>

💬 <b>Message:</b>
{text}

━━━━━━━━━━━━━━━━━━━━━
Reply with: /reply {user.id} Your message
Or click: /chat {user.id}
"""
        
        try:
            keyboard = [
                [InlineKeyboardButton(f"💬 Reply to {user.first_name}", callback_data=f"support_reply_{user.id}")],
                [InlineKeyboardButton("📋 View Chat History", callback_data=f"support_history_{user.id}")]
            ]
            await context.bot.send_message(
                chat_id=ADMIN_ID, 
                text=admin_msg, 
                parse_mode='HTML',
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
        except Exception as e:
            logger.error(f"Failed to forward to admin: {e}")
        
        # Auto-reply to customer - Beautiful Professional Message (English + Bangla)
        user_name = user.first_name or 'Valued Customer'
        current_time = datetime.now().strftime("%I:%M %p")
        
        auto_reply_msg = f"""
✨ <b>Thank You for Contacting Us!</b> ✨
✨ <b>যোগাযোগ করার জন্য ধন্যবাদ!</b> ✨
━━━━━━━━━━━━━━━━━━━━━━━━━━━

👋 Dear / প্রিয় <b>{user_name}</b>,

🇬🇧 <b>EN:</b> Your message has been received successfully!
🇧🇩 <b>বাং:</b> আপনার message সফলভাবে পৌঁছে গেছে!

📩 <b>Status:</b> Received / প্রাপ্ত ✅
🕐 <b>Time / সময়:</b> {current_time}

━━━━━━━━━━━━━━━━━━━━━━━━━━━
🎯 <b>What Happens Next? / এরপর কি হবে?</b>

🇬🇧 Our Admin/Support Team will review your
message and reply very soon!

🇧🇩 আমাদের Admin/Support Team আপনার 
message রিভিউ করে শীঘ্রই reply দিবে!

━━━━━━━━━━━━━━━━━━━━━━━━━━━
⏱️ <b>Expected Reply Time / প্রত্যাশিত সময়:</b>

• Usually within 1-2 hours
  (সাধারণত ১-২ ঘণ্টার মধ্যে)

• Maximum 24 hours
  (সর্বোচ্চ ২৪ ঘণ্টা)

━━━━━━━━━━━━━━━━━━━━━━━━━━━
📞 <b>Need Urgent Help? / জরুরি সাহায্য?</b>

🇬🇧 Feel free to send another message if urgent.
We'll try to respond as quickly as possible!

🇧🇩 জরুরি হলে আবার message করতে পারেন।
আমরা যত দ্রুত সম্ভব respond করব!

━━━━━━━━━━━━━━━━━━━━━━━━━━━
🙏 <b>Thank you for contacting us!</b>
🙏 <b>যোগাযোগ করার জন্য ধন্যবাদ!</b>

<b>— BabaYaga Trading Bot Team</b>
"""
        
        keyboard = [
            [InlineKeyboardButton("💰 Prices / দাম", callback_data="cust_prices")],
            [InlineKeyboardButton("🛒 Buy / কিনুন", callback_data="cust_buy")],
            [InlineKeyboardButton("🔑 Check License / চেক", callback_data="cust_verify")],
            [InlineKeyboardButton("🏠 Main Menu / মেনু", callback_data="cust_back")]
        ]
        await update.message.reply_text(
            auto_reply_msg,
            parse_mode='HTML',
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

# ─────────────────────────────────────────────────────────────────────────────
# ADMIN SUPPORT COMMANDS
# ─────────────────────────────────────────────────────────────────────────────

async def reply_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Reply to a customer: /reply <user_id> <message>"""
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Admin only command.")
        return
    
    if len(context.args) < 2:
        await update.message.reply_text(
            "<b>Usage:</b> /reply <code>user_id</code> <code>message</code>\n\n"
            "<b>Example:</b>\n/reply 123456789 Hello! How can I help?",
            parse_mode='HTML'
        )
        return
    
    try:
        target_user_id = int(context.args[0])
        message_text = ' '.join(context.args[1:])
        
        # Send message to customer
        await context.bot.send_message(
            chat_id=target_user_id,
            text=f"📩 <b>Support Reply:</b>\n\n{message_text}",
            parse_mode='HTML'
        )
        
        # Save to database
        customer_info = get_customer_by_id(target_user_id)
        save_support_message(
            target_user_id,
            customer_info.get('name', 'Customer') if customer_info else 'Customer',
            customer_info.get('username', '') if customer_info else '',
            message_text, 'text', 'outgoing'
        )
        
        await update.message.reply_text(f"✅ Message sent to user <code>{target_user_id}</code>!", parse_mode='HTML')
        
    except ValueError:
        await update.message.reply_text("❌ Invalid user ID. Must be a number.")
    except Exception as e:
        await update.message.reply_text(f"❌ Failed to send: {str(e)}")

async def chat_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Enter chat mode with a customer: /chat <user_id>"""
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Admin only command.")
        return
    
    if len(context.args) < 1:
        await update.message.reply_text(
            "<b>Usage:</b> /chat <code>user_id</code>\n\n"
            "This enters reply mode. Your next message will be sent to that customer.",
            parse_mode='HTML'
        )
        return
    
    try:
        target_user_id = int(context.args[0])
        customer_info = get_customer_by_id(target_user_id)
        
        # Set reply mode
        context.user_data['reply_to_user'] = target_user_id
        context.user_data['reply_to_info'] = customer_info or {'name': 'Customer'}
        
        # Show chat history
        history = get_chat_history(target_user_id, 10)
        
        msg = f"💬 <b>Chat with {customer_info.get('name', 'Customer') if customer_info else f'User {target_user_id}'}</b>\n\n"
        
        if history:
            msg += "<b>Recent Messages:</b>\n"
            for chat in history:
                direction = "👤" if chat['direction'] == 'incoming' else "📤"
                time = chat['timestamp'][11:16]  # HH:MM
                msg += f"{direction} [{time}] {chat['message'][:50]}{'...' if len(chat['message']) > 50 else ''}\n"
        
        msg += "\n━━━━━━━━━━━━━━━━━━━━━\n"
        msg += "✍️ <b>Reply Mode Active!</b>\n"
        msg += "এখন যা লিখবেন সেটা সরাসরি customer কে পাঠানো হবে।\n\n"
        msg += "Cancel করতে: /endchat"
        
        # Mark messages as read
        mark_messages_read(target_user_id)
        
        keyboard = [[InlineKeyboardButton("❌ End Chat", callback_data="support_endchat")]]
        await update.message.reply_text(msg, parse_mode='HTML', reply_markup=InlineKeyboardMarkup(keyboard))
        
    except ValueError:
        await update.message.reply_text("❌ Invalid user ID. Must be a number.")

async def endchat_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """End chat/reply mode: /endchat"""
    if not is_admin(update.effective_user.id):
        return
    
    context.user_data['reply_to_user'] = None
    context.user_data['reply_to_info'] = None
    await update.message.reply_text("✅ Chat mode ended. Normal mode active.")

async def inbox_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """View support inbox: /inbox"""
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Admin only command.")
        return
    
    conversations = get_support_conversations()
    
    if not conversations:
        await update.message.reply_text("📭 Support inbox is empty. No messages yet!")
        return
    
    msg = "📬 <b>Support Inbox</b>\n\n"
    
    for conv in conversations:
        unread = conv['unread']
        unread_badge = f"🔴 {unread}" if unread > 0 else "✅"
        name = conv['user_name'] or 'Unknown'
        username = f"@{conv['username']}" if conv['username'] else f"ID:{conv['user_id']}"
        last_time = conv['last_message'][11:16] if conv['last_message'] else 'N/A'
        
        msg += f"{unread_badge} <b>{name}</b> ({username})\n"
        msg += f"   📝 {conv['total_messages']} msgs | Last: {last_time}\n"
        msg += f"   /chat {conv['user_id']}\n\n"
    
    keyboard = [[InlineKeyboardButton("🔄 Refresh", callback_data="support_inbox")]]
    await update.message.reply_text(msg, parse_mode='HTML', reply_markup=InlineKeyboardMarkup(keyboard))

async def support_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle support-related button callbacks"""
    query = update.callback_query
    await query.answer()
    
    if not is_admin(query.from_user.id):
        return
    
    data = query.data
    
    if data.startswith("support_reply_"):
        # Enter reply mode for a user
        user_id = int(data.replace("support_reply_", ""))
        customer_info = get_customer_by_id(user_id)
        
        context.user_data['reply_to_user'] = user_id
        context.user_data['reply_to_info'] = customer_info or {'name': 'Customer'}
        
        await query.edit_message_text(
            f"✍️ <b>Reply Mode Active!</b>\n\n"
            f"Replying to: <b>{customer_info.get('name', 'Customer') if customer_info else f'User {user_id}'}</b>\n\n"
            f"এখন message লিখে send করুন।\n"
            f"Cancel করতে: /endchat",
            parse_mode='HTML'
        )
    
    elif data.startswith("support_history_"):
        user_id = int(data.replace("support_history_", ""))
        history = get_chat_history(user_id, 15)
        customer_info = get_customer_by_id(user_id)
        
        msg = f"📜 <b>Chat History - {customer_info.get('name', 'Customer') if customer_info else f'User {user_id}'}</b>\n\n"
        
        if not history:
            msg += "No messages yet."
        else:
            for chat in history:
                direction = "👤" if chat['direction'] == 'incoming' else "📤"
                time = chat['timestamp'][5:16]  # MM-DD HH:MM
                msg += f"{direction} [{time}]\n{chat['message']}\n\n"
        
        keyboard = [
            [InlineKeyboardButton(f"💬 Reply", callback_data=f"support_reply_{user_id}")],
            [InlineKeyboardButton("🔙 Back to Inbox", callback_data="support_inbox")]
        ]
        
        # Mark as read
        mark_messages_read(user_id)
        
        await query.edit_message_text(msg, parse_mode='HTML', reply_markup=InlineKeyboardMarkup(keyboard))
    
    elif data == "support_inbox":
        conversations = get_support_conversations()
        
        if not conversations:
            await query.edit_message_text("📭 Support inbox is empty!")
            return
        
        msg = "📬 <b>Support Inbox</b>\n\n"
        
        for conv in conversations:
            unread = conv['unread']
            unread_badge = f"🔴 {unread}" if unread > 0 else "✅"
            name = conv['user_name'] or 'Unknown'
            
            msg += f"{unread_badge} <b>{name}</b>\n"
            msg += f"   /chat {conv['user_id']}\n\n"
        
        keyboard = [[InlineKeyboardButton("🔄 Refresh", callback_data="support_inbox")]]
        await query.edit_message_text(msg, parse_mode='HTML', reply_markup=InlineKeyboardMarkup(keyboard))
    
    elif data == "support_endchat":
        context.user_data['reply_to_user'] = None
        context.user_data['reply_to_info'] = None
        await query.edit_message_text("✅ Chat mode ended.")


# ═══════════════════════════════════════════════════════════════════════════════
# FLASK HTTP API FOR TRADING BOT VERIFICATION
# ═══════════════════════════════════════════════════════════════════════════════

flask_app = Flask(__name__)

@flask_app.route("/")
def home():
    """Health check / status page"""
    health = check_db_health()
    db_color = "#00ff88" if health.get("ok") else "#ff5555"
    db_label = "UP" if health.get("ok") else "DOWN"
    try:
        stats = get_statistics() if health.get("ok") else {}
    except Exception:
        stats = {}
    active = stats.get("active", 0) if stats else 0
    return f"""
    <html>
    <head><title>BabaYaga License Server</title></head>
    <body style="background:#1a1a2e;color:#fff;font-family:Arial;display:flex;justify-content:center;align-items:center;min-height:100vh;margin:0;">
    <div style="text-align:center;padding:40px;background:#16213e;border-radius:20px;max-width:500px;">
        <h1 style="color:#00ff88;">🔐 BabaYaga License Server</h1>
        <p style="color:#00ff88;font-size:24px;">✅ Server is ONLINE!</p>
        <p style="color:{db_color};font-size:18px;">DB: <b>{db_label}</b> ({health.get('backend', '?')})</p>
        <p style="color:#aaa;font-size:12px;">{health.get('message', '')}</p>
        <hr style="border-color:#333;margin:20px 0;">
        <p>📊 Active Licenses: <b>{active}</b></p>
        <p>📅 Last Updated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</p>
        <hr style="border-color:#333;margin:20px 0;">
        <p style="color:#888;font-size:12px;">API: /api/verify?key=YOUR_KEY · /api/health</p>
    </div>
    </body>
    </html>
    """

@flask_app.route("/api/health")
def api_health():
    """JSON health probe — confirms Neon/Postgres connectivity."""
    health = check_db_health()
    code = 200 if health.get("ok") else 503
    return jsonify(health), code

@flask_app.route("/api/verify")
def api_verify():
    """
    HTTP API for license verification
    Used by Trading Bot to verify licenses
    
    Query params:
        - key: License key (required)
        - device_id: Device identifier (optional)
    
    Returns JSON:
        {
            "status": "VALID" | "INVALID" | "EXPIRED" | "ERROR",
            "message": "Human readable message",
            "data": { ... additional data ... }
        }
    """
    key = request.args.get('key', '').strip()
    device_id = request.args.get('device_id', '')
    ip_address = request.remote_addr
    
    if not key:
        return jsonify({
            "status": "ERROR",
            "message": "Missing 'key' parameter",
            "data": {}
        }), 400
    
    # Fail fast with clear message if DB is down (e.g. dead Render Postgres host)
    health = check_db_health()
    if not health.get("ok"):
        return jsonify({
            "status": "ERROR",
            "message": health.get("message", "Database unavailable"),
            "data": {"db": "down"}
        })
    
    status, message, data = verify_license_key(key, device_id, ip_address)
    
    return jsonify({
        "status": status,
        "message": message,
        "data": data
    })

@flask_app.route("/api/stats")
def api_stats():
    """Get license statistics (admin only - requires secret)"""
    secret = request.args.get('secret', '')
    if secret != str(ADMIN_ID):
        return jsonify({"error": "Unauthorized"}), 401
    
    health = check_db_health()
    if not health.get("ok"):
        return jsonify({"error": health.get("message", "Database unavailable"), "db": "down"}), 503
    
    return jsonify(get_statistics())

def run_flask():
    """Run Flask server"""
    port = int(os.environ.get('PORT', 10000))
    flask_app.run(host="0.0.0.0", port=port, threaded=True)

# ═══════════════════════════════════════════════════════════════════════════════
# MAIN - START BOT
# ═══════════════════════════════════════════════════════════════════════════════

def main():
    """Main function to start bot"""
    print("=" * 60)
    print("🔐 BabaYaga License Bot v2.0 Starting...")
    print("=" * 60)
    
    try:
        # Start Flask HTTP server in background thread
        flask_thread = threading.Thread(target=run_flask, daemon=True)
        flask_thread.start()
        print("✅ HTTP API Server started")
        
        # Check configuration
        if not BOT_TOKEN:
            logger.error("❌ ERROR: TOKEN not set!")
            print("Set TOKEN environment variable")
            while True:
                threading.Event().wait(60)
        
        if ADMIN_ID == 0:
            logger.error("❌ ERROR: ADMIN_ID not set!")
            print("Set ADMIN_ID environment variable")
            while True:
                threading.Event().wait(60)
        
        print(f"📱 Bot Token: {BOT_TOKEN[:15]}...")
        print(f"👤 Admin ID: {ADMIN_ID}")
        print(f"🗄  DB backend: {'postgres/Neon' if USE_POSTGRES else 'sqlite'}")
        if not USE_POSTGRES:
            print("⚠️ DATABASE_URL missing — using SQLite (data lost on Render restart).")
            print("   Set Render Environment DATABASE_URL to your Neon connection string.")
        
        # Initialize database (clear error if DATABASE_URL is dead)
        try:
            init_database()
            health = check_db_health()
            if health.get("ok"):
                print("✅ Database initialized and reachable")
            else:
                print(f"⚠️ Database init finished but health check failed: {health.get('message')}")
        except Exception as db_err:
            print(f"❌ Database init failed: {_friendly_db_error(db_err)}")
            print("   Fix: set Neon DATABASE_URL on Render, then redeploy.")
            # Keep Telegram + HTTP up so /api/health can report the problem

        
        # Post init callback to set menu commands
        async def post_init(application):
            await application.bot.set_my_commands([
                ("start", "🏠 Main Menu / মূল মেনু"),
                ("help", "💬 Help & Support / সাহায্য"),
                ("prices", "💰 License Prices / দাম"),
                ("buy", "🛒 Buy License / কিনুন"),
                ("check", "🔑 Check License / চেক করুন"),
            ])
            print("✅ Bot menu commands set successfully!")
        
        # Create bot application with post_init
        # PTB 21.x on Render: build Application after HTTP thread is up
        app = (
            Application.builder()
            .token(BOT_TOKEN)
            .post_init(post_init)
            .connect_timeout(30.0)
            .read_timeout(30.0)
            .build()
        )
        
        # Register handlers
        app.add_handler(CommandHandler("start", start_command))
        app.add_handler(CommandHandler("help", help_command))
        app.add_handler(CommandHandler("prices", prices_command))
        app.add_handler(CommandHandler("buy", buy_command))
        app.add_handler(CommandHandler("add", add_command))
        app.add_handler(CommandHandler("info", info_command))
        app.add_handler(CommandHandler("check", check_command))
        app.add_handler(CommandHandler("search", search_command))
        app.add_handler(CommandHandler("list", list_command))
        app.add_handler(CommandHandler("active", active_command))
        app.add_handler(CommandHandler("expired", expired_command))
        app.add_handler(CommandHandler("stats", stats_command))
        app.add_handler(CommandHandler("deactivate", deactivate_command))
        app.add_handler(CommandHandler("reactivate", reactivate_command))
        app.add_handler(CommandHandler("extend", extend_command))
        app.add_handler(CommandHandler("delete", delete_command))
        app.add_handler(CommandHandler("note", note_command))
        app.add_handler(CommandHandler("devices", devices_command))
        app.add_handler(CommandHandler("logs", logs_command))
        app.add_handler(CommandHandler("expiring", expiring_command))
        
        # Support message handlers
        app.add_handler(CommandHandler("reply", reply_command))
        app.add_handler(CommandHandler("chat", chat_command))
        app.add_handler(CommandHandler("endchat", endchat_command))
        app.add_handler(CommandHandler("inbox", inbox_command))
        
        app.add_handler(CallbackQueryHandler(support_callback, pattern="^support_"))
        app.add_handler(CallbackQueryHandler(button_handler))
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
        
        print("✅ All handlers registered")
        
        print("=" * 60)
        print("🚀 Bot is now running!")
        print("=" * 60)
        
        # Start polling
        app.run_polling(drop_pending_updates=True)
        
    except Exception as e:
        logger.error(f"❌ Bot crashed: {e}")
        import traceback
        traceback.print_exc()
        while True:
            threading.Event().wait(60)

if __name__ == "__main__":
    main()
