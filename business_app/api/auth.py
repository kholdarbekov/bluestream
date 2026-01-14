"""
Authentication API routes for the Water Business Platform
This file should be placed in business_app/api/auth.py
"""
import logging
from datetime import datetime, timezone, timedelta
from flask import Blueprint, request, jsonify, current_app
from flask_jwt_extended import jwt_required, get_jwt_identity, get_jwt, set_access_cookies, set_refresh_cookies, unset_jwt_cookies
from flasgger import swag_from
from pydantic import ValidationError as PydanticValidationError

from business_app.utils.service_factory import get_auth_service
from business_app.utils.decorators import (
    require_auth, validate_json, handle_exceptions,
    rate_limit, rate_limit_by_telegram_id, log_request
)
from business_app.middleware import jwt_required_with_refresh
from business_app.utils.validators import phone_validator, email_validator
from business_app.utils.exceptions import ValidationError, UnauthorizedError, ConflictError
from business_app.utils.helpers import get_current_language
from business_app.utils.translations import get_translation
from business_app.utils.constants import UserRole, UserStatus
from business_app.utils.csrf_protection import csrf_required
from business_app.models.user import User
from business_app import db
from business_app.utils.api_responses import (
    success_response,
    error_response,
    created_response,
    not_found_response,
    unauthorized_response,
    forbidden_response,
    validation_error_response,
    internal_error_response,
    conflict_response
)


# Create blueprint
auth_bp = Blueprint('auth', __name__)

logger = logging.getLogger(__name__)



@auth_bp.route('/register', methods=['POST'])
@rate_limit(10, 3600)  # 10 registrations per hour
@validate_json(['password', 'first_name'])  # Only password and first_name always required
@handle_exceptions
@log_request
def register():
    """
    User Registration (Email-based)
    ---
    tags:
      - Authentication
    description: |
      Register a new user with email and password.
      Note: For phone-based registration, use /phone/register/init and /phone/register/verify endpoints.
    parameters:
      - in: body
        name: body
        required: true
        schema:
          type: object
          required:
            - email
            - password
            - first_name
          properties:
            email:
              type: string
              format: email
              description: User email (required for email registration)
              example: user@example.com
            password:
              type: string
              minLength: 8
              example: SecurePass123
            phone:
              type: string
              description: Phone number (optional, can be added later)
              example: +998901234567
            first_name:
              type: string
              example: John
            last_name:
              type: string
              example: Doe
            date_of_birth:
              type: string
              format: date
              example: 1990-01-01
            gender:
              type: string
              enum: [male, female]
              example: male
            referral_code:
              type: string
              example: ABC123
    responses:
      201:
        description: User registered successfully
        schema:
          type: object
          properties:
            success:
              type: boolean
              example: true
            message:
              type: string
              example: Registration successful
            data:
              type: object
              properties:
                user:
                  $ref: '#/definitions/User'
                tokens:
                  $ref: '#/definitions/Tokens'
      400:
        description: Validation error
      409:
        description: User already exists
    """
    data = request.get_json()

    # Validate: email is required for this endpoint (email-based registration)
    if not data.get('email'):
        return validation_error_response(
            errors={'email': ['Email is required for email-based registration. Use /phone/register/init for phone registration.']}
        )

    # Last name is optional but recommended
    if not data.get('last_name'):
        data['last_name'] = ''

    try:
        user, tokens = get_auth_service().register_user(
            email=data['email'],
            password=data['password'],
            phone=data.get('phone'),  # Phone is optional for email registration
            first_name=data['first_name'],
            last_name=data['last_name'],
            date_of_birth=data.get('date_of_birth'),
            gender=data.get('gender'),
            referral_code=data.get('referral_code'),
            registration_method='email'  # Mark as email registration
        )

        # Create response with standardized format
        response_data, status_code = created_response(
            data={
                'user': {
                    'id': user.id,
                    'email': user.email,
                    'phone': user.phone,
                    'first_name': user.first_name,
                    'last_name': user.last_name,
                    'status': user.status.value if hasattr(user.status, 'value') else user.status,
                    'email_verified': user.email_verified_at is not None,
                    'phone_verified': user.phone_verified_at is not None
                },
                'tokens': tokens
            },
            message=get_translation('api.auth.registration_successful')
        )

        # Set JWT cookies for frontend navigation (same as login)
        set_access_cookies(response_data, tokens['access_token'])
        set_refresh_cookies(response_data, tokens['refresh_token'])

        return response_data, status_code

    except ValidationError as e:
        return error_response(
            message=e.message,
            errors=e.details,
            status_code=400
        )
    except ConflictError as e:
        return error_response(
            message=e.message,
            errors=e.details,
            status_code=409
        )


@auth_bp.route('/login', methods=['POST'])
@rate_limit(20, 3600)  # 20 login attempts per hour
@validate_json(['identifier', 'password'])
@handle_exceptions
@log_request
def login():
    """
    User Login
    ---
    tags:
      - Authentication
    parameters:
      - in: body
        name: body
        required: true
        schema:
          type: object
          required:
            - identifier
            - password
          properties:
            identifier:
              type: string
              description: Email or phone number
              example: user@example.com
            password:
              type: string
              example: SecurePass123
            remember_me:
              type: boolean
              example: false
    responses:
      200:
        description: Login successful
        schema:
          type: object
          properties:
            success:
              type: boolean
              example: true
            message:
              type: string
              example: Login successful
            data:
              type: object
              properties:
                user:
                  $ref: '#/definitions/User'
                tokens:
                  $ref: '#/definitions/Tokens'
                permissions:
                  type: object
      401:
        description: Invalid credentials
      423:
        description: Account locked
    """
    data = request.get_json()
    
    try:
        user, tokens = get_auth_service().login_user(
            identifier=data['identifier'].strip(),
            password=data['password']
        )
        
        # Create response with standardized format
        response_data, status_code = success_response(
            data={
                'user': {
                    'id': user.id,
                    'email': user.email,
                    'phone': user.phone,
                    'first_name': user.first_name,
                    'last_name': user.last_name,
                    'role': user.role.value if hasattr(user.role, 'value') else user.role,
                    'status': user.status.value if hasattr(user.status, 'value') else user.status,
                    'email_verified': user.email_verified_at is not None,
                    'phone_verified': user.phone_verified_at is not None,
                    'last_login': user.last_login.isoformat() if user.last_login else None
                },
                'tokens': tokens,
                'permissions': get_auth_service().get_user_permissions(user.id)
            },
            message=get_translation('api.auth.login_successful')
        )

        # Set JWT cookies for frontend navigation
        set_access_cookies(response_data, tokens['access_token'])
        set_refresh_cookies(response_data, tokens['refresh_token'])

        return response_data, status_code

    except UnauthorizedError as e:
        return unauthorized_response(message=e.message)
    except ValidationError as e:
        return error_response(
            message=e.message,
            errors=e.details,
            status_code=423
        )


# =============================================================================
# Phone Registration Endpoints (Uzbekistan +998 only)
# =============================================================================

@auth_bp.route('/phone/register/init', methods=['POST'])
@rate_limit(3, 600)  # 3 requests per 10 minutes per IP
@validate_json(['phone'])
@handle_exceptions
@log_request
def phone_register_init():
    """
    Initiate Phone Registration - Step 1: Request OTP
    ---
    tags:
      - Phone Registration
    parameters:
      - in: body
        name: body
        required: true
        schema:
          type: object
          required:
            - phone
          properties:
            phone:
              type: string
              description: Uzbekistan phone number (+998 XX XXX XX XX)
              example: +998901234567
            preferred_language:
              type: string
              enum: [uz, ru, en]
              default: uz
              example: uz
    responses:
      200:
        description: OTP sent successfully
        schema:
          type: object
          properties:
            success:
              type: boolean
              example: true
            message:
              type: string
              example: Verification code sent
            data:
              type: object
              properties:
                phone_masked:
                  type: string
                  example: +998***4567
                expires_in:
                  type: integer
                  example: 180
                resend_available_in:
                  type: integer
                  example: 60
      400:
        description: Validation error (invalid phone format)
      409:
        description: Phone already registered
      429:
        description: Rate limit exceeded or resend cooldown
    """
    from pydantic import ValidationError as PydanticValidationError
    from business_app.serializers.auth_serializers import PhoneRegistrationInitRequest

    data = request.get_json()

    try:
        # Validate with Pydantic
        req = PhoneRegistrationInitRequest(**data)
    except PydanticValidationError as e:
        errors = {}
        for error in e.errors():
            field = error['loc'][0] if error['loc'] else 'general'
            if field not in errors:
                errors[field] = []
            errors[field].append(error['msg'])
        return validation_error_response(errors=errors)

    try:
        result = get_auth_service().initiate_phone_registration(
            phone=req.phone,
            language=req.preferred_language
        )

        return success_response(
            data=result,
            message=get_translation('api.auth.otp_sent', default='Verification code sent')
        )

    except ValidationError as e:
        error_code = getattr(e, 'error_code', None)
        status_code = 429 if error_code in ['RESEND_COOLDOWN', 'OTP_MAX_ATTEMPTS'] else 400
        return error_response(
            message=e.message,
            errors=e.details,
            status_code=status_code
        )
    except ConflictError as e:
        return conflict_response(message=e.message)


@auth_bp.route('/phone/register/verify', methods=['POST'])
@rate_limit(10, 600)  # 10 requests per 10 minutes per IP
@validate_json(['phone', 'otp_code', 'first_name', 'password'])
@handle_exceptions
@log_request
def phone_register_verify():
    """
    Complete Phone Registration - Step 2: Verify OTP and Create Account
    ---
    tags:
      - Phone Registration
    parameters:
      - in: body
        name: body
        required: true
        schema:
          type: object
          required:
            - phone
            - otp_code
            - first_name
            - password
          properties:
            phone:
              type: string
              description: Uzbekistan phone number (+998 XX XXX XX XX)
              example: +998901234567
            otp_code:
              type: string
              description: 6-digit OTP code
              example: "123456"
            first_name:
              type: string
              example: John
            last_name:
              type: string
              example: Doe
            password:
              type: string
              minLength: 8
              example: SecurePass123!
            referral_code:
              type: string
              example: ABC123
    responses:
      201:
        description: Registration successful
        schema:
          type: object
          properties:
            success:
              type: boolean
              example: true
            message:
              type: string
              example: Registration successful
            data:
              type: object
              properties:
                user:
                  type: object
                  properties:
                    id:
                      type: integer
                    phone:
                      type: string
                    email:
                      type: string
                      nullable: true
                    first_name:
                      type: string
                    last_name:
                      type: string
                    is_verified:
                      type: boolean
                    registration_method:
                      type: string
                tokens:
                  type: object
                  properties:
                    access_token:
                      type: string
                    refresh_token:
                      type: string
                    expires_in:
                      type: integer
      400:
        description: Validation error or invalid OTP
      409:
        description: Phone already registered
      429:
        description: Too many OTP attempts
    """
    from pydantic import ValidationError as PydanticValidationError
    from business_app.serializers.auth_serializers import PhoneRegistrationVerifyRequest

    data = request.get_json()

    try:
        # Validate with Pydantic
        req = PhoneRegistrationVerifyRequest(**data)
    except PydanticValidationError as e:
        errors = {}
        for error in e.errors():
            field = error['loc'][0] if error['loc'] else 'general'
            if field not in errors:
                errors[field] = []
            errors[field].append(error['msg'])
        return validation_error_response(errors=errors)

    try:
        user, tokens = get_auth_service().complete_phone_registration(
            phone=req.phone,
            otp_code=req.otp_code,
            first_name=req.first_name,
            last_name=req.last_name,
            password=req.password,
            referral_code=req.referral_code
        )

        # Create response
        response_data, status_code = created_response(
            data={
                'user': {
                    'id': user.id,
                    'phone': user.phone,
                    'email': user.email,
                    'first_name': user.first_name,
                    'last_name': user.last_name,
                    'is_verified': user.is_verified,
                    'registration_method': user.registration_method
                },
                'tokens': tokens
            },
            message=get_translation('api.auth.registration_successful')
        )

        # Set JWT cookies
        set_access_cookies(response_data, tokens['access_token'])
        set_refresh_cookies(response_data, tokens['refresh_token'])

        return response_data, status_code

    except ValidationError as e:
        error_code = getattr(e, 'error_code', None)
        if error_code == 'OTP_EXPIRED':
            status_code = 400
        elif error_code in ['INVALID_OTP', 'OTP_MAX_ATTEMPTS']:
            status_code = 429 if error_code == 'OTP_MAX_ATTEMPTS' else 400
        else:
            status_code = 400
        return error_response(
            message=e.message,
            errors=e.details,
            status_code=status_code
        )
    except ConflictError as e:
        return conflict_response(message=e.message)


@auth_bp.route('/phone/resend-otp', methods=['POST'])
@rate_limit(3, 300)  # 3 requests per 5 minutes per IP
@validate_json(['phone'])
@handle_exceptions
@log_request
def phone_resend_otp():
    """
    Resend OTP for Phone Registration
    ---
    tags:
      - Phone Registration
    parameters:
      - in: body
        name: body
        required: true
        schema:
          type: object
          required:
            - phone
          properties:
            phone:
              type: string
              description: Uzbekistan phone number (+998 XX XXX XX XX)
              example: +998901234567
    responses:
      200:
        description: OTP resent successfully
        schema:
          type: object
          properties:
            success:
              type: boolean
              example: true
            message:
              type: string
              example: Verification code resent
            data:
              type: object
              properties:
                phone_masked:
                  type: string
                  example: +998***4567
                expires_in:
                  type: integer
                  example: 180
                resend_available_in:
                  type: integer
                  example: 60
      400:
        description: Invalid phone format
      409:
        description: Phone already registered
      429:
        description: Cooldown not expired
    """
    from pydantic import ValidationError as PydanticValidationError
    from business_app.serializers.auth_serializers import PhoneResendOtpRequest

    data = request.get_json()

    try:
        req = PhoneResendOtpRequest(**data)
    except PydanticValidationError as e:
        errors = {}
        for error in e.errors():
            field = error['loc'][0] if error['loc'] else 'general'
            if field not in errors:
                errors[field] = []
            errors[field].append(error['msg'])
        return validation_error_response(errors=errors)

    try:
        result = get_auth_service().resend_phone_registration_otp(phone=req.phone)

        return success_response(
            data=result,
            message=get_translation('api.auth.otp_resent', default='Verification code resent')
        )

    except ValidationError as e:
        error_code = getattr(e, 'error_code', None)
        status_code = 429 if error_code in ['RESEND_COOLDOWN', 'OTP_MAX_ATTEMPTS'] else 400
        return error_response(
            message=e.message,
            errors=e.details,
            status_code=status_code
        )
    except ConflictError as e:
        return conflict_response(message=e.message)


# =============================================================================
# End Phone Registration Endpoints
# =============================================================================


@auth_bp.route('/send-otp', methods=['POST'])
@validate_json(['phone'])
@jwt_required()
@rate_limit(3, 60)  # 3 OTP requests per minute
@handle_exceptions
@log_request
def send_otp():
    """
    Send OTP to Phone Number for Verification
    ---
    tags:
      - Authentication
    security:
      - Bearer: []
    parameters:
      - in: body
        name: body
        required: true
        schema:
          type: object
          required:
            - phone
          properties:
            phone:
              type: string
              example: +998901234567
    responses:
      200:
        description: OTP sent successfully
      400:
        description: Invalid phone number
      409:
        description: Phone number already in use
    """
    data = request.get_json()
    phone = data.get('phone')
    user_id = get_jwt_identity()
    
    if phone_validator(phone):
        return validation_error_response(
            errors=[get_translation('error.validation.invalid_phone')]
        )

    # Check if phone number is already in use by another user
    existing_user = User.query.filter(
        User.phone == phone,
        User.id != user_id,
        User.status != 'inactive'
    ).first()

    if existing_user:
        # Log suspicious activity
        from business_app.utils.audit_logger import audit_suspicious_activity
        audit_suspicious_activity(
            f"User {user_id} attempted to verify phone {phone} already in use by user {existing_user.id}",
            additional_data={'target_phone': phone, 'existing_user_id': existing_user.id}
        )
        return error_response(
            message=get_translation('api.auth.email_already_exists'),
            status_code=409
        )
    
    # Store pending phone number in Redis for verification
    auth_service = get_auth_service()
    pending_phone_key = f"pending_phone:{user_id}"
    auth_service.redis_client.setex(pending_phone_key, 1800, phone)  # 30 minutes expiry
    
    # Log phone verification attempt
    from business_app.utils.audit_logger import audit_logger, AuditEventType, AuditSeverity
    audit_logger.log_event(
        event_type=AuditEventType.SENSITIVE_DATA_ACCESS,
        action="phone_verification_requested",
        severity=AuditSeverity.MEDIUM,
        resource_type="user_phone",
        resource_id=str(user_id),
        description=f"Phone verification requested for {phone}",
        additional_data={'new_phone': phone}
    )
    
    # Generate and send OTP to the new phone number
    success = auth_service.send_verification_sms(user_id, phone)

    if success:
        return success_response(
            data={
                'phone_masked': phone[:3] + '***' + phone[-4:] if len(phone) > 7 else '***'
            },
            message=get_translation('api.auth.phone_verified')
        )
    else:
        return internal_error_response(
            message=get_translation('error.server_error')
        )
  

@auth_bp.route('/refresh', methods=['POST'])
@auth_bp.route('/refresh-token', methods=['POST'])  # Alias for backwards compatibility
@validate_json(['refresh_token'])
@handle_exceptions
@log_request
def refresh():
    """
    Refresh Access Token
    ---
    tags:
      - Authentication
    parameters:
      - in: body
        name: body
        required: true
        schema:
          type: object
          required:
            - refresh_token
          properties:
            refresh_token:
              type: string
              example: eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzI1NiJ9...
    responses:
      200:
        description: Token refreshed successfully
        schema:
          type: object
          properties:
            success:
              type: boolean
              example: true
            data:
              $ref: '#/definitions/Tokens'
      401:
        description: Invalid refresh token
    """
    data = request.get_json()
    
    try:
        # Support both AuthService and TokenService for token refresh
        try:
            tokens = get_auth_service().refresh_token(data['refresh_token'])
        except AttributeError:
            from business_app.services.token_service import TokenService
            token_service = TokenService()
            tokens = token_service.refresh_access_token(data['refresh_token'])
        
        return success_response(
            data=tokens,
            message=get_translation('api.auth.token_invalid')
        )

    except UnauthorizedError as e:
        return unauthorized_response(message=e.message)
    except Exception as e:
        logger.error(f"Token refresh failed: {e}")
        return unauthorized_response(message=get_translation('api.auth.token_invalid'))




@auth_bp.route('/verify-email', methods=['POST'])
@validate_json(['token'])
@handle_exceptions
@log_request
def verify_email():
    """
    Verify Email Address
    ---
    tags:
      - Authentication
    parameters:
      - in: body
        name: body
        required: true
        schema:
          type: object
          required:
            - token
          properties:
            token:
              type: string
              example: abc123def456
    responses:
      200:
        description: Email verified successfully
      400:
        description: Invalid or expired token
    """
    data = request.get_json()
    
    success = get_auth_service().verify_email(data['token'])

    if success:
        return success_response(
            message=get_translation('api.auth.phone_verified')
        )
    else:
        return error_response(
            message=get_translation('api.auth.token_invalid'),
            status_code=400
        )


@auth_bp.route('/verify-phone', methods=['POST'])
@auth_bp.route('/verify-otp', methods=['POST'])  # Alias for backwards compatibility
@rate_limit(30, 3600)
@validate_json(['otp'])
@handle_exceptions
@log_request
def verify_phone():
    """
    Verify Phone Number with OTP
    ---
    tags:
      - Authentication
    security:
      - Bearer: []
    parameters:
      - in: body
        name: body
        required: true
        schema:
          type: object
          required:
            - otp
          properties:
            otp:
              type: string
              example: "123456"
            user_id:
              type: integer
              description: Required when using /verify-otp endpoint without JWT
              example: 1
    responses:
      200:
        description: Phone verified and updated successfully
      400:
        description: Invalid OTP or no pending phone number
      404:
        description: No pending phone verification found
    """
    data = request.get_json()
    
    # Support both JWT-based and user_id-based verification
    if '/verify-otp' in request.path and 'user_id' in data:
        # Legacy verify-otp endpoint behavior
        user_id = data['user_id']
    else:
        # JWT-based verification
        user_id = get_jwt_identity()
    
    try:
        auth_service = get_auth_service()
        
        # Get pending phone number from Redis
        pending_phone_key = f"pending_phone:{user_id}"
        pending_phone = auth_service.redis_client.get(pending_phone_key)
        
        if not pending_phone:
            # Log suspicious activity
            from business_app.utils.audit_logger import audit_suspicious_activity
            audit_suspicious_activity(
                f"User {user_id} attempted phone verification without pending phone number",
                additional_data={'attempted_otp': data['otp']}
            )
            return not_found_response(
                message=get_translation('error.not_found')
            )
        
        pending_phone = pending_phone.decode('utf-8')
        
        # Verify OTP
        success = auth_service.verify_phone(user_id, data['otp'])
        
        if success:
            # OTP is valid, now update the user's phone number
            user = User.query.get(user_id)
            if user:
                old_phone = user.phone
                user.phone = pending_phone
                user.phone_verified_at = datetime.now(timezone.utc)
                db.session.commit()
                
                # Remove pending phone from Redis
                auth_service.redis_client.delete(pending_phone_key)
                
                # Log successful phone update with audit trail
                from business_app.utils.audit_logger import audit_logger, AuditEventType, AuditSeverity
                audit_logger.log_event(
                    event_type=AuditEventType.USER_UPDATED,
                    action="phone_number_verified_and_updated",
                    severity=AuditSeverity.HIGH,
                    resource_type="user_phone",
                    resource_id=str(user_id),
                    description=f"Phone number successfully verified and updated",
                    old_values={'phone': old_phone},
                    new_values={'phone': pending_phone, 'phone_verified_at': user.phone_verified_at.isoformat()},
                    success=True
                )
                
                logger.info(f"Phone verified and updated successfully for user {user_id}: {old_phone} -> {pending_phone}")
                return success_response(
                    data={
                        'phone': pending_phone,
                        'phone_verified': True
                    },
                    message=get_translation('api.auth.phone_verified')
                )
            else:
                return not_found_response(
                    message=get_translation('error.not_found')
                )
        else:
            # Log failed verification attempt
            from business_app.utils.audit_logger import audit_logger, AuditEventType, AuditSeverity
            audit_logger.log_event(
                event_type=AuditEventType.SENSITIVE_DATA_ACCESS,
                action="phone_verification_failed",
                severity=AuditSeverity.MEDIUM,
                resource_type="user_phone",
                resource_id=str(user_id),
                description=f"Failed phone verification attempt",
                additional_data={'pending_phone': pending_phone, 'provided_otp': data['otp']},
                success=False,
                error_message="Invalid OTP provided"
            )
            
            logger.warning(f"Invalid OTP provided for user {user_id}")
            return error_response(
                message=get_translation('api.auth.invalid_credentials'),
                status_code=400
            )

    except Exception as e:
        logger.error(f"Error in verify phone/OTP: {e}")
        # Log system error
        from business_app.utils.audit_logger import audit_logger, AuditEventType, AuditSeverity
        audit_logger.log_event(
            event_type=AuditEventType.SYSTEM_MAINTENANCE,
            action="phone_verification_system_error",
            severity=AuditSeverity.HIGH,
            resource_type="user_phone",
            resource_id=str(user_id),
            description=f"System error during phone verification",
            success=False,
            error_message=str(e)
        )
        return internal_error_response(
            message=get_translation('error.server_error')
        )


@auth_bp.route('/resend-email-verification', methods=['POST'])
@jwt_required()
@rate_limit(5, 3600)  # 5 resends per hour
@handle_exceptions
@log_request
def resend_email_verification():
    """
    Resend Email Verification
    ---
    tags:
      - Authentication
    security:
      - Bearer: []
    responses:
      200:
        description: Verification email sent
    """
    user_id = get_jwt_identity()

    success = get_auth_service().send_verification_email(user_id)

    return success_response(
        message=get_translation('success.sent')
    )


@auth_bp.route('/resend-sms-verification', methods=['POST'])
@jwt_required()
@rate_limit(5, 3600)  # 5 resends per hour
@handle_exceptions
@log_request
def resend_sms_verification():
    """
    Resend SMS Verification
    ---
    tags:
      - Authentication
    security:
      - Bearer: []
    responses:
      200:
        description: Verification SMS sent
    """
    user_id = get_jwt_identity()

    success = get_auth_service().send_verification_sms(user_id)

    return success_response(
        message=get_translation('success.sent')
    )


@auth_bp.route('/forgot-password', methods=['POST'])
@rate_limit(5, 3600)  # 5 password reset requests per hour
@validate_json(['identifier'])
@handle_exceptions
@log_request
def forgot_password():
    """
    Request Password Reset
    ---
    tags:
      - Authentication
    parameters:
      - in: body
        name: body
        required: true
        schema:
          type: object
          required:
            - identifier
          properties:
            identifier:
              type: string
              description: Email or phone number
              example: user@example.com
    responses:
      200:
        description: Password reset email sent (always returns success)
    """
    data = request.get_json()
    
    # Always return success to prevent email enumeration
    get_auth_service().request_password_reset(data['identifier'].strip())

    return success_response(
        message=get_translation('success.sent')
    )


@auth_bp.route('/reset-password', methods=['POST'])
@csrf_required
@validate_json(['token', 'new_password'])
@handle_exceptions
@log_request
def reset_password():
    """
    Reset Password
    ---
    tags:
      - Authentication
    parameters:
      - in: body
        name: body
        required: true
        schema:
          type: object
          required:
            - token
            - new_password
          properties:
            token:
              type: string
              example: abc123def456
            new_password:
              type: string
              minLength: 8
              example: NewSecurePass123
    responses:
      200:
        description: Password reset successfully
      400:
        description: Invalid token or weak password
    """
    data = request.get_json()
    
    try:
        success = get_auth_service().reset_password(data['token'], data['new_password'])

        if success:
            return success_response(
                message=get_translation('success.updated')
            )
        else:
            return error_response(
                message=get_translation('api.auth.token_invalid'),
                status_code=400
            )

    except ValidationError as e:
        return error_response(
            message=e.message,
            errors=e.details,
            status_code=400
        )


@auth_bp.route('/change-password', methods=['POST'])
@jwt_required()
@csrf_required
@validate_json(['current_password', 'new_password'])
@handle_exceptions
@log_request
def change_password():
    """
    Change Password
    ---
    tags:
      - Authentication
    security:
      - Bearer: []
    parameters:
      - in: body
        name: body
        required: true
        schema:
          type: object
          required:
            - current_password
            - new_password
          properties:
            current_password:
              type: string
              example: CurrentPass123
            new_password:
              type: string
              minLength: 8
              example: NewSecurePass123
    responses:
      200:
        description: Password changed successfully
      400:
        description: Invalid current password or weak new password
      401:
        description: Current password incorrect
    """
    data = request.get_json()
    user_id = get_jwt_identity()
    
    try:
        success = get_auth_service().change_password(
            user_id,
            data['current_password'],
            data['new_password']
        )

        if success:
            return success_response(
                message=get_translation('success.updated')
            )
        else:
            return error_response(
                message=get_translation('error.server_error'),
                status_code=400
            )

    except ValidationError as e:
        return error_response(
            message=e.message,
            errors=e.details,
            status_code=400
        )
    except UnauthorizedError as e:
        return unauthorized_response(message=e.message)


@auth_bp.route('/profile', methods=['GET'])
@jwt_required()
@handle_exceptions
def get_profile():
    """
    Get User Profile
    ---
    tags:
      - Authentication
    security:
      - Bearer: []
    responses:
      200:
        description: User profile retrieved successfully
        schema:
          type: object
          properties:
            success:
              type: boolean
              example: true
            data:
              $ref: '#/definitions/UserProfile'
    """
    user_id = get_jwt_identity()
    
    from business_app.models.user import User
    user = User.query.get(user_id)

    if not user:
        return not_found_response(
            message=get_translation('error.not_found')
        )

    return success_response(
        data={
            'id': user.id,
            'email': user.email,
            'phone': user.phone,
            'first_name': user.first_name,
            'last_name': user.last_name,
            'date_of_birth': user.date_of_birth.isoformat() if user.date_of_birth else None,
            'gender': user.gender.value if hasattr(user.gender, 'value') else user.gender,
            'role': user.role.value if hasattr(user.role, 'value') else user.role,
            'status': user.status.value if hasattr(user.status, 'value') else user.status,
            'email_verified': user.email_verified_at is not None,
            'phone_verified': user.phone_verified_at is not None,
            'created_at': user.created_at.isoformat(),
            'last_login': user.last_login.isoformat() if user.last_login else None,
            'preferred_language': getattr(user, 'preferred_language', 'en'),
            'permissions': get_auth_service().get_user_permissions(user_id)
        }
    )


@auth_bp.route('/permissions', methods=['GET'])
@jwt_required()
@handle_exceptions
def get_permissions():
    """
    Get User Permissions
    ---
    tags:
      - Authentication
    security:
      - Bearer: []
    responses:
      200:
        description: User permissions retrieved successfully
        schema:
          type: object
          properties:
            success:
              type: boolean
              example: true
            data:
              type: object
              additionalProperties:
                type: boolean
    """
    user_id = get_jwt_identity()
    permissions = get_auth_service().get_user_permissions(user_id)

    return success_response(
        data=permissions
    )


@auth_bp.route('/addresses', methods=['GET'])
@jwt_required()
@handle_exceptions
def get_user_addresses():
    """
    Get User Addresses
    ---
    tags:
      - Authentication
    security:
      - Bearer: []
    responses:
      200:
        description: User addresses retrieved successfully
        schema:
          type: object
          properties:
            success:
              type: boolean
              example: true
            data:
              type: object
              properties:
                addresses:
                  type: array
                  items:
                    type: object
    """
    user_id = get_jwt_identity()
    
    from business_app.models.user import UserAddress
    addresses = UserAddress.query.filter_by(user_id=user_id).all()

    return success_response(
        data={
            'addresses': [addr.to_dict() for addr in addresses]
        }
    )


@auth_bp.route('/addresses', methods=['POST'])
@jwt_required()
@validate_json(['title'])
@handle_exceptions
def add_user_address():
    """
    Add User Address
    ---
    tags:
      - Authentication
    security:
      - Bearer: []
    parameters:
      - in: body
        name: body
        required: true
        schema:
          type: object
          required:
            - title
          properties:
            title:
              type: string
              example: Home
            address_line1:
              type: string
              example: 123 Main St
            address_line2:
              type: string
              example: Apt 4B
            city:
              type: string
              example: Tashkent
            district:
              type: string
              example: Chilanzar
            postal_code:
              type: string
              example: 100000
            latitude:
              type: number
              example: 41.2995
            longitude:
              type: number
              example: 69.2401
            is_default:
              type: boolean
              example: false
            delivery_notes:
              type: string
              example: Ring bell twice
    responses:
      201:
        description: Address added successfully
      400:
        description: Validation error
    """
    user_id = get_jwt_identity()
    data = request.get_json()
    
    from business_app.models.user import UserAddress
    
    # If this is set as default, unset others
    if data.get('is_default', False):
        UserAddress.query.filter_by(user_id=user_id, is_default=True).update({'is_default': False})
    
    address = UserAddress(
        user_id=user_id,
        title=data['title'],
        full_address=data.get('full_address', data.get('address_line_1', '')),
        street_address=data.get('street_address', data.get('address_line_1')),
        city=data.get('city', 'Tashkent'),
        district=data.get('district'),
        postal_code=data.get('postal_code'),
        country=data.get('country', 'Uzbekistan'),
        latitude=data.get('latitude'),
        longitude=data.get('longitude'),
        is_default=data.get('is_default', False),
        is_business=data.get('is_business', False),
        delivery_instructions=data.get('delivery_instructions', data.get('delivery_notes')),
        landmark=data.get('landmark'),
        floor_number=data.get('floor_number'),
        apartment_number=data.get('apartment_number')
    )
    
    db.session.add(address)
    db.session.commit()

    return created_response(
        data={
            'address': address.to_dict()
        },
        message=get_translation('address_added_successfully')
    )


@auth_bp.route('/addresses/<int:address_id>', methods=['PUT', 'PATCH'])
@jwt_required()
@validate_json(required_fields=None)  # Remove required fields for partial updates
@handle_exceptions
def update_user_address(address_id):
    """
    Update User Address (Full or Partial)
    ---
    tags:
      - Authentication
    security:
      - Bearer: []
    parameters:
      - in: path
        name: address_id
        type: integer
        required: true
        description: Address ID to update
      - in: body
        name: body
        required: true
        description: Address fields to update (partial updates supported)
        schema:
          type: object
          properties:
            title:
              type: string
              example: Work Office
              description: Address title/name
            full_address:
              type: string
              example: 456 Business Blvd
              description: Complete address text
            street_address:
              type: string
              example: 456 Business Blvd
              description: Street address
            city:
              type: string
              example: Tashkent
              description: City name
            district:
              type: string
              example: Mirobod
              description: District/area name
            postal_code:
              type: string
              example: 100000
              description: Postal/ZIP code
            latitude:
              type: number
              example: 41.2995
              description: GPS latitude
            longitude:
              type: number
              example: 69.2401
              description: GPS longitude
            delivery_instructions:
              type: string
              example: Reception on ground floor
              description: Special delivery instructions
    responses:
      200:
        description: Address updated successfully
      404:
        description: Address not found
      400:
        description: Validation error
    """
    user_id = get_jwt_identity()
    data = request.get_json()

    logger.info(f"TEST API update_user_address: {data=}")
    
    
    from business_app.models.user import UserAddress
    
    # Find address belonging to current user
    address = UserAddress.query.filter_by(id=address_id, user_id=user_id).first()
    if not address:
        return not_found_response(message='Address not found')

    # Update address fields (partial update support)
    if 'title' in data:
        address.title = data['title']
    if 'full_address' in data:
        address.full_address = data['full_address']
    if 'street_address' in data:
        address.street_address = data['street_address']
    if 'city' in data:
        address.city = data['city']
    if 'district' in data:
        address.district = data['district']
    if 'postal_code' in data:
        address.postal_code = data['postal_code']
    if 'latitude' in data:
        address.latitude = data['latitude']
    if 'longitude' in data:
        address.longitude = data['longitude']
    if 'delivery_instructions' in data:
        address.delivery_instructions = data['delivery_instructions']
    if 'landmark' in data:
        address.landmark = data['landmark']
    if 'floor_number' in data:
        address.floor_number = data['floor_number']
    if 'apartment_number' in data:
        address.apartment_number = data['apartment_number']

    db.session.commit()

    return success_response(
        data={
            'address': address.to_dict()
        },
        message='Address updated successfully'
    )


@auth_bp.route('/addresses/<int:address_id>', methods=['DELETE'])
@jwt_required()
@handle_exceptions
def delete_user_address(address_id):
    """
    Delete User Address
    ---
    tags:
      - Authentication
    security:
      - Bearer: []
    parameters:
      - in: path
        name: address_id
        type: integer
        required: true
        description: Address ID to delete
    responses:
      200:
        description: Address deleted successfully
      404:
        description: Address not found
      400:
        description: Cannot delete default address with other addresses present
    """
    user_id = get_jwt_identity()
    
    from business_app.models.user import UserAddress
    
    # Find address belonging to current user
    address = UserAddress.query.filter_by(id=address_id, user_id=user_id).first()
    if not address:
        return not_found_response(message='Address not found')

    # Check if trying to delete default address when other addresses exist
    if address.is_default:
        other_addresses_count = UserAddress.query.filter(
            UserAddress.user_id == user_id,
            UserAddress.id != address_id
        ).count()

        if other_addresses_count > 0:
            return error_response(
                message=get_translation('error.forbidden'),
                status_code=400
            )

    db.session.delete(address)
    db.session.commit()

    return success_response(
        message=get_translation('success.deleted')
    )


@auth_bp.route('/addresses/<int:address_id>/set-default', methods=['PATCH'])
@jwt_required()
@handle_exceptions
def set_default_address(address_id):
    """
    Set Address as Default
    ---
    tags:
      - Authentication
    security:
      - Bearer: []
    parameters:
      - in: path
        name: address_id
        type: integer
        required: true
        description: Address ID to set as default
    responses:
      200:
        description: Address set as default successfully
      404:
        description: Address not found
    """
    user_id = get_jwt_identity()
    
    from business_app.models.user import UserAddress
    
    # Find address belonging to current user
    address = UserAddress.query.filter_by(id=address_id, user_id=user_id).first()
    if not address:
        return not_found_response(message='Address not found')

    # Unset all other addresses as default for this user
    UserAddress.query.filter_by(user_id=user_id, is_default=True).update({'is_default': False})

    # Set this address as default
    address.is_default = True
    db.session.commit()

    return success_response(
        data={
            'address': address.to_dict()
        },
        message=get_translation('success.updated')
    )


@auth_bp.route('/profile', methods=['PUT'])
@jwt_required()
@handle_exceptions
def update_profile():
    """
    Update User Profile
    ---
    tags:
      - Authentication
    security:
      - Bearer: []
    parameters:
      - in: body
        name: body
        schema:
          type: object
          properties:
            first_name:
              type: string
              example: John
            last_name:
              type: string
              example: Doe
            full_name:
              type: string
              example: John Doe
            phone:
              type: string
              example: +998901234567
            date_of_birth:
              type: string
              format: date
              example: 1990-01-01
            gender:
              type: string
              enum: [male, female]
              example: male
            preferred_language:
              type: string
              example: en
    responses:
      200:
        description: Profile updated successfully
      400:
        description: Validation error
    """
    user_id = get_jwt_identity()
    data = request.get_json() or {}
    
    from business_app.models.user import User
    user = User.query.get(user_id)

    if not user:
        return not_found_response(message=get_translation('user_not_found'))
    
    # Update fields if provided
    if 'first_name' in data:
        user.first_name = data['first_name']
    if 'last_name' in data:
        user.last_name = data['last_name']
    if 'phone' in data:
        # Log attempt to update phone through profile endpoint
        from business_app.utils.audit_logger import audit_logger, AuditEventType, AuditSeverity
        audit_logger.log_event(
            event_type=AuditEventType.SENSITIVE_DATA_ACCESS,
            action="phone_update_blocked_profile_endpoint",
            severity=AuditSeverity.MEDIUM,
            resource_type="user_phone",
            resource_id=str(user_id),
            description="Blocked attempt to update phone number through profile endpoint",
            additional_data={'attempted_phone': data['phone'], 'current_phone': user.phone},
            success=False,
            error_message="Phone number updates must be done through verification process"
        )
        # Skip phone update - must be done through verification process
        logger.warning(f"User {user_id} attempted to update phone directly through profile endpoint")
    if 'date_of_birth' in data:
        try:
            from datetime import datetime
            user.date_of_birth = datetime.fromisoformat(data['date_of_birth'])
        except ValueError:
            pass
    if 'gender' in data:
        user.gender = data['gender']
    if 'preferred_language' in data:
        user.preferred_language = data['preferred_language']
    
    from datetime import datetime, timezone
    user.updated_at = datetime.now(timezone.utc)
    db.session.commit()

    user_data = {
        'id': user.id,
        'email': user.email,
        'phone': user.phone,
        'first_name': user.first_name,
        'last_name': user.last_name,
        'full_name': user.full_name,
        'date_of_birth': user.date_of_birth.isoformat() if user.date_of_birth else None,
        'gender': user.gender,
        'role': user.role.value if hasattr(user.role, 'value') else user.role,
        'status': user.status.value if hasattr(user.status, 'value') else user.status,
        'preferred_language': user.preferred_language,
        'updated_at': user.updated_at.isoformat()
    }

    # Add warning if phone update was attempted
    if 'phone' in data:
        user_data['phone_change_instructions'] = get_translation('use_change_phone_endpoint')
        return success_response(
            data={
                'user': user_data,
                'warning': get_translation('error.forbidden')
            },
            message=get_translation('success.updated')
        )

    return success_response(
        data={'user': user_data},
        message=get_translation('profile_updated_successfully')
    )


@auth_bp.route('/change-phone', methods=['POST'])
@jwt_required()
@validate_json(['new_phone'])
@rate_limit(5, 3600)  # 5 phone change requests per hour
@handle_exceptions
@log_request
def change_phone():
    """
    Request Phone Number Change with Verification
    ---
    tags:
      - Authentication
    security:
      - Bearer: []
    parameters:
      - in: body
        name: body
        required: true
        schema:
          type: object
          required:
            - new_phone
          properties:
            new_phone:
              type: string
              example: +998901234567
              description: New phone number to verify and set
    responses:
      200:
        description: OTP sent to new phone number
      400:
        description: Invalid phone number format
      409:
        description: Phone number already in use
      429:
        description: Too many phone change requests
    """
    data = request.get_json()
    new_phone = data.get('new_phone')
    user_id = get_jwt_identity()
    
    if phone_validator(new_phone):
        return validation_error_response(
            errors=[get_translation('error.validation.invalid_phone')]
        )

    user = User.query.get(user_id)
    if not user:
        return not_found_response(
            message=get_translation('error.not_found')
        )

    # Check if it's the same as current phone
    if user.phone == new_phone:
        return error_response(
            message=get_translation('error.forbidden'),
            status_code=400
        )
    
    # Check if phone number is already in use by another user
    existing_user = User.query.filter(
        User.phone == new_phone,
        User.id != user_id,
        User.status != 'inactive'
    ).first()
    
    if existing_user:
        # Log suspicious activity
        from business_app.utils.audit_logger import audit_suspicious_activity
        audit_suspicious_activity(
            f"User {user_id} attempted to change to phone {new_phone} already in use by user {existing_user.id}",
            additional_data={'target_phone': new_phone, 'existing_user_id': existing_user.id}
        )
        return error_response(
            message=get_translation('api.auth.email_already_exists'),
            status_code=409
        )
    
    # Store pending phone number change request with audit
    auth_service = get_auth_service()
    pending_phone_key = f"pending_phone:{user_id}"
    auth_service.redis_client.setex(pending_phone_key, 1800, new_phone)  # 30 minutes expiry
    
    # Log phone change request with audit
    from business_app.utils.audit_logger import audit_logger, AuditEventType, AuditSeverity
    audit_logger.log_event(
        event_type=AuditEventType.USER_UPDATED,
        action="phone_change_requested",
        severity=AuditSeverity.HIGH,
        resource_type="user_phone",
        resource_id=str(user_id),
        description=f"Phone change requested from {user.phone} to {new_phone}",
        old_values={'phone': user.phone},
        new_values={'pending_phone': new_phone},
        additional_data={
            'current_phone': user.phone,
            'requested_phone': new_phone,
            'request_ip': request.remote_addr
        }
    )
    
    # Generate and send OTP to the new phone number
    success = auth_service.send_verification_sms(user_id, new_phone)

    if success:
        return success_response(
            data={
                'current_phone': user.phone,
                'new_phone_masked': new_phone[:3] + '***' + new_phone[-4:] if len(new_phone) > 7 else '***',
                'expires_in': 1800  # 30 minutes
            },
            message=get_translation('success.sent')
        )
    else:
        return internal_error_response(
            message=get_translation('error.server_error')
        )


@auth_bp.route('/cancel-phone-change', methods=['POST'])
@jwt_required()
@handle_exceptions
@log_request
def cancel_phone_change():
    """
    Cancel Pending Phone Number Change
    ---
    tags:
      - Authentication
    security:
      - Bearer: []
    responses:
      200:
        description: Phone change request cancelled
      404:
        description: No pending phone change found
    """
    user_id = get_jwt_identity()
    auth_service = get_auth_service()
    
    # Check if there's a pending phone change
    pending_phone_key = f"pending_phone:{user_id}"
    pending_phone = auth_service.redis_client.get(pending_phone_key)
    
    if not pending_phone:
        return not_found_response(
            message=get_translation('error.not_found')
        )

    pending_phone = pending_phone.decode('utf-8')

    # Remove pending phone change
    auth_service.redis_client.delete(pending_phone_key)

    # Log cancellation with audit
    from business_app.utils.audit_logger import audit_logger, AuditEventType, AuditSeverity
    audit_logger.log_event(
        event_type=AuditEventType.USER_UPDATED,
        action="phone_change_cancelled",
        severity=AuditSeverity.MEDIUM,
        resource_type="user_phone",
        resource_id=str(user_id),
        description=f"Phone change to {pending_phone} was cancelled by user",
        additional_data={'cancelled_phone': pending_phone}
    )

    return success_response(
        message=get_translation('success.deleted')
    )


@auth_bp.route('/telegram-login', methods=['POST'])
@rate_limit_by_telegram_id(100, 3600)  # 100 per hour PER TELEGRAM USER (not shared IP)
@validate_json(['telegram_id'])
@handle_exceptions
@log_request
def telegram_login():
    """
    Telegram Bot Authentication
    ---
    tags:
      - Authentication
    parameters:
      - in: body
        name: body
        required: true
        schema:
          type: object
          required:
            - telegram_id
          properties:
            telegram_id:
              type: integer
              example: 123456789
            username:
              type: string
              example: john_doe
            first_name:
              type: string
              example: John
            last_name:
              type: string
              example: Doe
    responses:
      200:
        description: Authentication successful
        schema:
          type: object
          properties:
            success:
              type: boolean
              example: true
            data:
              type: object
              properties:
                access_token:
                  type: string
                user:
                  $ref: '#/definitions/User'
      401:
        description: User not found or not registered
    """
    logger.info("=== TELEGRAM LOGIN API ENDPOINT CALLED ===")
    data = request.get_json()
    telegram_id = data['telegram_id']
    
    logger.info(f"Telegram login request for user: {telegram_id}")
    logger.info(f"Request data: telegram_id={telegram_id}, username={data.get('username')}, "
               f"first_name={data.get('first_name')}, last_name={data.get('last_name')}")
    
    try:
        # Try to authenticate existing telegram user
        logger.info("Calling auth service to authenticate telegram user")
        user, tokens = get_auth_service().authenticate_telegram_user(
            telegram_id=telegram_id,
            username=data.get('username'),
            first_name=data.get('first_name'),
            last_name=data.get('last_name')
        )
        logger.info(f"Authentication successful for user: {user.id}")
        
        logger.info("Preparing successful response")
        response_data = {
            'success': True,
            'data': {
                'access_token': tokens['access_token'],
                'refresh_token': tokens['refresh_token'],
                'user': {
                    'id': user.id,
                    'telegram_id': user.telegram_id,
                    'email': user.email,
                    'phone': user.phone,
                    'first_name': user.first_name,
                    'last_name': user.last_name,
                    'role': user.role.value if hasattr(user.role, 'value') else user.role,
                    'status': user.status.value if hasattr(user.status, 'value') else user.status,
                    'is_verified': user.is_verified,
                    'is_premium': user.is_premium
                }
            }
        }
        
        logger.info(f"Returning successful response for user: {user.id}")
        logger.info("=== TELEGRAM LOGIN API SUCCESS ===")
        return success_response(
            data={
                'access_token': tokens['access_token'],
                'refresh_token': tokens['refresh_token'],
                'user': {
                    'id': user.id,
                    'telegram_id': user.telegram_id,
                    'email': user.email,
                    'phone': user.phone,
                    'first_name': user.first_name,
                    'last_name': user.last_name,
                    'role': user.role.value if hasattr(user.role, 'value') else user.role,
                    'status': user.status.value if hasattr(user.status, 'value') else user.status,
                    'is_verified': user.is_verified,
                    'is_premium': user.is_premium
                }
            }
        )

    except UnauthorizedError as e:
        logger.error(f"Unauthorized error during telegram login: {e}")
        # User not found - create a temporary guest user or return specific error
        return error_response(
            message=get_translation('api.auth.unauthorized'),
            data={
                'telegram_id': telegram_id,
                'registration_required': True,
                'error_code': 'USER_NOT_REGISTERED'
            },
            status_code=401
        )
    except Exception as e:
        logger.error(f"Unexpected error during telegram login: {e}")
        logger.error(f"Exception type: {type(e)}")
        import traceback
        logger.error(f"Traceback: {traceback.format_exc()}")
        logger.error("=== TELEGRAM LOGIN API ERROR ===")

        return internal_error_response(
            message=get_translation('api.auth.invalid_credentials')
        )


@auth_bp.route('/telegram-register', methods=['POST'])
@rate_limit_by_telegram_id(30, 3600)  # 30 per hour PER TELEGRAM USER (not shared container IP)
@validate_json(['telegram_id'])
@handle_exceptions
@log_request
def telegram_register():
    """
    Telegram Bot User Registration (Unified Table)
    ---
    tags:
      - Authentication
    parameters:
      - in: body
        name: body
        required: true
        schema:
          type: object
          required:
            - telegram_id
          properties:
            telegram_id:
              type: integer
              example: 123456789
            username:
              type: string
              example: john_doe
            first_name:
              type: string
              example: John
            last_name:
              type: string
              example: Doe
            language_code:
              type: string
              example: en
    responses:
      201:
        description: Telegram user registered successfully in unified table
        schema:
          type: object
          properties:
            success:
              type: boolean
              example: true
            message:
              type: string
              example: Registration successful
            data:
              type: object
              properties:
                user:
                  $ref: '#/definitions/User'
                tokens:
                  $ref: '#/definitions/Tokens'
      409:
        description: User already exists
    """
    logger.info("=== TELEGRAM REGISTER API ENDPOINT CALLED (UNIFIED TABLE) ===")
    data = request.get_json()
    telegram_id = data['telegram_id']
    
    logger.info(f"Telegram registration request for user: {telegram_id}")
    logger.info(f"Request data: {data}")
    
    try:
        # Check if user already exists in unified table
        existing_user = User.query.filter_by(telegram_id=str(telegram_id)).first()
        
        if existing_user:
            logger.info(f"User already exists with telegram_id: {telegram_id}")
            # Return login response instead of error
            from business_app.services.token_service import TokenService
            token_service = TokenService()
            tokens = token_service.generate_tokens(existing_user)
            
            # Check for cross-platform linking opportunities
            from business_app.services.cross_platform_sync_service import cross_platform_sync_service
            sync_suggestions = cross_platform_sync_service.suggest_account_linking(existing_user)
            platform_status = cross_platform_sync_service.get_user_platform_status(existing_user)

            return success_response(
                data={
                    'user': existing_user.to_dict(),
                    'tokens': tokens,
                    'platform_status': platform_status,
                    'linking_suggestions': sync_suggestions
                },
                message=get_translation('api.auth.email_already_exists')
            )
        
        # Check for potential account matches before creating new user
        from business_app.services.cross_platform_sync_service import cross_platform_sync_service
        
        # Look for existing accounts with matching phone (if provided)
        potential_matches = []
        if data.get('phone'):
            potential_matches = cross_platform_sync_service.find_potential_matches(
                phone=data['phone']
            )
        
        # If matches found, suggest linking instead of creating new account
        if potential_matches:
            logger.info(f"Found {len(potential_matches)} potential account matches for telegram registration")
            return error_response(
                message=get_translation('api.auth.email_already_exists'),
                data={
                    'error_code': 'ACCOUNT_MATCH_FOUND',
                    'potential_matches': [
                        {
                            'user_id': match.id,
                            'email': match.email,
                            'name': f"{match.first_name} {match.last_name}".strip(),
                            'registration_source': match.registration_source,
                            'has_telegram': bool(match.telegram_id)
                        }
                        for match in potential_matches[:3]  # Limit to 3 suggestions
                    ],
                    'linking_options': [
                        {
                            'action': 'link_with_existing',
                            'description': 'Link Telegram with existing account',
                            'endpoint': '/api/v1/auth/link-web-account'
                        }
                    ]
                },
                status_code=409
            )
        
        # Create new telegram user in unified table
        logger.info("Creating new telegram user in unified table...")
        
        user = User(
            telegram_id=str(telegram_id),
            first_name=data.get('first_name', 'Telegram User'),
            last_name=data.get('last_name', ''),
            email=f"telegram_{telegram_id}@bluestream.local",  # Placeholder email
            phone=None,  # Phone will be collected later
            password_hash="telegram_user",  # Placeholder, no password needed
            role=UserRole.CUSTOMER,
            status=UserStatus.ACTIVE,
            is_verified=False,
            registration_source='telegram',
            preferred_language=data.get('language_code', 'en'),
            # Bot-specific fields in unified table
            telegram_username=data.get('username'),
            is_bot_active=True,
            bot_state='{}',  # Empty initial state
            last_bot_interaction=datetime.now(timezone.utc)
        )
        
        db.session.add(user)
        db.session.commit()
        
        # Generate tokens using TokenService
        from business_app.services.token_service import TokenService
        token_service = TokenService()
        tokens = token_service.generate_tokens(user)
        
        logger.info(f"Successfully created telegram user with ID: {user.id}")

        return created_response(
            data={
                'user': user.to_dict(),
                'tokens': tokens
            },
            message=get_translation('api.auth.registration_successful')
        )
        
    except Exception as e:
        logger.error(f"Unexpected error during telegram registration: {e}")
        import traceback
        logger.error(f"Traceback: {traceback.format_exc()}")
        db.session.rollback()

        return internal_error_response(message=get_translation('error.server_error'))


@auth_bp.route('/link-telegram', methods=['POST'])
@jwt_required()
@validate_json(['telegram_id'])
@handle_exceptions
@log_request
def link_telegram():
    """
    Link Telegram Account to Web User
    ---
    tags:
      - Authentication
    security:
      - Bearer: []
    parameters:
      - in: body
        name: body
        required: true
        schema:
          type: object
          required:
            - telegram_id
          properties:
            telegram_id:
              type: integer
              example: 123456789
            username:
              type: string
              example: john_doe
            first_name:
              type: string
              example: John
            last_name:
              type: string
              example: Doe
    responses:
      200:
        description: Telegram account linked successfully
      409:
        description: Telegram ID already linked to another account
    """
    current_user_id = get_jwt_identity()
    data = request.get_json()
    telegram_id = str(data['telegram_id'])
    
    # Check if this telegram_id is already linked to another user
    existing_user = User.query.filter(
        User.telegram_id == telegram_id,
        User.id != current_user_id
    ).first()
    
    if existing_user:
        return conflict_response(message=get_translation('api.auth.email_already_exists'))
    
    # Update current user with telegram info
    user = User.query.get(current_user_id)
    user.telegram_id = telegram_id
    
    # Update name if provided and current name is empty/placeholder
    if data.get('first_name') and (not user.first_name or user.first_name == 'Telegram User'):
        user.first_name = data.get('first_name')
    if data.get('last_name') and not user.last_name:
        user.last_name = data.get('last_name')
    
    db.session.commit()

    return success_response(
        data={
            'user': {
                'id': user.id,
                'telegram_id': user.telegram_id,
                'first_name': user.first_name,
                'last_name': user.last_name,
                'email': user.email,
                'phone': user.phone
            }
        },
        message=get_translation('success.saved')
    )


@auth_bp.route('/link-web-account', methods=['POST'])
@rate_limit(10, 3600)  # 10 attempts per hour
@validate_json(['telegram_id', 'email', 'password'])
@handle_exceptions
@log_request
def link_web_account():
    """
    Link Web Account to Telegram User
    ---
    tags:
      - Authentication
    parameters:
      - in: body
        name: body
        required: true
        schema:
          type: object
          required:
            - telegram_id
            - email
            - password
          properties:
            telegram_id:
              type: integer
              example: 123456789
            email:
              type: string
              example: user@example.com
            password:
              type: string
              example: UserPassword123
    responses:
      200:
        description: Accounts linked successfully
      401:
        description: Invalid credentials
      409:
        description: Account already linked
    """
    data = request.get_json()
    telegram_id = str(data['telegram_id'])
    email = data['email'].lower().strip()
    password = data['password']
    
    # Find telegram user
    telegram_user = User.query.filter_by(telegram_id=telegram_id).first()
    if not telegram_user:
        return not_found_response(message=get_translation('error.not_found'))
    
    # Find web user and verify password
    web_user = User.query.filter_by(email=email).first()
    if not web_user or not web_user.check_password(password):
        return unauthorized_response(message=get_translation('api.auth.invalid_credentials'))
    
    # Check if accounts are already linked
    if web_user.telegram_id or telegram_user.email != f"telegram_{telegram_id}@bluestream.local":
        return conflict_response(message=get_translation('api.auth.email_already_exists'))
    
    # Merge accounts - keep web user as primary, update with telegram info
    web_user.telegram_id = telegram_id
    
    # Update name from telegram if web user has no proper name
    if telegram_user.first_name and telegram_user.first_name != 'Telegram User':
        web_user.first_name = telegram_user.first_name
    if telegram_user.last_name:
        web_user.last_name = telegram_user.last_name
    
    # Transfer any orders/data from telegram user to web user if needed
    # (This would need more complex logic based on your business rules)
    
    # Mark telegram user as merged (or delete it)
    telegram_user.status = 'merged'
    telegram_user.telegram_id = None  # Remove telegram_id from old record
    
    db.session.commit()
    
    # Generate tokens for the merged account
    tokens = get_auth_service()._generate_tokens(web_user)

    return success_response(
        data={
            'user': {
                'id': web_user.id,
                'telegram_id': web_user.telegram_id,
                'email': web_user.email,
                'phone': web_user.phone,
                'first_name': web_user.first_name,
                'last_name': web_user.last_name,
                'role': web_user.role,
                'status': web_user.status
            },
            'tokens': tokens
        },
        message=get_translation('success.saved')
    )


@auth_bp.route('/check-phone-availability', methods=['POST'])
@rate_limit_by_telegram_id(30, 3600)  # 30 per hour per telegram user
@validate_json(['phone', 'telegram_id'])
@handle_exceptions
@log_request
def check_phone_availability():
    """
    Check Phone Number Availability for Telegram Registration
    ---
    tags:
      - Authentication
    parameters:
      - in: body
        name: body
        required: true
        schema:
          type: object
          required:
            - phone
            - telegram_id
          properties:
            phone:
              type: string
              example: +998901234567
            telegram_id:
              type: integer
              example: 123456789
    responses:
      200:
        description: Phone availability status
        schema:
          type: object
          properties:
            available:
              type: boolean
            can_link:
              type: boolean
            existing_user_masked:
              type: string
    """
    data = request.get_json()
    phone = data['phone']
    telegram_id = str(data['telegram_id'])
    
    # Normalize phone number
    from business_app.utils.validators import normalize_phone_number
    phone = normalize_phone_number(phone)
    
    # Check if phone exists
    existing_user = User.query.filter_by(phone=phone).first()
    
    if not existing_user:
        return success_response(
            data={
                'available': True,
                'can_link': False,
                'existing_user_masked': None
            },
            message='Phone number is available'
        )
    
    # Phone exists - check if it can be linked
    # Cannot link if: user already has telegram_id, or user is inactive/merged
    # Note: status can be enum or string depending on how SQLAlchemy returns it
    from business_app.utils.constants import UserStatus
    user_status = existing_user.status.value if isinstance(existing_user.status, UserStatus) else existing_user.status
    can_link = (
        existing_user.telegram_id is None and 
        user_status == 'active' and
        existing_user.registration_source in ['web', 'email', 'phone']
    )
    
    # Mask the user info for privacy
    masked_name = existing_user.first_name[:1] + '***' if existing_user.first_name else '***'
    masked_email = None
    if existing_user.email and not existing_user.email.endswith('@bluestream.local'):
        parts = existing_user.email.split('@')
        if len(parts) == 2:
            masked_email = parts[0][:2] + '***@' + parts[1]
    
    return success_response(
        data={
            'available': False,
            'can_link': can_link,
            'existing_user_masked': {
                'name': masked_name,
                'email': masked_email,
                'registration_source': existing_user.registration_source
            }
        },
        message='Phone number already registered'
    )


@auth_bp.route('/link-phone-account/send-otp', methods=['POST'])
@rate_limit_by_telegram_id(5, 3600)  # 5 OTP requests per hour per telegram user
@validate_json(['phone', 'telegram_id'])
@handle_exceptions
@log_request
def link_phone_send_otp():
    """
    Send OTP to Phone for Account Linking
    ---
    tags:
      - Authentication
    parameters:
      - in: body
        name: body
        required: true
        schema:
          type: object
          required:
            - phone
            - telegram_id
          properties:
            phone:
              type: string
              example: +998901234567
            telegram_id:
              type: integer
              example: 123456789
    responses:
      200:
        description: OTP sent successfully
      400:
        description: Cannot link - phone not found or already linked
    """
    data = request.get_json()
    phone = data['phone']
    telegram_id = str(data['telegram_id'])
    
    # Normalize phone number
    from business_app.utils.validators import normalize_phone_number
    phone = normalize_phone_number(phone)
    
    # Verify telegram user exists
    telegram_user = User.query.filter_by(telegram_id=telegram_id).first()
    if not telegram_user:
        return not_found_response(message='Telegram user not found')
    
    # Verify web user exists with this phone
    web_user = User.query.filter_by(phone=phone).first()
    if not web_user:
        return not_found_response(message='No account found with this phone')
    
    # Verify account can be linked
    if web_user.telegram_id:
        return error_response(
            message='This phone is already linked to another Telegram account',
            status_code=409
        )
    
    # Check status (handle enum or string)
    from business_app.utils.constants import UserStatus
    web_user_status = web_user.status.value if isinstance(web_user.status, UserStatus) else web_user.status
    if web_user_status != 'active':
        return error_response(
            message='The account with this phone is not active',
            status_code=409
        )
    
    # Store linking intent in Redis
    auth_service = get_auth_service()
    link_key = f"phone_link:{telegram_id}"
    link_data = {
        'phone': phone,
        'web_user_id': web_user.id,
        'telegram_user_id': telegram_user.id
    }
    import json
    auth_service.redis_client.setex(link_key, 600, json.dumps(link_data))  # 10 minutes expiry
    
    # Send OTP to the phone
    success = auth_service.send_verification_sms(telegram_user.id, phone)
    
    if success:
        logger.info(f"OTP sent for account linking: telegram_user={telegram_id}, phone={phone}")
        return success_response(
            data={
                'phone_masked': phone[:7] + '****' + phone[-2:] if len(phone) > 9 else '***'
            },
            message='OTP sent successfully'
        )
    else:
        return internal_error_response(message='Failed to send OTP')


@auth_bp.route('/link-phone-account/verify', methods=['POST'])
@rate_limit_by_telegram_id(10, 3600)  # 10 verification attempts per hour
@validate_json(['telegram_id', 'otp'])
@handle_exceptions
@log_request
def link_phone_verify():
    """
    Verify OTP and Link Accounts
    ---
    tags:
      - Authentication
    parameters:
      - in: body
        name: body
        required: true
        schema:
          type: object
          required:
            - telegram_id
            - otp
          properties:
            telegram_id:
              type: integer
              example: 123456789
            otp:
              type: string
              example: "123456"
    responses:
      200:
        description: Accounts linked successfully
      400:
        description: Invalid OTP or no pending link request
    """
    data = request.get_json()
    telegram_id = str(data['telegram_id'])
    otp = data['otp']
    
    auth_service = get_auth_service()
    
    # Get stored linking intent
    link_key = f"phone_link:{telegram_id}"
    link_data_raw = auth_service.redis_client.get(link_key)
    
    if not link_data_raw:
        return not_found_response(message='No pending link request found. Please start again.')
    
    import json
    link_data = json.loads(link_data_raw.decode('utf-8'))
    phone = link_data['phone']
    web_user_id = link_data['web_user_id']
    telegram_user_id = link_data['telegram_user_id']
    
    # Verify OTP
    telegram_user = User.query.get(telegram_user_id)
    if not telegram_user:
        return not_found_response(message='Telegram user not found')
    
    success = auth_service.verify_phone(telegram_user_id, otp)
    
    if not success:
        return error_response(
            message='Invalid OTP. Please try again.',
            status_code=400
        )
    
    # OTP verified - now merge accounts
    web_user = User.query.get(web_user_id)
    if not web_user:
        return not_found_response(message='Web account not found')
    
    # Use cross-platform sync service to link accounts
    from business_app.services.cross_platform_sync_service import cross_platform_sync_service
    
    result = cross_platform_sync_service.auto_link_accounts(
        primary_user=web_user,  # Keep web account as primary (has real email/phone)
        secondary_user=telegram_user,
        link_type='merge'
    )
    
    if not result.get('success'):
        return internal_error_response(message=result.get('error', 'Failed to link accounts'))
    
    # Clean up Redis
    auth_service.redis_client.delete(link_key)
    
    # Generate new tokens for the merged account
    from business_app.services.token_service import TokenService
    token_service = TokenService()
    tokens = token_service.generate_tokens(web_user)
    
    logger.info(f"Successfully linked accounts: telegram_id={telegram_id} -> web_user_id={web_user_id}")
    
    return success_response(
        data={
            'user': web_user.to_dict(),
            'tokens': tokens,
            'linked': True
        },
        message='Accounts linked successfully!'
    )


@auth_bp.route('/sync-profile', methods=['POST'])
@jwt_required()
@handle_exceptions
@log_request  
def sync_profile():
    """
    Sync Profile Information Between Platforms
    ---
    tags:
      - Authentication
    security:
      - Bearer: []
    parameters:
      - in: body
        name: body
        schema:
          type: object
          properties:
            phone:
              type: string
              example: +998901234567
            email:
              type: string
              example: user@example.com
            first_name:
              type: string
              example: John
            last_name:
              type: string
              example: Doe
            preferred_language:
              type: string
              example: en
            sync_source:
              type: string
              enum: [telegram, web]
              example: telegram
    responses:
      200:
        description: Profile synced successfully
    """
    current_user_id = get_jwt_identity()
    data = request.get_json() or {}
    
    user = User.query.get(current_user_id)
    if not user:
        return not_found_response(message=get_translation('user_not_found'))
    
    # Update fields based on sync source priority
    sync_source = data.get('sync_source', 'web')
    updated_fields = []
    
    # Phone numbers must be verified through the proper verification process
    if data.get('phone'):
        # Log attempt to update phone through sync endpoint
        from business_app.utils.audit_logger import audit_logger, AuditEventType, AuditSeverity
        audit_logger.log_event(
            event_type=AuditEventType.SENSITIVE_DATA_ACCESS,
            action="phone_update_blocked_sync_endpoint",
            severity=AuditSeverity.MEDIUM,
            resource_type="user_phone",
            resource_id=str(current_user_id),
            description="Blocked attempt to update phone number through sync endpoint",
            additional_data={
                'attempted_phone': data['phone'], 
                'current_phone': user.phone,
                'sync_source': sync_source
            },
            success=False,
            error_message="Phone number updates must be done through verification process"
        )
        logger.warning(f"User {current_user_id} attempted to update phone through sync endpoint from {sync_source}")
    
    # Only update email if it's not a placeholder telegram email
    if data.get('email') and not user.email.endswith('@bluestream.local'):
        # Don't overwrite real email with telegram placeholder
        pass
    elif data.get('email') and user.email.endswith('@bluestream.local'):
        # Replace placeholder email with real one
        user.email = data['email']
        updated_fields.append('email')
    
    # Update names if source is telegram and current names are empty/placeholder
    if sync_source == 'telegram':
        if data.get('first_name') and (not user.first_name or user.first_name == 'Telegram User'):
            user.first_name = data['first_name']
            updated_fields.append('first_name')
        if data.get('last_name') and not user.last_name:
            user.last_name = data['last_name']
            updated_fields.append('last_name')
    
    # Update language preference
    if data.get('preferred_language'):
        user.preferred_language = data['preferred_language']
        updated_fields.append('preferred_language')
    
    if updated_fields:
        from datetime import datetime, timezone
        user.updated_at = datetime.now(timezone.utc)
        db.session.commit()

    return success_response(
        data={
            'updated_fields': updated_fields,
            'user': {
                'id': user.id,
                'telegram_id': user.telegram_id,
                'email': user.email,
                'phone': user.phone,
                'first_name': user.first_name,
                'last_name': user.last_name,
                'preferred_language': user.preferred_language,
                'registration_source': user.registration_source
            }
        },
        message=get_translation('success.updated')
    )




@auth_bp.route('/admin/create-user', methods=['POST'])
@rate_limit(10, 3600)
@validate_json(['email', 'password', 'first_name', 'last_name', 'role'])
@swag_from({
    'tags': ['Admin'],
    'description': 'Create a new user (admin only)',
    'security': [{'Bearer': []}],
    'parameters': [
        {
            'name': 'body',
            'in': 'body',
            'required': True,
            'schema': {
                'type': 'object',
                'properties': {
                    'email': {'type': 'string', 'example': 'admin@bluestream.com'},
                    'password': {'type': 'string', 'example': 'SecurePassword123'},
                    'first_name': {'type': 'string', 'example': 'John'},
                    'last_name': {'type': 'string', 'example': 'Admin'},
                    'role': {'type': 'string', 'enum': ['admin', 'manager', 'operator', 'delivery_driver'], 'example': 'admin'},
                    'phone': {'type': 'string', 'example': '+998901234567'}
                },
                'required': ['email', 'password', 'first_name', 'last_name', 'role']
            }
        }
    ],
    'responses': {
        '201': {
            'description': 'User created successfully',
            'schema': {
                'type': 'object',
                'properties': {
                    'success': {'type': 'boolean', 'example': True},
                    'message': {'type': 'string', 'example': 'User created successfully'},
                    'user': {
                        'type': 'object',
                        'properties': {
                            'id': {'type': 'integer'},
                            'email': {'type': 'string'},
                            'role': {'type': 'string'},
                            'status': {'type': 'string'}
                        }
                    }
                }
            }
        },
        '400': {'description': 'Validation error'},
        '403': {'description': 'Insufficient permissions'},
        '409': {'description': 'User already exists'}
    }
})
def admin_create_user():
    """Create a new user (admin only)"""
    from business_app.middleware.auth_middleware import admin_required
    from flask_jwt_extended import jwt_required
    
    # Apply decorators manually since we're inside the function
    @jwt_required()
    @admin_required
    def _create_user():
        try:
            data = request.get_json()
            
            # Validate role
            valid_roles = [UserRole.ADMIN.value, UserRole.MANAGER.value, UserRole.OPERATOR.value, UserRole.DELIVERY_DRIVER.value]
            if data['role'] not in valid_roles:
                return {
                    'success': False,
                    'message': f'Invalid role. Must be one of: {", ".join(valid_roles)}'
                }, 400

            # Create user using auth service
            auth_service = get_auth_service()

            try:
                if data['role'] == UserRole.ADMIN.value:
                    user = auth_service.create_admin_user(
                        phone=data['phone'],
                        email=data['email'],
                        password=data['password'],
                        first_name=data['first_name'],
                        last_name=data['last_name']
                    )
                else:
                    # Create regular user with specified role
                    user, tokens = auth_service.register_user(
                        email=data['email'],
                        password=data['password'],
                        phone=data.get('phone', ''),
                        first_name=data['first_name'],
                        last_name=data['last_name'],
                        role=data['role'],
                        status='active',
                        email_verified_at=datetime.now(timezone.utc),
                        is_verified=True
                    )
                
                logger.info(f"Admin created new user: {user.email} with role {user.role}")
                
                return {
                    'success': True,
                    'message': 'User created successfully',
                    'user': {
                        'id': user.id,
                        'email': user.email,
                        'first_name': user.first_name,
                        'last_name': user.last_name,
                        'role': user.role.value if hasattr(user.role, 'value') else user.role,
                        'status': user.status.value if hasattr(user.status, 'value') else user.status,
                        'created_at': user.created_at.isoformat() if user.created_at else None
                    }
                }, 201
                
            except ConflictError as e:
                return {'success': False, 'message': str(e)}, 409
            except ValidationError as e:
                return {'success': False, 'message': str(e), 'errors': e.details}, 400
            
        except Exception as e:
            logger.error(f"Error in admin create user: {e}")
            return {'success': False, 'message': get_translation('error.server_error')}, 500
    
    return _create_user()


@auth_bp.route('/admin/users', methods=['GET'])
@swag_from({
    'tags': ['Admin'],
    'description': 'Get list of all users (admin/manager only)',
    'security': [{'Bearer': []}],
    'parameters': [
        {
            'name': 'page',
            'in': 'query',
            'type': 'integer',
            'default': 1,
            'description': 'Page number'
        },
        {
            'name': 'per_page',
            'in': 'query',
            'type': 'integer',
            'default': 20,
            'description': 'Items per page'
        },
        {
            'name': 'role',
            'in': 'query',
            'type': 'string',
            'description': 'Filter by role'
        },
        {
            'name': 'status',
            'in': 'query',
            'type': 'string',
            'description': 'Filter by status'
        }
    ],
    'responses': {
        '200': {
            'description': 'Users retrieved successfully',
            'schema': {
                'type': 'object',
                'properties': {
                    'success': {'type': 'boolean', 'example': True},
                    'users': {'type': 'array'},
                    'pagination': {'type': 'object'}
                }
            }
        },
        '403': {'description': 'Insufficient permissions'}
    }
})
def admin_get_users():
    """Get list of all users (admin/manager only)"""
    from business_app.middleware.auth_middleware import manager_or_admin_required
    from flask_jwt_extended import jwt_required
    
    @jwt_required()
    @manager_or_admin_required
    def _get_users():
        try:
            page = request.args.get('page', 1, type=int)
            per_page = min(request.args.get('per_page', 20, type=int), 100)
            role_filter = request.args.get('role')
            status_filter = request.args.get('status')
            
            # Build query
            query = User.query
            
            if role_filter:
                query = query.filter(User.role == role_filter)
            
            if status_filter:
                query = query.filter(User.status == status_filter)
            
            # Paginate
            pagination = query.paginate(
                page=page,
                per_page=per_page,
                error_out=False
            )
            
            users = []
            for user in pagination.items:
                users.append({
                    'id': user.id,
                    'email': user.email,
                    'first_name': user.first_name,
                    'last_name': user.last_name,
                    'phone': user.phone,
                    'role': user.role.value if hasattr(user.role, 'value') else user.role,
                    'status': user.status.value if hasattr(user.status, 'value') else user.status,
                    'is_verified': user.is_verified,
                    'email_verified': user.email_verified_at is not None,
                    'phone_verified': user.phone_verified_at is not None,
                    'last_login': user.last_login.isoformat() if user.last_login else None,
                    'created_at': user.created_at.isoformat() if user.created_at else None,
                    'registration_source': user.registration_source,
                    'telegram_id': user.telegram_id
                })
            
            return {
                'success': True,
                'users': users,
                'pagination': {
                    'page': page,
                    'per_page': per_page,
                    'total': pagination.total,
                    'pages': pagination.pages,
                    'has_next': pagination.has_next,
                    'has_prev': pagination.has_prev
                }
            }, 200
            
        except Exception as e:
            logger.error(f"Error in admin get users: {e}")
            return {'success': False, 'message': get_translation('error.server_error')}, 500
    
    return _get_users()


@auth_bp.route('/orders/summary', methods=['GET'])
@jwt_required()
@handle_exceptions
def get_orders_summary():
    """Get user orders summary across all platforms"""
    user_id = get_jwt_identity()
    
    try:
        from business_app.models.order import Order
        from sqlalchemy import func
        
        # Get total orders count
        total_orders = Order.query.filter_by(user_id=user_id).count()
        
        # Get orders by status
        order_stats = db.session.query(
            Order.status,
            func.count(Order.id).label('count')
        ).filter_by(user_id=user_id).group_by(Order.status).all()
        
        # Get orders by platform/source
        platform_stats = db.session.query(
            Order.order_source,
            func.count(Order.id).label('count')
        ).filter_by(user_id=user_id).group_by(Order.order_source).all()
        
        # Get recent orders
        recent_orders = Order.query.filter_by(user_id=user_id).order_by(
            Order.created_at.desc()
        ).limit(5).all()
        
        return success_response(
            data={
                'total_orders': total_orders,
                'order_stats': [{'status': stat.status, 'count': stat.count} for stat in order_stats],
                'platform_stats': [{'platform': stat.order_source, 'count': stat.count} for stat in platform_stats],
                'recent_orders': [{
                    'id': order.id,
                    'order_number': order.order_number,
                    'status': order.status,
                    'platform': order.order_source,
                    'total_amount': float(order.total_amount),
                    'created_at': order.created_at.isoformat()
                } for order in recent_orders]
            }
        )
    except Exception as e:
        logger.error(f"Failed to get orders summary: {e}")
        return internal_error_response(message=get_translation('error.server_error'))


@auth_bp.route('/sync-platform-data', methods=['POST'])
@jwt_required()
@validate_json(['platform'])
@handle_exceptions
def sync_platform_data():
    """Sync user data across platforms"""
    user_id = get_jwt_identity()
    data = request.get_json()
    platform = data.get('platform')
    
    if platform not in ['web', 'telegram']:
        return error_response(message=get_translation('error.forbidden'), status_code=400)
    
    try:
        # Update user's last platform activity
        user = User.query.get(user_id)
        if user:
            user.last_platform_activity = platform
            db.session.commit()
        
        # Sync orders, addresses, and other data
        sync_results = {
            'orders_synced': 0,
            'addresses_synced': 0,
            'profile_synced': True
        }
        
        # In future implementation, this would sync actual data between platforms
        # For now, we'll just return success
        
        return success_response(
            data=sync_results,
            message=get_translation('success.updated')
        )
    except Exception as e:
        logger.error(f"Failed to sync platform data: {e}")
        return internal_error_response(message=get_translation('error.server_error'))


@auth_bp.route('/export-data', methods=['POST'])
@jwt_required()
@rate_limit(max_requests=2, window_seconds=3600, per='user')  # 2 data exports per hour per user
@handle_exceptions
def export_account_data():
    """Export user account data for download"""
    user_id = get_jwt_identity()
    
    try:
        user = User.query.get(user_id)
        if not user:
            return not_found_response(message=get_translation('error.not_found'))
        
        # Collect user data
        from business_app.models.order import Order
        from business_app.models.user import UserAddress
        
        orders = Order.query.filter_by(user_id=user_id).all()
        addresses = UserAddress.query.filter_by(user_id=user_id).all()
        
        export_data = {
            'user_profile': {
                'id': user.id,
                'email': user.email,
                'phone': user.phone,
                'first_name': user.first_name,
                'last_name': user.last_name,
                'date_of_birth': user.date_of_birth.isoformat() if user.date_of_birth else None,
                'gender': user.gender,
                'preferred_language': user.preferred_language,
                'status': user.status.value if hasattr(user.status, 'value') else user.status,
                'registration_source': user.registration_source,
                'created_at': user.created_at.isoformat() if user.created_at else None,
                'email_verified_at': user.email_verified_at.isoformat() if user.email_verified_at else None,
                'phone_verified_at': user.phone_verified_at.isoformat() if user.phone_verified_at else None
            },
            'orders': [{
                'id': order.id,
                'order_number': order.order_number,
                'status': order.status,
                'platform': order.order_source,
                'total_amount': float(order.total_amount),
                'created_at': order.created_at.isoformat()
            } for order in orders],
            'addresses': [{
                'id': addr.id,
                'title': addr.title,
                'full_address': addr.full_address,
                'city': addr.city,
                'is_default': addr.is_default,
                'is_business': addr.is_business
            } for addr in addresses],
            'export_date': db.func.now().isoformat()
        }
        
        import json
        from flask import Response
        
        json_data = json.dumps(export_data, indent=2, ensure_ascii=False)
        
        return Response(
            json_data,
            mimetype='application/json',
            headers={
                'Content-Disposition': f'attachment; filename=account-data-{user_id}.json'
            }
        )
    except Exception as e:
        logger.error(f"Failed to export account data: {e}")
        return internal_error_response(message=get_translation('error.server_error'))




@auth_bp.route('/validate-token', methods=['POST'])
@jwt_required()
@handle_exceptions
def validate_token():
    """Validate current token integrity"""
    try:
        from business_app.services.token_service import TokenService
        token_service = TokenService()
        
        # Get token from header
        auth_header = request.headers.get('Authorization', '')
        if not auth_header.startswith('Bearer '):
            return unauthorized_response(message=get_translation('api.auth.unauthorized'))
        
        token = auth_header.split(' ')[1]
        result = token_service.validate_token_integrity(token)
        
        if result['valid']:
            return success_response(
                data={
                    'user_id': result['user'].id,
                    'email': result['user'].email,
                    'role': result['user'].role,
                    'verified': result['user'].is_verified
                },
                message=get_translation('success.saved')
            )
        else:
            return unauthorized_response(message=result['reason'])
            
    except Exception as e:
        logger.error(f"Token validation failed: {e}")
        return unauthorized_response(message=get_translation('api.auth.token_invalid'))


@auth_bp.route('/sessions', methods=['GET'])
@jwt_required()
@handle_exceptions
def get_user_sessions():
    """Get all active sessions for the current user"""
    user_id = get_jwt_identity()
    
    try:
        from business_app.services.token_service import TokenService
        token_service = TokenService()
        
        sessions = token_service.get_user_sessions(user_id)
        
        # Format sessions for response
        formatted_sessions = []
        for session in sessions:
            formatted_sessions.append({
                'session_id': session.get('session_id'),
                'platform': session.get('platform'),
                'ip_address': session.get('ip'),
                'user_agent': session.get('user_agent'),
                'created_at': session.get('created_at'),
                'last_refresh': session.get('last_refresh'),
                'is_current': session.get('session_id') == get_jwt().get('session_id')
            })
        
        return success_response(
            data={
                'sessions': formatted_sessions,
                'total_sessions': len(formatted_sessions)
            }
        )
        
    except Exception as e:
        logger.error(f"Failed to get user sessions: {e}")
        return internal_error_response(message=get_translation('error.server_error'))


@auth_bp.route('/sessions/<session_id>', methods=['DELETE'])
@jwt_required()
@handle_exceptions
def revoke_session(session_id):
    """Revoke a specific session"""
    user_id = get_jwt_identity()
    current_session_id = get_jwt().get('session_id')
    
    try:
        from business_app.services.token_service import TokenService
        token_service = TokenService()
        
        # Get user sessions to find the target session
        sessions = token_service.get_user_sessions(user_id)
        target_session = None
        
        for session in sessions:
            if session.get('session_id') == session_id:
                target_session = session
                break
        
        if not target_session:
            return not_found_response(message=get_translation('error.not_found'))
        
        # Blacklist tokens for this session with proper expiry
        if 'access_token_jti' in target_session:
            access_expires = current_app.config.get('JWT_ACCESS_TOKEN_EXPIRES', timedelta(hours=1))
            token_service.blacklist_token(target_session['access_token_jti'], expires_delta=access_expires)
        if 'refresh_token_jti' in target_session:
            refresh_expires = current_app.config.get('JWT_REFRESH_TOKEN_EXPIRES', timedelta(days=30))
            token_service.blacklist_token(target_session['refresh_token_jti'], expires_delta=refresh_expires)
        
        # Remove session info
        token_service._remove_session_info(user_id, session_id)
        
        # Check if user revoked their current session
        is_current_session = session_id == current_session_id

        return success_response(
            data={
                'revoked_session_id': session_id,
                'is_current_session': is_current_session
            },
            message=get_translation('success.deleted')
        )
        
    except Exception as e:
        logger.error(f"Failed to revoke session {session_id}: {e}")
        return internal_error_response(message=get_translation('error.server_error'))




@auth_bp.route('/logout', methods=['POST'])
@jwt_required()
@handle_exceptions
def logout():
    """
    User Logout
    ---
    tags:
      - Authentication
    security:
      - bearerAuth: []
    responses:
      200:
        description: Logout successful
        schema:
          type: object
          properties:
            success:
              type: boolean
              example: true
            message:
              type: string
              example: Logout successful
      401:
        description: Unauthorized
    """
    user_id = get_jwt_identity()
    claims = get_jwt()
    jti = claims['jti']
    session_id = claims.get('session_id')

    try:
        from business_app.services.token_service import TokenService
        token_service = TokenService()

        # Blacklist the current token with proper expiry
        # Get the actual token from Authorization header to extract proper expiry
        auth_header = request.headers.get('Authorization', '')
        if auth_header.startswith('Bearer '):
            current_token = auth_header[7:]  # Remove "Bearer " prefix
            blacklisted = token_service.blacklist_token_by_string(current_token)
            logger.info(f"Token blacklisted for user {user_id}: {blacklisted}")
        else:
            # Fallback to JTI with default expiry
            access_expires = current_app.config.get('JWT_ACCESS_TOKEN_EXPIRES', timedelta(hours=1))
            blacklisted = token_service.blacklist_token(jti, expires_delta=access_expires)
            logger.info(f"Token JTI blacklisted for user {user_id}: {blacklisted}")

        # Remove session info if session_id exists
        if session_id:
            session_removed = token_service._remove_session_info(user_id, session_id)
            logger.info(f"Session removed for user {user_id}: {session_removed}")

        # Create response and clear JWT cookies
        # Handle both tuple and Response object returns from success_response
        response_result = success_response(message=get_translation('api.auth.logout_successful'))

        if isinstance(response_result, tuple):
            response = response_result[0]  # Extract response object from tuple
            status_code = response_result[1] if len(response_result) > 1 else 200
        else:
            response = response_result
            status_code = 200

        # Clear JWT cookies - this removes both access and refresh token cookies
        logger.info(f"Clearing JWT cookies for user {user_id}")
        unset_jwt_cookies(response)

        # Also manually clear CSRF cookies (Flask-JWT-Extended only clears JWT cookies)
        # Get cookie configuration from app config
        cookie_domain = current_app.config.get('JWT_COOKIE_DOMAIN', None)
        cookie_path = current_app.config.get('JWT_COOKIE_PATH', '/')
        cookie_secure = current_app.config.get('JWT_COOKIE_SECURE', False)
        cookie_samesite = current_app.config.get('JWT_COOKIE_SAMESITE', 'Lax')

        # Clear CSRF token cookies explicitly
        response.set_cookie(
            'csrf_access_token',
            value='',
            max_age=0,
            expires=0,
            path=cookie_path,
            domain=cookie_domain,
            secure=cookie_secure,
            httponly=False,
            samesite=cookie_samesite
        )
        response.set_cookie(
            'csrf_refresh_token',
            value='',
            max_age=0,
            expires=0,
            path=cookie_path,
            domain=cookie_domain,
            secure=cookie_secure,
            httponly=False,
            samesite=cookie_samesite
        )

        logger.info(f"JWT cookies cleared, response Set-Cookie headers: {response.headers.getlist('Set-Cookie')}")

        logger.info(f"User {user_id} logged out successfully")
        return response, status_code

    except Exception as e:
        logger.error(f"Logout failed for user {user_id}: {e}", exc_info=True)
        return internal_error_response(message=get_translation('error.server_error'))


@auth_bp.route('/logout-all', methods=['POST'])
@auth_bp.route('/sessions/revoke-all', methods=['POST'])  # Alias for backwards compatibility
@jwt_required()
@handle_exceptions
def logout_all():
    """Logout from all sessions or revoke all sessions except current"""
    user_id = get_jwt_identity()
    current_session_id = get_jwt().get('session_id')
    
    # Check if this is a revoke-all request (exclude current session)
    exclude_current = '/sessions/revoke-all' in request.path
    
    try:
        from business_app.services.token_service import TokenService
        token_service = TokenService()
        
        if exclude_current:
            # Revoke all tokens except current session
            success = token_service.revoke_user_tokens(user_id, exclude_session_id=current_session_id)
            message = get_translation('success.deleted') if success else get_translation('error.server_error')
        else:
            # Revoke all user tokens including current
            success = token_service.revoke_user_tokens(user_id)
            message = get_translation('api.auth.logout_successful') if success else get_translation('error.server_error')
        
        if success:
            return success_response(message=message)
        else:
            return internal_error_response(message=message)
            
    except Exception as e:
        logger.error(f"Logout/revoke all failed for user {user_id}: {e}")
        return internal_error_response(message=get_translation('error.server_error'))


@auth_bp.route('/platform-status', methods=['GET'])
@jwt_required_with_refresh()
def get_platform_status():
    """
    Get Cross-Platform Account Status
    ---
    tags:
      - Authentication
    security:
      - bearerAuth: []
    responses:
      200:
        description: Platform status retrieved successfully
        schema:
          type: object
          properties:
            success:
              type: boolean
            data:
              type: object
              properties:
                platform_status:
                  type: object
                linking_suggestions:
                  type: object
    """
    user_id = get_jwt_identity()
    user = User.query.get(user_id)

    if not user:
        return not_found_response(message=get_translation('error.not_found'))

    from business_app.services.cross_platform_sync_service import cross_platform_sync_service
    
    platform_status = cross_platform_sync_service.get_user_platform_status(user)
    linking_suggestions = cross_platform_sync_service.suggest_account_linking(user)

    return success_response(
        data={
            'platform_status': platform_status,
            'linking_suggestions': linking_suggestions
        }
    )


@auth_bp.route('/suggest-auto-link', methods=['POST'])
@jwt_required_with_refresh()
@validate_json(['target_user_id'])
def suggest_auto_link():
    """
    Suggest Automatic Account Linking
    ---
    tags:
      - Authentication
    security:
      - bearerAuth: []
    parameters:
      - in: body
        name: body
        required: true
        schema:
          type: object
          required:
            - target_user_id
          properties:
            target_user_id:
              type: integer
              example: 123
            confirm:
              type: boolean
              example: false
    responses:
      200:
        description: Link suggestion or completion
      400:
        description: Invalid request
      409:
        description: Cannot link accounts
    """
    data = request.get_json()
    current_user_id = get_jwt_identity()
    target_user_id = data['target_user_id']
    confirm = data.get('confirm', False)
    
    current_user = User.query.get(current_user_id)
    target_user = User.query.get(target_user_id)
    
    if not current_user or not target_user:
        return not_found_response(message=get_translation('error.not_found'))
    
    from business_app.services.cross_platform_sync_service import cross_platform_sync_service
    
    if not confirm:
        # Just analyze and return suggestion
        if current_user.registration_source == target_user.registration_source:
            return conflict_response(message=get_translation('error.forbidden'))
        
        return success_response(
            data={
                'link_preview': {
                    'primary_account': {
                        'id': current_user.id,
                        'email': current_user.email,
                        'platform': current_user.registration_source,
                        'name': current_user.full_name
                    },
                    'secondary_account': {
                        'id': target_user.id,
                        'email': target_user.email,
                        'platform': target_user.registration_source,
                        'name': target_user.full_name
                    },
                    'benefits': [
                        'Unified account across all platforms',
                        'Single login for web and Telegram',
                        'Synchronized preferences and data'
                    ]
                }
            },
            message=get_translation('success.saved')
        )
    
    # Perform the linking
    result = cross_platform_sync_service.auto_link_accounts(
        primary_user=current_user,
        secondary_user=target_user,
        link_type='merge'
    )
    
    if result['success']:
        # Generate new tokens for the linked account
        from business_app.services.token_service import TokenService
        token_service = TokenService()
        tokens = token_service.generate_tokens(current_user)
        
        return success_response(
            data={
                'user': current_user.to_dict(),
                'tokens': tokens,
                'link_result': result
            },
            message=result['message']
        )
    else:
        return error_response(
            message=result.get('error', 'Failed to link accounts'),
            status_code=400
        )


@auth_bp.route('/generate-telegram-auth', methods=['POST'])
@jwt_required()
@handle_exceptions
def generate_telegram_auth():
    """
    Generate Telegram Authentication Link/Code for Web User
    ---
    tags:
      - Authentication
    security:
      - bearerAuth: []
    responses:
      200:
        description: Telegram auth code generated successfully
      400:
        description: User already has Telegram access
      404:
        description: User not found
    """
    user_id = get_jwt_identity()
    user = User.query.get(user_id)

    if not user:
        return not_found_response(message=get_translation('error.not_found'))

    # Check if user already has Telegram access
    if user.telegram_id:
        return error_response(
            message=get_translation('error.forbidden'),
            status_code=400
        )
    
    # Generate a secure auth code for Telegram bot linking
    import secrets
    import string
    from datetime import timedelta
    
    auth_code = ''.join(secrets.choice(string.ascii_uppercase + string.digits) for _ in range(8))
    
    # Store the auth code with expiry (5 minutes)
    from business_app.services.token_service import TokenService
    token_service = TokenService()
    
    # Store auth code in Redis with user_id
    auth_data = {
        'user_id': user_id,
        'email': user.email,
        'created_at': datetime.now(timezone.utc).isoformat(),
        'type': 'telegram_auth'
    }
    
    try:
        token_service._ensure_redis_connection()
        if token_service.redis_available:
            import json
            token_service.redis_client.setex(
                f"telegram_auth:{auth_code}",
                300,  # 5 minutes expiry
                json.dumps(auth_data)
            )
        else:
            # Fallback to in-memory storage (not recommended for production)
            if not hasattr(token_service, '_telegram_auth_codes'):
                token_service._telegram_auth_codes = {}
            token_service._telegram_auth_codes[auth_code] = {
                **auth_data,
                'expires_at': datetime.now(timezone.utc) + timedelta(minutes=5)
            }
    except Exception as e:
        logger.error(f"Failed to store telegram auth code: {e}")
        return internal_error_response(message=get_translation('api.auth.error.auth_code_failed'))
    
    # Create Telegram bot link
    bot_username = current_app.config.get('TELEGRAM_BOT_USERNAME', 'bluewaterbot')
    telegram_link = f"https://t.me/{bot_username}?start=auth_{auth_code}"

    return success_response(
        data={
            'auth_code': auth_code,
            'telegram_link': telegram_link,
            'expires_in': 300,  # 5 minutes
            'instructions': [
                f"Click the link to open Telegram: {telegram_link}",
                f"Or manually open @{bot_username} and send: /start auth_{auth_code}",
                "Your accounts will be automatically linked"
            ]
        },
        message=get_translation('success.saved')
    )


@auth_bp.route('/generate-web-auth', methods=['POST'])
@rate_limit(5, 300)  # 5 attempts per 5 minutes
@validate_json(['telegram_id'])
@handle_exceptions
def generate_web_auth():
    """
    Generate Web Authentication Token for Telegram User
    ---
    tags:
      - Authentication
    parameters:
      - in: body
        name: body
        required: true
        schema:
          type: object
          required:
            - telegram_id
          properties:
            telegram_id:
              type: string
              example: "123456789"
    responses:
      200:
        description: Web auth token generated successfully
      404:
        description: Telegram user not found
      400:
        description: User already has web access
    """
    data = request.get_json()
    telegram_id = str(data['telegram_id'])
    
    # Find telegram user
    user = User.query.filter_by(telegram_id=telegram_id).first()
    if not user:
        return not_found_response(message=get_translation('error.not_found'))
    
    # Check if user already has proper web access
    if user.email and not user.email.startswith('telegram_') and user.password_hash != 'telegram_user':
        return error_response(
            message=get_translation('error.forbidden'),
            status_code=400
        )
    
    # Generate secure temporary web auth token
    from business_app.services.token_service import TokenService
    token_service = TokenService()
    
    # Generate temporary access tokens for web login
    temp_tokens = token_service.generate_tokens(user)
    
    # Create a secure one-time web auth link
    import secrets
    web_auth_token = secrets.token_urlsafe(32)
    
    # Store the web auth token
    auth_data = {
        'user_id': user.id,
        'telegram_id': telegram_id,
        'access_token': temp_tokens['access_token'],
        'refresh_token': temp_tokens['refresh_token'],
        'created_at': datetime.now(timezone.utc).isoformat(),
        'type': 'web_auth'
    }
    
    try:
        token_service._ensure_redis_connection()
        if token_service.redis_available:
            import json
            token_service.redis_client.setex(
                f"web_auth:{web_auth_token}",
                600,  # 10 minutes expiry
                json.dumps(auth_data)
            )
        else:
            # Fallback to in-memory storage
            if not hasattr(token_service, '_web_auth_tokens'):
                token_service._web_auth_tokens = {}
            token_service._web_auth_tokens[web_auth_token] = {
                **auth_data,
                'expires_at': datetime.now(timezone.utc) + timedelta(minutes=10)
            }
    except Exception as e:
        logger.error(f"Failed to store web auth token: {e}")
        return internal_error_response(message=get_translation('api.auth.error.web_token_failed'))
    
    # Create web app authentication link
    web_app_url = current_app.config.get('WEB_APP_URL', 'https://bluestream.uz')
    web_auth_link = f"{web_app_url}/auth/telegram-login?token={web_auth_token}"

    return success_response(
        data={
            'auth_token': web_auth_token,
            'web_auth_link': web_auth_link,
            'expires_in': 600,  # 10 minutes
            'instructions': [
                f"Click this link to access the web app: {web_auth_link}",
                "You'll be automatically logged in",
                "Link expires in 10 minutes for security"
            ]
        },
        message=get_translation('success.saved')
    )


@auth_bp.route('/verify-telegram-auth/<auth_code>', methods=['POST'])
@rate_limit(10, 300)  # 10 attempts per 5 minutes
@handle_exceptions
def verify_telegram_auth(auth_code):
    """
    Verify Telegram Authentication Code and Link Account
    ---
    tags:
      - Authentication
    parameters:
      - in: path
        name: auth_code
        type: string
        required: true
        description: The authentication code from /generate-telegram-auth
      - in: body
        name: body
        required: true
        schema:
          type: object
          required:
            - telegram_id
            - telegram_username
          properties:
            telegram_id:
              type: string
              example: "123456789"
            telegram_username:
              type: string
              example: "username"
            first_name:
              type: string
              example: "John"
            last_name:
              type: string
              example: "Doe"
    responses:
      200:
        description: Accounts linked successfully
      400:
        description: Invalid or expired auth code
      409:
        description: Telegram ID already linked
    """
    data = request.get_json()
    
    # Retrieve auth code data
    from business_app.services.token_service import TokenService
    token_service = TokenService()
    
    auth_data = None
    try:
        token_service._ensure_redis_connection()
        if token_service.redis_available:
            import json
            auth_data_str = token_service.redis_client.get(f"telegram_auth:{auth_code}")
            if auth_data_str:
                auth_data = json.loads(auth_data_str)
                # Delete the used code
                token_service.redis_client.delete(f"telegram_auth:{auth_code}")
        else:
            # Check in-memory storage
            if hasattr(token_service, '_telegram_auth_codes') and auth_code in token_service._telegram_auth_codes:
                stored_data = token_service._telegram_auth_codes[auth_code]
                if datetime.now(timezone.utc) < stored_data['expires_at']:
                    auth_data = stored_data
                # Delete used code
                del token_service._telegram_auth_codes[auth_code]
    except Exception as e:
        logger.error(f"Failed to retrieve auth code: {e}")
    
    if not auth_data:
        return error_response(
            message=get_translation('api.auth.token_expired'),
            status_code=400
        )
    
    # Get the web user
    user = User.query.get(auth_data['user_id'])
    if not user:
        return not_found_response(message=get_translation('error.not_found'))
    
    telegram_id = str(data['telegram_id'])
    
    # Check if this telegram_id is already linked to another account
    existing_telegram_user = User.query.filter_by(telegram_id=telegram_id).first()
    if existing_telegram_user and existing_telegram_user.id != user.id:
        return conflict_response(message=get_translation('api.auth.email_already_exists'))
    
    # Link the telegram account to web user
    user.telegram_id = telegram_id
    user.telegram_username = data.get('telegram_username')
    user.is_bot_active = True
    user.last_bot_interaction = datetime.now(timezone.utc)
    
    # Update name if web user has incomplete info
    if data.get('first_name') and not user.first_name:
        user.first_name = data['first_name']
    if data.get('last_name') and not user.last_name:
        user.last_name = data['last_name']
    
    db.session.commit()
    
    logger.info(f"Successfully linked Telegram account {telegram_id} to web user {user.id}")

    return success_response(
        data={
            'user': user.to_dict(),
            'linked_platforms': ['web', 'telegram']
        },
        message=get_translation('success.saved')
    )


@auth_bp.route('/verify-web-auth/<auth_token>', methods=['GET'])
@rate_limit(10, 600)  # 10 attempts per 10 minutes
@handle_exceptions
def verify_web_auth(auth_token):
    """
    Verify Web Authentication Token and Return Login Tokens
    ---
    tags:
      - Authentication
    parameters:
      - in: path
        name: auth_token
        type: string
        required: true
        description: The authentication token from /generate-web-auth
    responses:
      200:
        description: Authentication successful, returns tokens
      400:
        description: Invalid or expired auth token
    """
    # Retrieve auth token data
    from business_app.services.token_service import TokenService
    token_service = TokenService()
    
    auth_data = None
    try:
        token_service._ensure_redis_connection()
        if token_service.redis_available:
            import json
            auth_data_str = token_service.redis_client.get(f"web_auth:{auth_token}")
            if auth_data_str:
                auth_data = json.loads(auth_data_str)
                # Delete the used token
                token_service.redis_client.delete(f"web_auth:{auth_token}")
        else:
            # Check in-memory storage
            if hasattr(token_service, '_web_auth_tokens') and auth_token in token_service._web_auth_tokens:
                stored_data = token_service._web_auth_tokens[auth_token]
                if datetime.now(timezone.utc) < stored_data['expires_at']:
                    auth_data = stored_data
                # Delete used token
                del token_service._web_auth_tokens[auth_token]
    except Exception as e:
        logger.error(f"Failed to retrieve web auth token: {e}")
    
    if not auth_data:
        return error_response(
            message=get_translation('api.auth.token_expired'),
            status_code=400
        )
    
    # Get the user
    user = User.query.get(auth_data['user_id'])
    if not user:
        return not_found_response(message=get_translation('error.not_found'))
    
    # Return the authentication tokens
    return success_response(
        data={
            'user': user.to_dict(),
            'tokens': {
                'access_token': auth_data['access_token'],
                'refresh_token': auth_data['refresh_token']
            },
            'linked_platforms': ['telegram', 'web'] if user.email and not user.email.startswith('telegram_') else ['telegram']
        },
        message=get_translation('api.auth.login_successful')
    )
  