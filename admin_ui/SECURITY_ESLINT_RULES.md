# ESLint Security Rules - Admin UI

This document describes the ESLint security rules implemented to prevent XSS and other security vulnerabilities in the admin UI.

## Security Rules Enabled

### Critical Security Rules (Always Error)

1. **`no-eval`** - Prevents `eval()` usage which can execute arbitrary code
2. **`no-implied-eval`** - Prevents `setTimeout`/`setInterval` with strings
3. **`no-new-func`** - Prevents `Function` constructor usage
4. **`no-script-url`** - Prevents `javascript:` URLs
5. **`no-global-assign`** - Prevents assignment to global variables
6. **`no-implicit-globals`** - Prevents implicit global variables
7. **`no-proto`** - Prevents `__proto__` usage

### React Security Rules

1. **`react/no-danger`** - Prevents `dangerouslySetInnerHTML` usage
2. **`react/no-danger-with-children`** - Prevents `dangerouslySetInnerHTML` with children
3. **`react/jsx-no-script-url`** - Prevents `javascript:` URLs in JSX
4. **`react/jsx-no-target-blank`** - Prevents unsafe `target="_blank"`

### Security Plugin Rules

1. **`security/detect-object-injection`** - Detects object injection vulnerabilities
2. **`security/detect-non-literal-regexp`** - Warns about dynamic regex patterns
3. **`security/detect-eval-with-expression`** - Detects eval with expressions
4. **`security/detect-unsafe-regex`** - Detects potentially unsafe regex patterns
5. **`security/detect-buffer-noassert`** - Prevents unsafe buffer usage
6. **`security/detect-child-process`** - Prevents child process usage
7. **`security/detect-new-buffer`** - Prevents deprecated Buffer constructor

## Common Security Issues Fixed

### Object Injection Prevention

**Before (Vulnerable):**
```javascript
// Potential XSS through object injection
return permissions[permission] === true;
```

**After (Secure):**
```javascript
// Safe object property access
if (typeof permission !== 'string' || permission.includes('__proto__') || permission.includes('constructor')) {
  return false;
}
return Object.prototype.hasOwnProperty.call(permissions, permission) && permissions[permission] === true;
```

### Function Hoisting Issues

**Before (Error-prone):**
```javascript
// Function used before definition
onClick: () => handleViewUser(record)

// Function defined later
const handleViewUser = (user) => { ... }
```

**After (Correct):**
```javascript
// Function defined first
const handleViewUser = (user) => { ... }

// Then used
onClick: () => handleViewUser(record)
```

### Unused Variables Cleanup

**Before:**
```javascript
import React, { useState } from 'react';
import { Button, Form } from 'antd'; // Form not used
```

**After:**
```javascript
import React, { useState } from 'react';
import { Button } from 'antd';
```

## Security Benefits

1. **XSS Prevention**: Rules prevent common XSS attack vectors
2. **Code Injection Prevention**: Blocks eval and dynamic code execution
3. **Object Injection Prevention**: Prevents prototype pollution attacks
4. **Unsafe HTML Prevention**: Blocks dangerous innerHTML usage
5. **URL Safety**: Prevents javascript: URLs and unsafe links

## Development Workflow

### Running ESLint

```bash
# Check for issues
npm run lint

# Auto-fix what can be fixed
npm run lint:fix

# Run specific security checks
npx eslint src --ext .js,.jsx --rule 'security/detect-object-injection: error'
```

### Handling Security Warnings

1. **Error Level Issues**: Must be fixed before deployment
2. **Warning Level Issues**: Should be reviewed and ideally fixed
3. **Security Plugin Issues**: Always take seriously, never disable

### Exceptions and Overrides

If you absolutely must disable a security rule:

```javascript
// Disable specific rule with justification
// eslint-disable-next-line security/detect-object-injection
const value = obj[userInput]; // Safe because userInput is validated above
```

**Never disable these rules globally:**
- `no-eval`
- `react/no-danger`
- `security/detect-object-injection`
- `security/detect-eval-with-expression`

## Configuration Files

- **`.eslintrc.js`** - Main ESLint configuration
- **`package.json`** - Dependencies for security plugins
- **`SECURITY_ESLINT_RULES.md`** - This documentation

## Security Plugins Used

1. **eslint-plugin-security**: Detects security anti-patterns
2. **eslint-plugin-react-hooks**: Prevents React hooks misuse
3. **ESLint core rules**: Built-in security rules

## Continuous Integration

ESLint security checks should be part of the CI/CD pipeline:

```yaml
# Example GitHub Actions step
- name: Run ESLint Security Check
  run: |
    npm run lint
    # Fail build if security errors found
```

## Training and Awareness

### Common Mistakes to Avoid

1. **Dynamic Property Access**: Always validate object keys
2. **String Concatenation for HTML**: Use proper templating
3. **User Input in URLs**: Validate and sanitize
4. **Eval Usage**: Never use eval() with user input
5. **Prototype Pollution**: Check for __proto__ and constructor

### Best Practices

1. **Input Validation**: Always validate user input
2. **Output Encoding**: Properly encode output
3. **CSP Headers**: Use Content Security Policy
4. **HTTPS Only**: Always use HTTPS in production
5. **Regular Updates**: Keep dependencies updated

## Monitoring and Alerts

Set up monitoring for:
- ESLint security rule violations
- Failed security checks in CI/CD
- Runtime security errors
- Dependency vulnerabilities

## Resources

- [ESLint Security Plugin](https://github.com/nodesecurity/eslint-plugin-security)
- [React Security Best Practices](https://reactjs.org/docs/dom-elements.html#dangerouslysetinnerhtml)
- [OWASP XSS Prevention](https://owasp.org/www-community/xss-filter-evasion-cheatsheet)
- [Content Security Policy](https://developer.mozilla.org/en-US/docs/Web/HTTP/CSP)