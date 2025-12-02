"""
Data validation utilities for the Water Business Platform
"""
import re
from datetime import datetime, date
from typing import Any, Dict, List, Optional, Union
from decimal import Decimal, InvalidOperation
import phonenumbers
from phonenumbers import NumberParseException

from business_app.utils.exceptions import ValidationError
from business_app.utils.constants import PATTERNS, BUSINESS_RULES


class Validator:
    """Base validator class"""
    
    def __init__(self, value: Any, field_name: str = "field"):
        self.value = value
        self.field_name = field_name
        self.errors = []
    
    def required(self, message: str = None):
        """Check if value is required"""
        if self.value is None or self.value == "" or (isinstance(self.value, str) and not self.value.strip()):
            message = message or f"{self.field_name} is required"
            self.errors.append(message)
        return self
    
    def min_length(self, min_len: int, message: str = None):
        """Check minimum length"""
        if self.value and len(str(self.value)) < min_len:
            message = message or f"{self.field_name} must be at least {min_len} characters long"
            self.errors.append(message)
        return self
    
    def max_length(self, max_len: int, message: str = None):
        """Check maximum length"""
        if self.value and len(str(self.value)) > max_len:
            message = message or f"{self.field_name} must be no more than {max_len} characters long"
            self.errors.append(message)
        return self
    
    def pattern(self, regex_pattern: str, message: str = None):
        """Check regex pattern"""
        if self.value and not re.match(regex_pattern, str(self.value)):
            message = message or f"{self.field_name} has invalid format"
            self.errors.append(message)
        return self
    
    def one_of(self, allowed_values: List[Any], message: str = None):
        """Check if value is in allowed list"""
        if self.value and self.value not in allowed_values:
            message = message or f"{self.field_name} must be one of: {', '.join(map(str, allowed_values))}"
            self.errors.append(message)
        return self
    
    def is_valid(self) -> bool:
        """Check if validation passed"""
        return len(self.errors) == 0
    
    def get_errors(self) -> List[str]:
        """Get validation errors"""
        return self.errors


class EmailValidator(Validator):
    """Email validation"""
    
    def validate(self):
        """Validate email format"""
        if self.value:
            if not re.match(PATTERNS['EMAIL'], self.value):
                self.errors.append(f"{self.field_name} must be a valid email address")
            
            # Check for common typos
            common_domains = ['gmail.com', 'yahoo.com', 'outlook.com', 'mail.ru']
            domain = self.value.split('@')[1] if '@' in self.value else ''
            
            # Simple typo detection
            if domain and domain not in common_domains:
                similar_domains = [d for d in common_domains if abs(len(d) - len(domain)) <= 2]
                if similar_domains:
                    self.errors.append(f"Did you mean one of these domains: {', '.join(similar_domains)}?")
        
        return self


class PhoneValidator(Validator):
    """Phone number validation"""
    
    def __init__(self, value: Any, field_name: str = "phone", region: str = 'UZ'):
        super().__init__(value, field_name)
        self.region = region
    
    def validate(self):
        """Validate phone number"""
        if self.value:
            try:
                # Clean phone number
                cleaned_phone = re.sub(r'\D', '', str(self.value))
                
                # Parse phone number
                parsed_number = phonenumbers.parse(self.value, self.region)
                
                # Check if valid
                if not phonenumbers.is_valid_number(parsed_number):
                    self.errors.append(f"{self.field_name} is not a valid phone number")
                
                # Check if it's a mobile number (for SMS)
                number_type = phonenumbers.number_type(parsed_number)
                if number_type not in [phonenumbers.PhoneNumberType.MOBILE, 
                                     phonenumbers.PhoneNumberType.FIXED_LINE_OR_MOBILE]:
                    self.errors.append(f"{self.field_name} must be a mobile number")
                
                # Specific validation for Uzbekistan numbers
                if self.region == 'UZ':
                    if not cleaned_phone.startswith('998') or len(cleaned_phone) != 12:
                        self.errors.append(f"{self.field_name} must be a valid Uzbek phone number")
                
            except NumberParseException as e:
                self.errors.append(f"{self.field_name} is not a valid phone number: {e}")
        
        return self


class PasswordValidator(Validator):
    """Password validation"""
    
    def validate(self, min_length: int = None):
        """Validate password strength"""
        if min_length is None:
            min_length = BUSINESS_RULES.get('PASSWORD_MIN_LENGTH', 8)
        
        if self.value:
            password = str(self.value)
            
            # Length check
            if len(password) < min_length:
                self.errors.append(f"{self.field_name} must be at least {min_length} characters long")
            
            # Complexity checks
            if not re.search(r'[a-z]', password):
                self.errors.append(f"{self.field_name} must contain at least one lowercase letter")
            
            if not re.search(r'[A-Z]', password):
                self.errors.append(f"{self.field_name} must contain at least one uppercase letter")
            
            if not re.search(r'\d', password):
                self.errors.append(f"{self.field_name} must contain at least one digit")
            
            # Check for common weak passwords
            weak_passwords = [
                'password', '123456', 'qwerty', 'abc123', 'password123',
                'admin', 'user', 'guest', '111111', '000000'
            ]
            if password.lower() in weak_passwords:
                self.errors.append(f"{self.field_name} is too common and weak")
        
        return self


class CoordinateValidator(Validator):
    """Geographic coordinate validation"""
    
    def validate_latitude(self):
        """Validate latitude"""
        if self.value is not None:
            try:
                lat = float(self.value)
                if not (-90 <= lat <= 90):
                    self.errors.append(f"{self.field_name} must be between -90 and 90")
            except (ValueError, TypeError):
                self.errors.append(f"{self.field_name} must be a valid number")
        return self
    
    def validate_longitude(self):
        """Validate longitude"""
        if self.value is not None:
            try:
                lon = float(self.value)
                if not (-180 <= lon <= 180):
                    self.errors.append(f"{self.field_name} must be between -180 and 180")
            except (ValueError, TypeError):
                self.errors.append(f"{self.field_name} must be a valid number")
        return self


class DateValidator(Validator):
    """Date validation"""
    
    def validate_date(self, date_format: str = '%Y-%m-%d'):
        """Validate date format"""
        if self.value:
            try:
                if isinstance(self.value, str):
                    datetime.strptime(self.value, date_format)
                elif not isinstance(self.value, (date, datetime)):
                    self.errors.append(f"{self.field_name} must be a valid date")
            except ValueError:
                self.errors.append(f"{self.field_name} must be in format {date_format}")
        return self
    
    def future_date(self, message: str = None):
        """Check if date is in the future"""
        if self.value:
            try:
                check_date = self.value
                if isinstance(self.value, str):
                    check_date = datetime.strptime(self.value, '%Y-%m-%d').date()
                elif isinstance(self.value, datetime):
                    check_date = self.value.date()
                
                if check_date <= date.today():
                    message = message or f"{self.field_name} must be a future date"
                    self.errors.append(message)
            except (ValueError, TypeError):
                self.errors.append(f"{self.field_name} must be a valid date")
        return self
    
    def past_date(self, message: str = None):
        """Check if date is in the past"""
        if self.value:
            try:
                check_date = self.value
                if isinstance(self.value, str):
                    check_date = datetime.strptime(self.value, '%Y-%m-%d').date()
                elif isinstance(self.value, datetime):
                    check_date = self.value.date()
                
                if check_date >= date.today():
                    message = message or f"{self.field_name} must be a past date"
                    self.errors.append(message)
            except (ValueError, TypeError):
                self.errors.append(f"{self.field_name} must be a valid date")
        return self


class NumericValidator(Validator):
    """Numeric validation"""
    
    def min_value(self, min_val: Union[int, float], message: str = None):
        """Check minimum value"""
        if self.value is not None:
            try:
                num_value = float(self.value)
                if num_value < min_val:
                    message = message or f"{self.field_name} must be at least {min_val}"
                    self.errors.append(message)
            except (ValueError, TypeError):
                self.errors.append(f"{self.field_name} must be a valid number")
        return self
    
    def max_value(self, max_val: Union[int, float], message: str = None):
        """Check maximum value"""
        if self.value is not None:
            try:
                num_value = float(self.value)
                if num_value > max_val:
                    message = message or f"{self.field_name} must be no more than {max_val}"
                    self.errors.append(message)
            except (ValueError, TypeError):
                self.errors.append(f"{self.field_name} must be a valid number")
        return self
    
    def positive(self, message: str = None):
        """Check if value is positive"""
        if self.value is not None:
            try:
                num_value = float(self.value)
                if num_value <= 0:
                    message = message or f"{self.field_name} must be positive"
                    self.errors.append(message)
            except (ValueError, TypeError):
                self.errors.append(f"{self.field_name} must be a valid number")
        return self
    
    def integer(self, message: str = None):
        """Check if value is integer"""
        if self.value is not None:
            try:
                int(self.value)
            except (ValueError, TypeError):
                message = message or f"{self.field_name} must be an integer"
                self.errors.append(message)
        return self


class OrderValidator:
    """Order-specific validation"""
    
    @staticmethod
    def validate_order_data(data: Dict[str, Any]) -> List[str]:
        """Validate order data"""
        errors = []
        
        # Required fields
        required_fields = ['items', 'delivery_address']
        for field in required_fields:
            if field not in data or not data[field]:
                errors.append(f"{field} is required")
        
        # Validate items
        if 'items' in data and data['items']:
            if not isinstance(data['items'], list):
                errors.append("items must be a list")
            else:
                for i, item in enumerate(data['items']):
                    if not isinstance(item, dict):
                        errors.append(f"Item {i+1} must be an object")
                        continue
                    
                    if 'product_id' not in item:
                        errors.append(f"Item {i+1} must have product_id")
                    
                    if 'quantity' not in item:
                        errors.append(f"Item {i+1} must have quantity")
                    else:
                        try:
                            quantity = int(item['quantity'])
                            if quantity <= 0:
                                errors.append(f"Item {i+1} quantity must be positive")
                            if quantity > BUSINESS_RULES['MAX_ORDER_ITEMS']:
                                errors.append(f"Item {i+1} quantity exceeds maximum allowed")
                        except (ValueError, TypeError):
                            errors.append(f"Item {i+1} quantity must be a valid number")
        
        # Validate delivery address
        if 'delivery_address' in data and data['delivery_address']:
            address = data['delivery_address']
            address_fields = ['street', 'city', 'latitude', 'longitude']
            for field in address_fields:
                if field not in address or not address[field]:
                    errors.append(f"delivery_address.{field} is required")
            
            # Validate coordinates
            if 'latitude' in address:
                CoordinateValidator(address['latitude'], 'delivery_address.latitude').validate_latitude()
            
            if 'longitude' in address:
                CoordinateValidator(address['longitude'], 'delivery_address.longitude').validate_longitude()
        
        return errors


class PaymentValidator:
    """Payment-specific validation"""
    
    @staticmethod
    def validate_payment_data(data: Dict[str, Any]) -> List[str]:
        """Validate payment data"""
        errors = []
        
        # Required fields
        if 'amount' not in data:
            errors.append("amount is required")
        else:
            NumericValidator(data['amount'], 'amount').positive().min_value(BUSINESS_RULES['MIN_ORDER_AMOUNT'])
        
        if 'payment_method' not in data:
            errors.append("payment_method is required")
        else:
            from .constants import PaymentMethod
            valid_methods = [method.value for method in PaymentMethod]
            if data['payment_method'] not in valid_methods:
                errors.append(f"payment_method must be one of: {', '.join(valid_methods)}")
        
        return errors


def validate_data(data: Dict[str, Any], validation_rules: Dict[str, List]) -> Dict[str, List[str]]:
    """Generic data validation function"""
    errors = {}
    
    for field, rules in validation_rules.items():
        field_errors = []
        value = data.get(field)
        
        for rule in rules:
            if callable(rule):
                try:
                    rule_errors = rule(value, field)
                    if rule_errors:
                        field_errors.extend(rule_errors)
                except Exception as e:
                    field_errors.append(f"Validation error: {str(e)}")
        
        if field_errors:
            errors[field] = field_errors
    
    return errors


def required_validator(value: Any, field: str) -> List[str]:
    """Required field validator"""
    return Validator(value, field).required().get_errors()


def email_validator(value: Any, field: str) -> List[str]:
    """Email validator"""
    return EmailValidator(value, field).validate().get_errors()


def phone_validator(value: Any) -> List[str]:
    """Phone validator"""
    return PhoneValidator(value).validate().get_errors()


def password_validator(value: Any, field: str) -> List[str]:
    """Password validator"""
    return PasswordValidator(value, field).validate().get_errors()