from flask import (
    Blueprint, flash, g, redirect, render_template, request, url_for
)
from werkzeug.exceptions import abort

from core.auth import login_required
from core.db import get_db

bp = Blueprint('landing', __name__)


@bp.route('/')
def index():
    return render_template('landing/index.html')