"""Authentication blueprint — handle login, logout, and initial password setup."""

import logging
from urllib.parse import urlsplit
from flask import Blueprint, request, jsonify, render_template, redirect, url_for, session, current_app

logger = logging.getLogger(__name__)
auth_bp = Blueprint("auth", __name__)


def _safe_next_url(target):
    """Return ``target`` only if it leads back into this app, else the dashboard.

    The access-control gate sends the absolute request URL, so a same-host
    absolute URL is accepted alongside a plain path. Browsers read a
    backslash as a slash and drop tabs and newlines, so those are refused
    rather than letting "/\\evil" or a slash-tab-slash become "//evil".
    """
    fallback = url_for('main.main_page')
    if not target or '\\' in target or any(ord(c) < 0x21 for c in target):
        return fallback
    parts = urlsplit(target)
    if not parts.scheme and not parts.netloc:
        return target if target.startswith('/') and not target.startswith('//') else fallback
    if parts.scheme in ('http', 'https') and parts.netloc == request.host:
        return target
    return fallback

@auth_bp.route('/login', methods=['GET', 'POST'])
def login():
    """Render login page and handle password submission."""
    device_config = current_app.config['DEVICE_CONFIG']
    
    # If no password set, redirect to setup
    if not device_config.has_password():
        return redirect(url_for('auth.setup_password'))
        
    if request.method == 'POST':
        password = request.form.get('password')
        if device_config.check_password(password):
            session['authenticated'] = True
            session.permanent = True
            # Redirect to next page or dashboard
            return redirect(_safe_next_url(request.args.get('next')))
        else:
            return render_template('login.html', error="Invalid password")
            
    return render_template('login.html')

@auth_bp.route('/logout')
def logout():
    """Clear session and log out."""
    session.pop('authenticated', None)
    return redirect(url_for('auth.login'))

@auth_bp.route('/setup_password', methods=['GET', 'POST'])
def setup_password():
    """Initial password setup for first-time use."""
    device_config = current_app.config['DEVICE_CONFIG']
    
    # If password already set, redirect to login
    if device_config.has_password():
        return redirect(url_for('auth.login'))
        
    if request.method == 'POST':
        password = request.form.get('password')
        confirm = request.form.get('confirm')
        
        if not password or len(password) < 4:
            return render_template('setup_password.html', error="Password must be at least 4 characters")
            
        if password != confirm:
            return render_template('setup_password.html', error="Passwords do not match")
            
        device_config.set_password(password)
        session['authenticated'] = True
        session.permanent = True
        return redirect(url_for('main.main_page'))
        
    return render_template('setup_password.html')

@auth_bp.route('/change_password', methods=['POST'])
def change_password():
    """Change admin password. Requires current password."""
    if not session.get('authenticated'):
        return jsonify({"error": "Unauthorized"}), 401
        
    device_config = current_app.config['DEVICE_CONFIG']
    data = request.get_json() or {}
    
    current_pwd = data.get('current_password')
    new_pwd = data.get('new_password')
    confirm_pwd = data.get('confirm_password')
    
    if not current_pwd or not new_pwd or not confirm_pwd:
        return jsonify({"error": "Missing required fields"}), 400
        
    if not device_config.check_password(current_pwd):
        return jsonify({"error": "Incorrect current password"}), 400
        
    if new_pwd != confirm_pwd:
        return jsonify({"error": "New passwords do not match"}), 400
        
    if len(new_pwd) < 4:
        return jsonify({"error": "New password must be at least 4 characters"}), 400
        
    device_config.set_password(new_pwd)
    return jsonify({"success": True, "message": "Password updated successfully"})
