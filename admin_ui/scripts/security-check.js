#!/usr/bin/env node

/**
 * Security-focused ESLint check script for Admin UI
 * Runs specific security rules and fails if critical issues are found
 */

const { execSync } = require('child_process');
const path = require('path');

// Critical security rules that must pass
const CRITICAL_SECURITY_RULES = [
  'no-eval',
  'no-implied-eval',
  'no-new-func',
  'no-script-url',
  'react/no-danger',
  'react/jsx-no-script-url',
  'security/detect-object-injection',
  'security/detect-eval-with-expression',
  'security/detect-unsafe-regex'
];

// Colors for output
const colors = {
  red: '\x1b[31m',
  green: '\x1b[32m',
  yellow: '\x1b[33m',
  blue: '\x1b[34m',
  reset: '\x1b[0m',
  bold: '\x1b[1m'
};

function log(message, color = 'reset') {
  console.log(colors[color] + message + colors.reset);
}

function runSecurityCheck() {
  log('🔒 Running ESLint Security Check...', 'blue');
  
  try {
    // Run ESLint with JSON output for parsing
    const command = 'npx eslint src --ext .js,.jsx --format json';
    const result = execSync(command, { 
      encoding: 'utf-8', 
      cwd: path.dirname(__dirname),
      stdio: 'pipe'
    });
    
    const lintResults = JSON.parse(result);
    let criticalIssues = 0;
    let warningIssues = 0;
    let securityIssues = 0;
    
    // Analyze results
    lintResults.forEach(file => {
      file.messages.forEach(message => {
        const ruleId = message.ruleId;
        
        if (message.severity === 2) { // Error
          criticalIssues++;
          
          // Check if it's a critical security rule
          if (CRITICAL_SECURITY_RULES.includes(ruleId) || ruleId?.startsWith('security/')) {
            securityIssues++;
            log(`❌ SECURITY ERROR in ${file.filePath}:${message.line}`, 'red');
            log(`   Rule: ${ruleId}`, 'red');
            log(`   Message: ${message.message}`, 'red');
            log('');
          }
        } else if (message.severity === 1) { // Warning
          warningIssues++;
          
          if (ruleId?.startsWith('security/')) {
            log(`⚠️  SECURITY WARNING in ${file.filePath}:${message.line}`, 'yellow');
            log(`   Rule: ${ruleId}`, 'yellow');
            log(`   Message: ${message.message}`, 'yellow');
            log('');
          }
        }
      });
    });
    
    // Summary
    log('📊 Security Check Summary:', 'bold');
    log(`   Critical Issues: ${criticalIssues}`, criticalIssues > 0 ? 'red' : 'green');
    log(`   Security Issues: ${securityIssues}`, securityIssues > 0 ? 'red' : 'green');
    log(`   Warnings: ${warningIssues}`, warningIssues > 0 ? 'yellow' : 'green');
    
    // Exit with error if critical security issues found
    if (securityIssues > 0) {
      log('🚨 CRITICAL SECURITY ISSUES DETECTED!', 'red');
      log('Please fix security issues before proceeding.', 'red');
      process.exit(1);
    }
    
    if (criticalIssues > 0) {
      log('⚠️  Critical linting issues found. Please review.', 'yellow');
      log('Consider running: npm run lint:fix', 'blue');
      process.exit(1);
    }
    
    log('✅ Security check passed!', 'green');
    
  } catch (error) {
    if (error.status === 1) {
      // ESLint found issues, handle the output
      try {
        const lintResults = JSON.parse(error.stdout);
        log('🔍 Processing ESLint results...', 'blue');
        
        let securityIssues = 0;
        lintResults.forEach(file => {
          file.messages.forEach(message => {
            if (message.ruleId?.startsWith('security/') || 
                CRITICAL_SECURITY_RULES.includes(message.ruleId)) {
              securityIssues++;
              const severity = message.severity === 2 ? 'ERROR' : 'WARNING';
              log(`🚨 SECURITY ${severity} in ${file.filePath}:${message.line}`, 'red');
              log(`   Rule: ${message.ruleId}`, 'red');
              log(`   Message: ${message.message}`, 'red');
              log('');
            }
          });
        });
        
        if (securityIssues > 0) {
          log(`🚨 Found ${securityIssues} security issues!`, 'red');
          process.exit(1);
        }
        
      } catch (parseError) {
        log('❌ Failed to parse ESLint output', 'red');
        console.error(error.stdout);
        process.exit(1);
      }
    } else {
      log('❌ ESLint execution failed', 'red');
      console.error(error.message);
      process.exit(1);
    }
  }
}

function main() {
  const args = process.argv.slice(2);
  
  if (args.includes('--help') || args.includes('-h')) {
    log('🔒 ESLint Security Checker', 'bold');
    log('');
    log('Usage: node security-check.js [options]', 'blue');
    log('');
    log('Options:', 'blue');
    log('  --help, -h     Show this help message');
    log('  --fix          Run with auto-fix enabled');
    log('  --rules        Show critical security rules');
    log('');
    log('Exit codes:', 'blue');
    log('  0 - No security issues found');
    log('  1 - Security issues found');
    return;
  }
  
  if (args.includes('--rules')) {
    log('🔒 Critical Security Rules:', 'bold');
    CRITICAL_SECURITY_RULES.forEach(rule => {
      log(`  • ${rule}`, 'blue');
    });
    return;
  }
  
  if (args.includes('--fix')) {
    log('🔧 Running ESLint with auto-fix...', 'blue');
    try {
      execSync('npm run lint:fix', { 
        stdio: 'inherit', 
        cwd: path.dirname(__dirname) 
      });
      log('✅ Auto-fix completed!', 'green');
    } catch (error) {
      log('❌ Auto-fix failed', 'red');
      process.exit(1);
    }
  }
  
  runSecurityCheck();
}

if (require.main === module) {
  main();
}

module.exports = { runSecurityCheck };