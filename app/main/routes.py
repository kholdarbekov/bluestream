from flask import render_template
from app.main import bp


@bp.route('/')
def index():
    return render_template('main/index-4.html')