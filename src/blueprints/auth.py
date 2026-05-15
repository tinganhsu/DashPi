"""Authentication blueprint — handle login, logout, and initial password setup."""

import logging
from flask import Blueprint, request, jsonify, render_template, redirect, url_for, session, current_app

logger = logging.getLogger(__name__)
auth_bp = Blueprint("auth", __name__)

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
            next_url = request.args.get('next') or url_for('main.main_page')
            return redirect(next_url)
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
