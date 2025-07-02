import os

from flask import Flask
from config import Config
from app.extensions import db as db_extension


def create_app(config_class=Config):
    # create and configure the app
    app = Flask(__name__, instance_relative_config=True)
    app.config.from_object(config_class)

    db_extension.init_app(app)

    from . import main, db, auth, landing
    db.init_app(app)
    app.register_blueprint(main.bp)
    app.register_blueprint(auth.bp)
    app.register_blueprint(landing.bp)

    return app