"""
Frontend Blueprint for Blue Stream Water Business Platform
Handles all frontend routes and UI rendering
"""
from flask import Blueprint

frontend_bp = Blueprint('frontend', __name__, template_folder='../templates', static_folder='../static')

from . import routes