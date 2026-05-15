#!/usr/bin/env python3
"""DashPi — main Flask application entry point.

Initializes the display, config, plugin system, and refresh task, then serves
the web UI via Waitress. Supports --dev mode for local development on port 8080.
"""

# set up logging
import os, logging.config

logging.config.fileConfig(os.path.join(os.path.dirname(__file__), 'config', 'logging.conf'))

import logging
import argparse
import socket
import warnings

# Suppress noisy warning from Inky e-paper library (harmless on non-Inky hardware)
warnings.filterwarnings("ignore", message=".*Busy Wait: Held high.*")

from utils.app_utils import generate_startup_image
from utils.wifi_manager import WifiManager
from flask import Flask
from config import Config
from display.display_manager import DisplayManager
from refresh_task import RefreshTask
from blueprints.main import main_bp
from blueprints.settings import settings_bp
from blueprints.plugin import plugin_bp
from blueprints.apikeys import apikeys_bp
from blueprints.loops import loops_bp
from blueprints.wifi import wifi_bp
from blueprints.auth import auth_bp
from plugins.pluginmanager.api import plugin_manage_bp
from jinja2 import ChoiceLoader, FileSystemLoader
from plugins.plugin_registry import load_plugins
from waitress import serve


logger = logging.getLogger(__name__)

# Parse command line arguments
parser = argparse.ArgumentParser(description='DashPi Display Server')
parser.add_argument('--dev', action='store_true', help='Run in development mode')
args = parser.parse_args()

# Set development mode settings
if args.dev:
    Config.config_file = os.path.join(Config.BASE_DIR, "config", "device_dev.json")
    DEV_MODE = True
    PORT = 8080
    logger.info("Starting in DEVELOPMENT mode on port 8080")
else:
    DEV_MODE = False
    PORT = 80
    logger.info("Starting in PRODUCTION mode on port 80")
logging.getLogger('waitress.queue').setLevel(logging.ERROR)
app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 64 * 1024 * 1024  # 64MB upload limit (config backups with images)
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
app.config['SESSION_COOKIE_HTTPONLY'] = True

@app.before_request
def require_login():
    """Global request interceptor for authentication and CSRF protection."""
    # 1. CSRF Protection (Origin/Referer validation for mutations)
    if request.method in ['POST', 'PUT', 'DELETE']:
        origin = request.headers.get('Origin')
        referer = request.headers.get('Referer')
        target = f"{request.scheme}://{request.host}"
        
        # Check Origin first, fall back to Referer
        if origin:
            if origin != target:
                logger.warning(f"CSRF Blocked: Origin mismatch ({origin} != {target})")
                return "CSRF validation failed", 403
        elif referer:
            if not referer.startswith(target):
                logger.warning(f"CSRF Blocked: Referer mismatch ({referer} not starting with {target})")
                return "CSRF validation failed", 403
        else:
            # Most modern browsers send Origin for POST. If both missing, it's suspicious.
            logger.warning(f"CSRF Blocked: Missing Origin/Referer for {request.method} request")
            return "CSRF validation failed", 403

    # 2. Authentication Protection
    # Define public routes that don't require login
    public_endpoints = [
        'auth.login', 'auth.setup_password', 'static', 
        'wifi.wifi_portal', 'wifi.wifi_scan', 'wifi.wifi_connect', 
        'wifi.wifi_status', 'wifi.captive_android', 'wifi.captive_apple', 
        'wifi.captive_windows'
    ]
    
    # Also allow viewing the display and current image without login
    public_endpoints.extend(['main.display_page', 'main.get_current_image'])

    if request.endpoint in public_endpoints or not request.endpoint:
        return

    # If password not set yet, redirect to setup (handled in auth blueprint too but good to have here)
    if not device_config.has_password():
        if request.endpoint != 'auth.setup_password':
            return redirect(url_for('auth.setup_password'))
        return

    # Check if authenticated
    if not session.get('authenticated'):
        return redirect(url_for('auth.login', next=request.url))

template_dirs = [
   os.path.join(os.path.dirname(__file__), "templates"),    # Default template folder
   os.path.join(os.path.dirname(__file__), "plugins"),      # Plugin templates
]
app.jinja_loader = ChoiceLoader([FileSystemLoader(directory) for directory in template_dirs])

device_config = Config()
display_manager = DisplayManager(device_config)
wifi_manager = WifiManager()
refresh_task = RefreshTask(device_config, display_manager, wifi_manager)

load_plugins(device_config.get_plugins())

# Determine the device name: config > hostname > "DashPi"
_device_name = device_config.get_config("device_name", default="")
if not _device_name:
    _device_name = socket.gethostname() or "DashPi"
    # Strip .local suffix if present (mDNS adds it automatically)
    if _device_name.endswith(".local"):
        _device_name = _device_name[:-6]
    device_config.update_value("device_name", _device_name, write=True)

# Store dependencies
app.config['DEVICE_CONFIG'] = device_config
app.config['DISPLAY_MANAGER'] = display_manager
app.config['REFRESH_TASK'] = refresh_task
app.config['WIFI_MANAGER'] = wifi_manager

# Set additional parameters
app.config['MAX_FORM_PARTS'] = 10_000

# Register Blueprints
app.register_blueprint(main_bp)
app.register_blueprint(settings_bp)
app.register_blueprint(plugin_bp)
app.register_blueprint(apikeys_bp)
app.register_blueprint(loops_bp)
app.register_blueprint(wifi_bp)
app.register_blueprint(auth_bp)
app.register_blueprint(plugin_manage_bp)

# Inject project_name and version into all templates
@app.context_processor
def inject_globals():
    from blueprints.main import get_version
    device_name = device_config.get_config("device_name", default="DashPi")
    return dict(project_name="DashPi", device_name=device_name, version=get_version())

# Security headers
@app.after_request
def set_security_headers(response):
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['X-Frame-Options'] = 'DENY'
    response.headers['Referrer-Policy'] = 'strict-origin-when-cross-origin'
    response.headers['Content-Security-Policy'] = (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline' https://unpkg.com; "
        "style-src 'self' 'unsafe-inline' https://unpkg.com; "
        "img-src 'self' data: https:; "
        "font-src 'self'; "
        "connect-src 'self'"
    )
    return response

# Register opener for HEIF/HEIC images
try:
    from pi_heif import register_heif_opener
    register_heif_opener()
except ImportError:
    logger.debug("pi_heif not available, HEIF/HEIC support disabled")

if __name__ == '__main__':

    # start the background refresh task
    refresh_task.start()

    # display startup image on first boot
    if device_config.get_config("startup") is True:
        logger.info("Startup flag is set, displaying startup image")
        if wifi_manager.check_connectivity():
            img = generate_startup_image(device_config.get_resolution())
            display_manager.display_image(img)
        else:
            # No WiFi — enter AP mode and show setup screen
            logger.info("No WiFi at startup, entering AP mode")
            from utils.wifi_display import generate_wifi_setup_image
            device_name = device_config.get_config("device_name", default="DashPi")
            ap_ssid = wifi_manager.get_ap_ssid(device_name)
            wifi_manager.start_ap_mode(device_name)
            portal_url = f"http://{wifi_manager.get_hotspot_ip()}/wifi"
            img = generate_wifi_setup_image(
                device_config.get_resolution(), ap_ssid, portal_url,
                password=wifi_manager.get_ap_password()
            )
            display_manager.display_image(img)
        device_config.update_value("startup", False, write=True)
    elif not wifi_manager.check_connectivity():
        # Not first boot, but no WiFi — enter AP mode
        logger.info("No WiFi detected, entering AP mode")
        from utils.wifi_display import generate_wifi_setup_image
        device_name = device_config.get_config("device_name", default="DashPi")
        ap_ssid = wifi_manager.get_ap_ssid(device_name)
        wifi_manager.start_ap_mode(device_name)
        portal_url = f"http://{wifi_manager.get_hotspot_ip()}/wifi"
        img = generate_wifi_setup_image(
            device_config.get_resolution(), ap_ssid, portal_url,
            password=wifi_manager.get_ap_password()
        )
        display_manager.display_image(img)

    try:
        # Run the Flask app
        app.secret_key = os.urandom(24).hex()

        # Get local IP address for display (only in dev mode when running on non-Pi)
        if DEV_MODE:
            try:
                s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                s.connect(("8.8.8.8", 80))
                local_ip = s.getsockname()[0]
                s.close()
                logger.info(f"Serving on http://{local_ip}:{PORT}")
            except (OSError, socket.error):
                pass  # Ignore if we can't get the IP

        serve(app, host="0.0.0.0", port=PORT, threads=2)
    finally:
        refresh_task.stop()
        # Clean up HTTP session connection pool
        from utils.http_client import close_http_session
        close_http_session()
