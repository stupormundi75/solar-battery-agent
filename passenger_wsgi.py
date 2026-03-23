"""
passenger_wsgi.py
=================
Phusion Passenger entry point for simply.com shared hosting.

This file must live in the domain/subdomain root that Passenger watches
(the folder you set as PassengerAppRoot in .htaccess).

Flask and dependencies were installed with:
  python3 -m pip install --user flask requests backports.zoneinfo

So we use the system Python 3 and rely on ~/.local for packages.
No virtualenv needed.
"""
import sys
import os

# Use the system Python 3 (packages installed via --user)
INTERP = "/usr/bin/python3"
if sys.executable != INTERP:
    os.execl(INTERP, INTERP, *sys.argv)

# Add the app directory to the Python path
APP_DIR = os.path.expanduser("~/solar-agent")
if APP_DIR not in sys.path:
    sys.path.insert(0, APP_DIR)

# Make sure --user installed packages are on the path
import site
user_site = site.getusersitepackages()
if user_site not in sys.path:
    sys.path.insert(0, user_site)

# Import the Flask app — 'application' is the magic name Passenger looks for
from app import application
