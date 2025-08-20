"""
Configuration management CLI commands
"""
import os
import shutil
import click
from flask import current_app
from flask.cli import with_appcontext

from business_app.config import (
    get_config, 
    validate_environment, 
    get_environment_info,
    config
)


@click.group()
def config_cli():
    """Configuration management commands"""
    pass


@config_cli.command()
@click.option('--detailed', '-d', is_flag=True, help='Show detailed configuration')
@with_appcontext
def info(detailed):
    """Show current environment configuration information"""
    env_info = get_environment_info()
    config_obj = get_config()
    
    click.echo(f"Environment: {click.style(env_info['environment'], fg='green', bold=True)}")
    click.echo(f"Config Class: {env_info['config_class']}")
    click.echo(f"Debug Mode: {click.style(str(env_info['debug']), fg='yellow' if env_info['debug'] else 'green')}")
    click.echo(f"Testing Mode: {env_info['testing']}")
    
    if detailed:
        click.echo("\nConfiguration Details:")
        click.echo(f"  Database URI: {env_info['database_uri'][:50]}..." if len(env_info['database_uri']) > 50 else f"  Database URI: {env_info['database_uri']}")
        click.echo(f"  Redis URL: {env_info['redis_url'][:50]}..." if len(env_info['redis_url']) > 50 else f"  Redis URL: {env_info['redis_url']}")
        click.echo(f"  Secret Key Set: {env_info['secret_key_set']}")
        click.echo(f"  JWT Secret Set: {env_info['jwt_secret_set']}")
        
        if hasattr(config_obj, 'CORS_ORIGINS'):
            click.echo(f"  CORS Origins: {', '.join(config_obj.CORS_ORIGINS)}")
        
        if hasattr(config_obj, 'STORAGE_TYPE'):
            click.echo(f"  Storage Type: {config_obj.STORAGE_TYPE}")
        
        if hasattr(config_obj, 'FEATURE_FLAGS'):
            click.echo("  Feature Flags:")
            for flag, value in config_obj.FEATURE_FLAGS.items():
                click.echo(f"    {flag}: {value}")


@config_cli.command()
@click.option('--detailed', '-d', is_flag=True, help='Show detailed validation results')
@click.option('--suggestions', '-s', is_flag=True, help='Show suggestions for fixing issues')
@with_appcontext
def validate(detailed, suggestions):
    """Validate current environment configuration"""
    from business_app.utils.env_validator import EnvironmentValidator
    
    click.echo("Validating environment configuration...")
    
    # Basic configuration validation
    is_valid, message = validate_environment()
    
    # Detailed environment validation
    env = os.environ.get('FLASK_ENV', 'development')
    validator = EnvironmentValidator(env)
    env_valid, errors, warnings = validator.validate_all()
    
    # Show results
    if is_valid and env_valid:
        click.echo(click.style("✓ Configuration is valid", fg='green'))
        if detailed:
            click.echo(f"  Environment: {env}")
            click.echo(f"  Basic validation: {message}")
    else:
        click.echo(click.style("✗ Configuration is invalid", fg='red'))
        if not is_valid:
            click.echo(f"  Basic validation: {message}")
    
    # Show detailed results
    if detailed or errors or warnings:
        if errors:
            click.echo(click.style("\nErrors:", fg='red', bold=True))
            for error in errors:
                click.echo(f"  ✗ {error}")
        
        if warnings:
            click.echo(click.style("\nWarnings:", fg='yellow', bold=True))
            for warning in warnings:
                click.echo(f"  ⚠️  {warning}")
    
    # Show suggestions
    if suggestions and (errors or warnings):
        fixes = validator.suggest_fixes()
        if fixes:
            click.echo(click.style("\nSuggested fixes:", fg='blue', bold=True))
            for fix in fixes:
                click.echo(f"  💡 {fix}")
    
    if not (is_valid and env_valid):
        raise click.ClickException("Configuration validation failed")


@config_cli.command()
@click.argument('environment', type=click.Choice(['development', 'staging', 'production', 'testing']))
@click.option('--force', '-f', is_flag=True, help='Overwrite existing .env file')
def switch(environment, force):
    """Switch to a different environment configuration"""
    env_file = f".env.{environment}"
    target_file = ".env"
    
    if not os.path.exists(env_file):
        raise click.ClickException(f"Environment file {env_file} not found")
    
    if os.path.exists(target_file) and not force:
        if not click.confirm(f"Overwrite existing {target_file}?"):
            click.echo("Operation cancelled")
            return
    
    try:
        shutil.copy2(env_file, target_file)
        click.echo(f"Switched to {click.style(environment, fg='green', bold=True)} environment")
        click.echo(f"Copied {env_file} to {target_file}")
        
        # Show warning for production
        if environment == 'production':
            click.echo(click.style("⚠️  WARNING: You are now using production configuration!", fg='yellow', bold=True))
            click.echo("Make sure to replace placeholder values with actual secrets")
        
    except Exception as e:
        raise click.ClickException(f"Failed to switch environment: {str(e)}")


@config_cli.command()
@click.argument('environment', type=click.Choice(['development', 'staging', 'production', 'testing']))
def check(environment):
    """Check if an environment configuration would be valid"""
    env_file = f".env.{environment}"
    
    if not os.path.exists(env_file):
        raise click.ClickException(f"Environment file {env_file} not found")
    
    # Temporarily set environment variables from file
    original_env = dict(os.environ)
    
    try:
        # Load environment file
        with open(env_file, 'r') as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith('#') and '=' in line:
                    key, value = line.split('=', 1)
                    os.environ[key] = value
        
        # Override the environment variable
        os.environ['FLASK_ENV'] = environment
        
        # Get configuration for this environment
        config_class = config.get(environment, config['default'])
        
        # Validate
        try:
            config_class.validate_required_env_vars()
            config_class.validate_secret_key()
            config_class.validate_debug_mode()
            
            if hasattr(config_class, 'validate_production_settings') and environment == 'production':
                config_class.validate_production_settings()
            elif hasattr(config_class, 'validate_staging_settings') and environment == 'staging':
                config_class.validate_staging_settings()
            
            click.echo(f"✓ {click.style(environment, fg='green')} configuration is valid")
            
        except ValueError as e:
            click.echo(f"✗ {click.style(environment, fg='red')} configuration is invalid: {str(e)}")
            
    finally:
        # Restore original environment
        os.environ.clear()
        os.environ.update(original_env)


@config_cli.command()
def list_envs():
    """List available environment configurations"""
    click.echo("Available environment configurations:")
    
    for env_name in ['development', 'staging', 'production', 'testing']:
        env_file = f".env.{env_name}"
        if os.path.exists(env_file):
            status = click.style("✓", fg='green')
        else:
            status = click.style("✗", fg='red')
        
        click.echo(f"  {status} {env_name} ({env_file})")


@config_cli.command()
@click.argument('key')
@click.argument('environment', type=click.Choice(['development', 'staging', 'production', 'testing']), required=False)
def get_value(key, environment):
    """Get a configuration value from the current or specified environment"""
    if environment:
        # Load from specific environment file
        env_file = f".env.{environment}"
        if not os.path.exists(env_file):
            raise click.ClickException(f"Environment file {env_file} not found")
        
        with open(env_file, 'r') as f:
            for line in f:
                line = line.strip()
                if line.startswith(f"{key}="):
                    value = line.split('=', 1)[1]
                    click.echo(f"{key}={value}")
                    return
        
        click.echo(f"Key '{key}' not found in {env_file}")
    else:
        # Get from current environment
        value = os.environ.get(key)
        if value:
            click.echo(f"{key}={value}")
        else:
            click.echo(f"Key '{key}' not found in current environment")


@config_cli.command()
@click.option('--check-secrets', is_flag=True, help='Check for placeholder secrets that need to be replaced')
def security_check(check_secrets):
    """Perform security checks on the current configuration"""
    config_obj = get_config()
    env = os.environ.get('FLASK_ENV', 'development')
    issues = []
    warnings = []
    
    # Check debug mode in production
    if env == 'production' and getattr(config_obj, 'DEBUG', False):
        issues.append("DEBUG mode is enabled in production")
    
    # Check secure cookies in production/staging
    if env in ['production', 'staging']:
        if not getattr(config_obj, 'SESSION_COOKIE_SECURE', False):
            issues.append("SESSION_COOKIE_SECURE is not enabled")
        if not getattr(config_obj, 'JWT_COOKIE_SECURE', False):
            issues.append("JWT_COOKIE_SECURE is not enabled")
    
    # Check secret key length
    secret_key = getattr(config_obj, 'SECRET_KEY', '')
    if len(secret_key) < 32:
        issues.append("SECRET_KEY is too short (minimum 32 characters)")
    
    # Check for placeholder secrets
    if check_secrets:
        placeholder_patterns = [
            'REPLACE_WITH_',
            'your_',
            'change_me',
            'secret-key-change',
            'dev-secret-key'
        ]
        
        for attr in dir(config_obj):
            if not attr.startswith('_'):
                value = str(getattr(config_obj, attr, ''))
                for pattern in placeholder_patterns:
                    if pattern.lower() in value.lower():
                        warnings.append(f"{attr} appears to contain a placeholder value")
    
    # Report results
    if issues:
        click.echo(click.style("Security Issues Found:", fg='red', bold=True))
        for issue in issues:
            click.echo(f"  ✗ {issue}")
    
    if warnings:
        click.echo(click.style("Warnings:", fg='yellow', bold=True))
        for warning in warnings:
            click.echo(f"  ⚠️  {warning}")
    
    if not issues and not warnings:
        click.echo(click.style("✓ No security issues found", fg='green'))
    
    if issues:
        raise click.ClickException("Security issues detected")


@config_cli.command()
@click.option('--environment', '-e', type=click.Choice(['development', 'staging', 'production', 'testing']),
              help='Check specific environment (default: current)')
def env_check(environment):
    """Comprehensive environment validation check"""
    from business_app.utils.env_validator import EnvironmentValidator, check_security_issues
    
    env = environment or os.environ.get('FLASK_ENV', 'development')
    click.echo(f"Checking environment: {click.style(env, fg='cyan', bold=True)}")
    
    # General validation
    validator = EnvironmentValidator(env)
    is_valid, errors, warnings = validator.validate_all()
    
    # Security check
    security_issues = check_security_issues(env)
    
    # Display results
    if is_valid and not any(security_issues.values()):
        click.echo(click.style("✓ Environment check passed", fg='green', bold=True))
    else:
        click.echo(click.style("✗ Environment check failed", fg='red', bold=True))
    
    # Show errors
    if errors:
        click.echo(click.style("\n🚨 Errors:", fg='red', bold=True))
        for error in errors:
            click.echo(f"  ✗ {error}")
    
    # Show warnings
    if warnings:
        click.echo(click.style("\n⚠️ Warnings:", fg='yellow', bold=True))
        for warning in warnings:
            click.echo(f"  ⚠️  {warning}")
    
    # Show security issues
    for severity, issues in security_issues.items():
        if issues:
            color = {'critical': 'red', 'high': 'yellow', 'medium': 'blue'}[severity]
            click.echo(click.style(f"\n🔒 {severity.title()} Security Issues:", fg=color, bold=True))
            for issue in issues:
                click.echo(f"  🔒 {issue}")
    
    # Show suggestions
    suggestions = validator.suggest_fixes()
    if suggestions:
        click.echo(click.style("\n💡 Suggestions:", fg='blue', bold=True))
        for suggestion in suggestions:
            click.echo(f"  💡 {suggestion}")


@config_cli.command()
@click.option('--environment', '-e', type=click.Choice(['development', 'staging', 'production', 'testing']),
              help='Check for specific environment')
def missing_vars(environment):
    """List missing required environment variables"""
    from business_app.utils.env_validator import get_missing_vars
    
    env = environment or os.environ.get('FLASK_ENV', 'development')
    missing = get_missing_vars(env)
    
    if not missing:
        click.echo(click.style(f"✓ All required variables are set for {env}", fg='green'))
    else:
        click.echo(click.style(f"Missing variables for {env}:", fg='red', bold=True))
        for var in missing:
            click.echo(f"  ✗ {var}")
        
        click.echo(f"\nSet these variables in your .env.{env} file or environment")


@config_cli.command()
@click.argument('var_name')
@click.argument('var_value', required=False)
def validate_var(var_name, var_value):
    """Validate a specific environment variable"""
    from business_app.utils.env_validator import EnvironmentValidator
    
    validator = EnvironmentValidator()
    
    # Use provided value or get from environment
    value = var_value or os.environ.get(var_name)
    
    if not value:
        click.echo(f"Variable {var_name} is not set")
        return
    
    is_valid, errors = validator.validate_specific_var(var_name, value)
    
    if is_valid:
        click.echo(click.style(f"✓ {var_name} is valid", fg='green'))
    else:
        click.echo(click.style(f"✗ {var_name} is invalid:", fg='red'))
        for error in errors:
            click.echo(f"  ✗ {error}")


# Register the CLI group
def init_app(app):
    """Initialize configuration CLI commands"""
    app.cli.add_command(config_cli, name='config')