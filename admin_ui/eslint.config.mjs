import js from '@eslint/js';
import react from 'eslint-plugin-react';
import reactHooks from 'eslint-plugin-react-hooks';
import security from 'eslint-plugin-security';
import globals from 'globals';

const isProduction = process.env.NODE_ENV === 'production';

export default [
  {
    ignores: ['build/**', 'dist/**', 'coverage/**', 'node_modules/**'],
  },
  js.configs.recommended,
  security.configs.recommended,
  {
    files: ['**/*.{js,jsx}'],
    languageOptions: {
      ecmaVersion: 'latest',
      sourceType: 'module',
      parserOptions: {
        ecmaFeatures: { jsx: true },
      },
      globals: {
        ...globals.browser,
        ...globals.node,
        ...globals.es2021,
      },
    },
    plugins: {
      react,
      'react-hooks': reactHooks,
      security,
    },
    settings: {
      react: { version: 'detect' },
    },
    rules: {
      // ===== SECURITY RULES =====
      'no-eval': 'error',
      'no-implied-eval': 'error',
      'no-new-func': 'error',
      'no-script-url': 'error',
      'no-global-assign': 'error',
      'no-implicit-globals': 'error',
      'no-proto': 'error',
      strict: ['error', 'never'],

      'react/no-danger': 'error',
      'react/no-danger-with-children': 'error',
      'react/jsx-no-script-url': 'error',
      'react/jsx-no-target-blank': 'error',

      'security/detect-object-injection': 'error',
      'security/detect-non-literal-regexp': 'warn',
      'security/detect-non-literal-fs-filename': 'error',
      'security/detect-eval-with-expression': 'error',
      'security/detect-pseudoRandomBytes': 'warn',
      'security/detect-possible-timing-attacks': 'warn',
      'security/detect-unsafe-regex': 'error',
      'security/detect-buffer-noassert': 'error',
      'security/detect-child-process': 'error',
      'security/detect-disable-mustache-escape': 'error',
      'security/detect-new-buffer': 'error',

      // ===== ERROR PREVENTION =====
      'no-unused-vars': 'warn',
      'no-undef': 'error',
      'no-dupe-keys': 'error',
      'no-duplicate-case': 'error',
      'no-unreachable': 'error',
      'no-redeclare': 'error',
      'no-constant-condition': 'warn',
      'no-empty': 'warn',

      'react/jsx-key': 'error',
      'react/jsx-no-duplicate-props': 'error',
      'react/jsx-no-undef': 'error',
      'react/jsx-uses-react': 'off',
      'react/react-in-jsx-scope': 'off',
      'react/jsx-uses-vars': 'error',
      'react/no-direct-mutation-state': 'error',
      'react/no-is-mounted': 'error',
      'react/no-typos': 'error',
      'react/require-render-return': 'error',
      'react/no-unescaped-entities': 'error',
      'react/no-children-prop': 'error',
      'react/no-array-index-key': 'warn',
      'react/prop-types': 'off',

      'react-hooks/rules-of-hooks': 'error',
      'react-hooks/exhaustive-deps': 'warn',

      // ===== BEST PRACTICES =====
      eqeqeq: ['error', 'always', { null: 'ignore' }],
      'no-throw-literal': 'error',
      'no-implicit-coercion': 'warn',
      'no-use-before-define': ['error', { functions: false, classes: true, variables: true }],
      curly: ['error', 'multi-line'],
      'guard-for-in': 'error',
      'no-extend-native': 'error',
      'no-new-wrappers': 'error',
      radix: 'error',

      // ===== CODE QUALITY =====
      'no-var': 'warn',
      'prefer-const': 'warn',
      'prefer-template': 'warn',
      'no-duplicate-imports': 'error',
      'object-shorthand': 'warn',

      'no-console': isProduction ? 'error' : 'warn',
      'no-debugger': isProduction ? 'error' : 'warn',
      'no-alert': isProduction ? 'error' : 'warn',

      'no-useless-catch': 'off',
      'no-undef-init': 'off',
      'no-unused-expressions': 'off',

      'react/jsx-pascal-case': 'off',
      'react/self-closing-comp': 'off',
      'react/jsx-wrap-multilines': 'off',
    },
  },
  {
    files: [
      '**/__tests__/**/*.{js,jsx}',
      '**/*.test.{js,jsx}',
      '**/*.spec.{js,jsx}',
      'src/setupTests.js',
    ],
    languageOptions: {
      globals: {
        ...globals.jest,
        vi: 'readonly',
        vitest: 'readonly',
      },
    },
    rules: {
      'no-console': 'off',
      'no-unused-vars': 'off',
      // Tests that verify a module's SHAPE have to read properties by computed
      // name — `real[name]` where `name` came from `Object.getOwnPropertyNames`,
      // or `actual.default[name]` over a declared list of required methods.
      // This rule flags every bracket access as a "Generic Object Injection
      // Sink" without tracking where the key came from, so on that code it is a
      // pure false positive: the key is derived from the module under test, not
      // from user input, and a test file has no untrusted input to begin with.
      //
      // Turned off here rather than as inline disables, because the alternative
      // is a new `eslint-disable-next-line` every time someone writes a
      // shape-verifying test — which is exactly the kind of test that caught a
      // deleted service method still answering calls.
      'security/detect-object-injection': 'off',
    },
  },
  {
    files: ['scripts/**/*.{js,cjs,mjs}', '*.config.{js,cjs,mjs}'],
    languageOptions: {
      globals: { ...globals.node },
    },
    rules: {
      'no-console': 'off',
    },
  },
];
