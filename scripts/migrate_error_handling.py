#!/usr/bin/env python3
"""
Script to migrate existing API endpoints to use standardized error handling
"""
import os
import re
import glob
from pathlib import Path

def find_api_files():
    """Find all API files that need to be updated"""
    api_dir = Path(__file__).parent.parent / "business_app" / "api"
    return list(api_dir.glob("*.py"))

def analyze_current_error_patterns(file_path):
    """Analyze current error handling patterns in a file"""
    with open(file_path, 'r') as f:
        content = f.read()
    
    patterns = {
        'raw_try_except': len(re.findall(r'try:\s*\n.*?except\s+Exception', content, re.DOTALL)),
        'manual_jsonify_errors': len(re.findall(r'return\s+jsonify\(\s*\{[^}]*[\'"]error[\'"]', content)),
        'handle_exceptions_decorator': len(re.findall(r'@handle_exceptions', content)),
        'raw_status_codes': len(re.findall(r'return.*?,\s*[45]\d\d', content)),
    }
    
    return patterns

def suggest_refactoring(file_path):
    """Suggest refactoring for a specific file"""
    with open(file_path, 'r') as f:
        content = f.read()
    
    suggestions = []
    
    # Find functions with raw try-except blocks
    raw_exceptions = re.finditer(
        r'def\s+(\w+)\([^)]*\):[^}]*?try:\s*\n(.*?)except\s+Exception\s+as\s+\w+:(.*?)return\s+jsonify\([^}]*\{[^}]*[\'"]error[\'"]',
        content, 
        re.DOTALL
    )
    
    for match in raw_exceptions:
        func_name = match.group(1)
        suggestions.append({
            'function': func_name,
            'issue': 'Raw exception handling',
            'recommendation': 'Use @handle_api_exception decorator and raise specific exceptions',
            'line_start': content[:match.start()].count('\n') + 1
        })
    
    # Find manual error responses
    manual_errors = re.finditer(
        r'return\s+jsonify\(\s*\{[^}]*[\'"]error[\'"]:\s*[\'"]([^"\']+)[\'"]',
        content
    )
    
    for match in manual_errors:
        error_msg = match.group(1)
        suggestions.append({
            'issue': 'Manual error response',
            'error_message': error_msg,
            'recommendation': 'Raise appropriate exception instead of manual error response',
            'line_start': content[:match.start()].count('\n') + 1
        })
    
    return suggestions

def generate_migration_recommendations():
    """Generate comprehensive migration recommendations"""
    api_files = find_api_files()
    
    print("BlueStream API Error Handling Migration Analysis")
    print("=" * 60)
    
    total_issues = 0
    files_analyzed = 0
    
    for file_path in api_files:
        if file_path.name == "__init__.py":
            continue
            
        files_analyzed += 1
        patterns = analyze_current_error_patterns(file_path)
        suggestions = suggest_refactoring(file_path)
        
        file_issues = sum(patterns.values())
        total_issues += file_issues
        
        if file_issues > 0:
            print(f"\n📁 {file_path.name}")
            print(f"   Raw try-except blocks: {patterns['raw_try_except']}")
            print(f"   Manual error responses: {patterns['manual_jsonify_errors']}")
            print(f"   Using handle_exceptions: {patterns['handle_exceptions_decorator']}")
            print(f"   Raw status codes: {patterns['raw_status_codes']}")
            
            if suggestions:
                print(f"   Specific recommendations:")
                for suggestion in suggestions[:3]:  # Show first 3 suggestions
                    print(f"     • Line {suggestion.get('line_start', '?')}: {suggestion['recommendation']}")
                
                if len(suggestions) > 3:
                    print(f"     • ... and {len(suggestions) - 3} more issues")
    
    print(f"\n📊 Summary")
    print(f"   Files analyzed: {files_analyzed}")
    print(f"   Total issues found: {total_issues}")
    print(f"   Estimated effort: {estimate_migration_effort(total_issues)}")
    
    print(f"\n🔧 Migration Steps")
    print_migration_steps()

def estimate_migration_effort(total_issues):
    """Estimate migration effort based on issues found"""
    if total_issues < 10:
        return "Low (1-2 hours)"
    elif total_issues < 30:
        return "Medium (3-5 hours)"
    elif total_issues < 60:
        return "High (1-2 days)"
    else:
        return "Very High (2+ days)"

def print_migration_steps():
    """Print detailed migration steps"""
    steps = [
        "1. Import new error handlers in each API file:",
        "   from business_app.utils.error_handlers import handle_api_exception, create_success_response",
        "   from business_app.utils.exceptions import ValidationError, NotFoundError, etc.",
        "",
        "2. Replace @handle_exceptions with @handle_api_exception",
        "",
        "3. Replace try-except blocks with specific exception raises:",
        "   # Before:",
        "   try:",
        "       # ... logic ...",
        "   except Exception as e:",
        "       return jsonify({'error': 'Failed'}), 500",
        "",
        "   # After:",
        "   # ... logic ... (let @handle_api_exception catch exceptions)",
        "   # If validation fails: raise ValidationError('Invalid input')",
        "",
        "4. Replace manual error responses with exception raises:",
        "   # Before:",
        "   return jsonify({'error': 'Not found'}), 404",
        "",
        "   # After:",
        "   raise NotFoundError('Resource not found')",
        "",
        "5. Use create_success_response for consistent success responses:",
        "   # Before:",
        "   return jsonify({'success': True, 'data': result})",
        "",
        "   # After:",
        "   return create_success_response(data=result, message='Operation successful')",
        "",
        "6. Add specific exception handling decorators where needed:",
        "   @handle_database_exceptions  # For DB operations",
        "   @handle_external_service_exceptions('payment_gateway')  # For external calls",
    ]
    
    for step in steps:
        print(f"   {step}")

def create_example_migration():
    """Create an example of before/after migration"""
    print(f"\n📝 Example Migration")
    print("=" * 40)
    
    before = '''
# BEFORE - Inconsistent error handling
@auth_bp.route('/login', methods=['POST'])
@validate_json(['email', 'password'])
def login():
    try:
        data = request.get_json()
        user = auth_service.authenticate_user(data['email'], data['password'])
        if not user:
            return jsonify({'error': 'Invalid credentials'}), 401
        
        token = create_access_token(identity=user.id)
        return jsonify({
            'success': True,
            'access_token': token,
            'user': user.to_dict()
        })
    except Exception as e:
        current_app.logger.error(f"Login error: {e}")
        return jsonify({'error': 'Login failed'}), 500
'''
    
    after = '''
# AFTER - Standardized error handling
@auth_bp.route('/login', methods=['POST'])
@handle_api_exception
@validate_json(['email', 'password'])
def login():
    data = request.get_json()
    user = auth_service.authenticate_user(data['email'], data['password'])
    
    if not user:
        raise UnauthorizedError('Invalid credentials', details={'field': 'email_password'})
    
    token = create_access_token(identity=user.id)
    return create_success_response(
        data={
            'access_token': token,
            'user': user.to_dict()
        },
        message='Login successful'
    )
'''
    
    print("BEFORE:")
    print(before)
    print("AFTER:")
    print(after)

def check_specific_file(file_path):
    """Analyze a specific file in detail"""
    if not os.path.exists(file_path):
        print(f"❌ File not found: {file_path}")
        return
    
    print(f"🔍 Detailed analysis of {os.path.basename(file_path)}")
    print("=" * 50)
    
    patterns = analyze_current_error_patterns(file_path)
    suggestions = suggest_refactoring(file_path)
    
    print(f"Current error handling patterns:")
    for pattern, count in patterns.items():
        if count > 0:
            print(f"  • {pattern.replace('_', ' ').title()}: {count}")
    
    if suggestions:
        print(f"\nSpecific recommendations:")
        for i, suggestion in enumerate(suggestions, 1):
            print(f"\n{i}. {suggestion['issue']}")
            print(f"   Line: {suggestion.get('line_start', 'Unknown')}")
            print(f"   Recommendation: {suggestion['recommendation']}")
            if 'error_message' in suggestion:
                print(f"   Current error: '{suggestion['error_message']}'")
    
    print(f"\n💡 Quick fixes for this file:")
    print(f"   1. Add @handle_api_exception decorator to all endpoint functions")
    print(f"   2. Replace {patterns['manual_jsonify_errors']} manual error responses with exception raises")
    print(f"   3. Remove {patterns['raw_try_except']} raw try-except blocks")

if __name__ == "__main__":
    import sys
    
    if len(sys.argv) > 1:
        # Analyze specific file
        file_path = sys.argv[1]
        check_specific_file(file_path)
    else:
        # Generate full migration report
        generate_migration_recommendations()
        create_example_migration()
        
        print(f"\n🎯 Next Steps:")
        print(f"   1. Run this script on specific files: python migrate_error_handling.py <file_path>")
        print(f"   2. Start migration with the files that have the most issues")
        print(f"   3. Test each API endpoint after migration")
        print(f"   4. Update API documentation to reflect new error response format")