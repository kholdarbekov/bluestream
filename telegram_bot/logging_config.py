"""
Logging configuration for the Telegram Bot
"""
import logging
import logging.config
import sys
from pathlib import Path

from shared.url_redaction import redact_bot_token, redact_url_credentials

def setup_logging(log_level="INFO", log_to_file=False, log_file_path="bot.log"):
    """
    Setup logging configuration for the bot
    """

    # Create logs directory if it doesn't exist
    if log_to_file:
        log_path = Path(log_file_path)
        log_path.parent.mkdir(parents=True, exist_ok=True)

    # Define logging configuration
    logging_config = {
        'version': 1,
        'disable_existing_loggers': False,
        'formatters': {
            'detailed': {
                'format': '%(asctime)s - %(name)s - %(levelname)s - %(funcName)s:%(lineno)d - %(message)s',
                'datefmt': '%Y-%m-%d %H:%M:%S'
            },
            'simple': {
                'format': '%(asctime)s - %(levelname)s - %(message)s',
                'datefmt': '%Y-%m-%d %H:%M:%S'
            }
        },
        'handlers': {
            'console': {
                'class': 'logging.StreamHandler',
                'level': log_level,
                'formatter': 'detailed',
                'stream': sys.stdout
            }
        },
        'loggers': {
            # Bot modules
            'bot': {
                'level': log_level,
                'handlers': ['console'],
                'propagate': False
            },
            'handlers': {
                'level': log_level,
                'handlers': ['console'],
                'propagate': False
            },
            'api_client': {
                'level': log_level,
                'handlers': ['console'],
                'propagate': False
            },
            'utils': {
                'level': log_level,
                'handlers': ['console'],
                'propagate': False
            },
            'database': {
                'level': log_level,
                'handlers': ['console'],
                'propagate': False
            },
            'config': {
                'level': log_level,
                'handlers': ['console'],
                'propagate': False
            },
            # Telegram library (reduced verbosity)
            'telegram': {
                'level': 'WARNING',
                'handlers': ['console'],
                'propagate': False
            },
            'telegram.ext': {
                'level': 'WARNING',
                'handlers': ['console'],
                'propagate': False
            },
            'httpx': {
                'level': 'WARNING',
                'handlers': ['console'],
                'propagate': False
            },
            'httpcore': {
                'level': 'WARNING',
                'handlers': ['console'],
                'propagate': False
            }
        },
        'root': {
            'level': log_level,
            'handlers': ['console']
        }
    }

    # Add file handler if requested
    if log_to_file:
        logging_config['handlers']['file'] = {
            'class': 'logging.handlers.RotatingFileHandler',
            'level': log_level,
            'formatter': 'detailed',
            'filename': log_file_path,
            'maxBytes': 10485760,  # 10MB
            'backupCount': 5
        }

        # Add file handler to all loggers
        for logger_name in logging_config['loggers']:
            logging_config['loggers'][logger_name]['handlers'].append('file')

        logging_config['root']['handlers'].append('file')

    # Apply logging configuration
    logging.config.dictConfig(logging_config)

    # Set up root logger
    root_logger = logging.getLogger()
    root_logger.setLevel(getattr(logging, log_level.upper()))

    print(f"Logging configured - Level: {log_level}, File: {log_to_file}")

    return root_logger

def log_bot_startup_info():
    """Log important bot startup information"""
    logger = logging.getLogger('bot')

    logger.info("=" * 60)
    logger.info("TELEGRAM BOT STARTING UP")
    logger.info("=" * 60)

    from config import config

    # Log configuration (without sensitive data)
    # Only the bot id, the text before the first ':', is public
    logger.info(f"Bot Token: {redact_bot_token(config.telegram.bot_token)}")
    logger.info(f"Webhook URL: {config.telegram.webhook_url or 'POLLING MODE'}")
    logger.info(f"Business API URL: {config.business_api.base_url}")
    logger.info(f"Database URL: {redact_url_credentials(config.database.url) if config.database.url else 'NOT SET'}")
    logger.info(f"Default Language: {config.localization.default_language}")
    logger.info(f"Supported Languages: {config.localization.supported_languages}")

    logger.info("=" * 60)
