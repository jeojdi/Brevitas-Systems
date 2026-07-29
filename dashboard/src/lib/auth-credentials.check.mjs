import assert from 'node:assert/strict'
import test from 'node:test'

import {
  normalizeAuthEmail,
  PASSWORD_MIN_LENGTH,
  PASSWORD_MISMATCH_MESSAGE,
  passwordsMatch,
  passwordStrengthProblem,
} from './auth-credentials.js'

test('address normalization folds the case and padding that autofill introduces', () => {
  assert.equal(normalizeAuthEmail('User@Example.COM'), 'user@example.com')
  assert.equal(normalizeAuthEmail('  user@example.com  '), 'user@example.com')
  assert.equal(normalizeAuthEmail('\t\n user@example.com \r\n'), 'user@example.com')
  assert.equal(normalizeAuthEmail(' User@Example.com '), 'user@example.com')
  assert.equal(normalizeAuthEmail('user@example.com'), 'user@example.com')
})

test('address normalization trims invisibles that a paste can smuggle in', () => {
  // Each of these looks exactly like a space in the input field.
  assert.equal(normalizeAuthEmail(' user@example.com '), 'user@example.com', 'NBSP')
  assert.equal(normalizeAuthEmail('　user@example.com　'), 'user@example.com', 'ideographic')
  assert.equal(normalizeAuthEmail(' user@example.com '), 'user@example.com', 'thin space')
  assert.equal(normalizeAuthEmail('﻿user@example.com'), 'user@example.com', 'BOM / ZWNBSP')

  // Known gap, asserted so it is a decision rather than a surprise: U+200B is not
  // whitespace, so trim() leaves it and the lookup still fails. Stripping arbitrary
  // invisibles would alter addresses the server treats as distinct.
  assert.equal(normalizeAuthEmail('​user@example.com'), '​user@example.com')
})

test('address normalization is locale-independent so ASCII I never becomes dotless', () => {
  // toLocaleLowerCase under a Turkish locale maps 'I' to 'ı' and corrupts the address.
  assert.equal(normalizeAuthEmail('IRENE@EXAMPLE.COM'), 'irene@example.com')
  assert.equal(normalizeAuthEmail('ISTANBUL@Example.COM'), 'istanbul@example.com')
  assert.doesNotMatch(normalizeAuthEmail('IRENE@EXAMPLE.COM'), /ı/)
})

test('address normalization preserves unicode local parts and internal structure', () => {
  assert.equal(normalizeAuthEmail('  JOSÉ@Example.com '), 'josé@example.com')
  assert.equal(normalizeAuthEmail('ÄÖÜ@Example.com'), 'äöü@example.com')
  assert.equal(normalizeAuthEmail('用户@example.com'), '用户@example.com')
  assert.equal(
    normalizeAuthEmail('  First.Last+Tag@Sub.Example.CO.UK  '),
    'first.last+tag@sub.example.co.uk',
  )
  // Internal whitespace is structural, not padding — normalization must not eat it.
  assert.equal(normalizeAuthEmail('  two words@example.com  '), 'two words@example.com')
})

test('address normalization yields an empty string for every non-string input', () => {
  assert.equal(normalizeAuthEmail(''), '')
  assert.equal(normalizeAuthEmail('     '), '')
  assert.equal(normalizeAuthEmail(null), '')
  assert.equal(normalizeAuthEmail(undefined), '')
  assert.equal(normalizeAuthEmail(), '')
  assert.equal(normalizeAuthEmail(0), '')
  assert.equal(normalizeAuthEmail(42), '')
  assert.equal(normalizeAuthEmail(NaN), '')
  assert.equal(normalizeAuthEmail(true), '')
  assert.equal(normalizeAuthEmail({}), '')
  assert.equal(normalizeAuthEmail([]), '')
  assert.equal(normalizeAuthEmail(['user@example.com']), '')
  assert.equal(normalizeAuthEmail(() => 'user@example.com'), '')
  assert.equal(normalizeAuthEmail(Symbol('user@example.com')), '')
  assert.equal(normalizeAuthEmail(10n), '')
  // Boxed strings are objects; callers must pass primitives.
  assert.equal(normalizeAuthEmail(new String('User@Example.com')), '')
})

test('address normalization is idempotent and handles very long input', () => {
  const long = `${'a'.repeat(10_000)}@example.com`
  assert.equal(normalizeAuthEmail(`  ${long.toUpperCase()}  `), long)
  const once = normalizeAuthEmail('  User@Example.COM  ')
  assert.equal(normalizeAuthEmail(once), once)
})

test('password comparison is exact and never trims a legitimate trailing space', () => {
  assert.equal(passwordsMatch('correct horse', 'correct horse'), true)
  assert.equal(passwordsMatch('trailing ', 'trailing '), true)
  assert.equal(passwordsMatch(' leading', ' leading'), true)

  // The whole point: these are different secrets and must not be treated as equal.
  assert.equal(passwordsMatch('trailing ', 'trailing'), false)
  assert.equal(passwordsMatch('trailing', 'trailing '), false)
  assert.equal(passwordsMatch(' pad ', 'pad'), false)
  assert.equal(passwordsMatch('tab\t', 'tab'), false)
  assert.equal(passwordsMatch('nl\n', 'nl'), false)
  assert.equal(passwordsMatch('nbsp ', 'nbsp'), false)
})

test('password comparison is case-sensitive and unicode-exact', () => {
  assert.equal(passwordsMatch('Passw0rd', 'passw0rd'), false)
  assert.equal(passwordsMatch('пароль123', 'пароль123'), true)
  assert.equal(passwordsMatch('\u{1F510}\u{1F510}\u{1F510}', '\u{1F510}\u{1F510}\u{1F510}'), true)
  assert.equal(passwordsMatch('\u{1F510}\u{1F510}\u{1F510}', '\u{1F510}\u{1F510}'), false)
  assert.equal(passwordsMatch('a b', 'a b'), true)

  // Canonically equivalent but differently encoded — precomposed U+00E9 versus 'e' plus
  // a combining acute. They render identically yet only one hashes to the stored
  // credential, so they are not the same password. Written as escapes so the
  // distinction cannot be erased by an editor or formatter normalizing this file.
  assert.equal(passwordsMatch('caf\u0065\u0301', 'caf\u00e9'), false)
  assert.equal(passwordsMatch('caf\u00e9', 'caf\u00e9'), true)
})

test('password comparison fails closed when either side is not a string', () => {
  assert.equal(passwordsMatch(null, null), false)
  assert.equal(passwordsMatch(undefined, undefined), false)
  assert.equal(passwordsMatch(), false)
  assert.equal(passwordsMatch('secret', undefined), false)
  assert.equal(passwordsMatch(undefined, 'secret'), false)
  assert.equal(passwordsMatch('secret', null), false)
  assert.equal(passwordsMatch(123456, 123456), false)
  assert.equal(passwordsMatch('123456', 123456), false)
  assert.equal(passwordsMatch(new String('secret'), 'secret'), false)
  assert.equal(passwordsMatch({}, {}), false)
  assert.equal(passwordsMatch(NaN, NaN), false)
})

test('empty strings match each other, so a strength check must also gate submission', () => {
  // Documents why a caller cannot rely on passwordsMatch alone.
  assert.equal(passwordsMatch('', ''), true)
  assert.notEqual(passwordStrengthProblem(''), '')
})

test('password strength accepts anything at or above the six-character floor', () => {
  assert.equal(PASSWORD_MIN_LENGTH, 6)
  assert.equal(passwordStrengthProblem('abcdef'), '')
  assert.equal(passwordStrengthProblem('123456'), '')
  assert.equal(passwordStrengthProblem('      '), '')
  assert.equal(passwordStrengthProblem('a'.repeat(100)), '')
  assert.equal(passwordStrengthProblem('a'.repeat(10_000)), '')

  // No composition rules: all-lowercase, digit-free, and symbol-free all pass.
  assert.equal(passwordStrengthProblem('password'), '')
  assert.equal(passwordStrengthProblem('пароль'), '')
})

test('password strength rejects only what is shorter than the floor', () => {
  const problem = passwordStrengthProblem('abcde')
  assert.equal(typeof problem, 'string')
  assert.match(problem, /at least 6 characters/)

  for (const short of ['', 'a', 'ab', 'abc', 'abcd', 'abcde']) {
    assert.notEqual(
      passwordStrengthProblem(short), '', `expected a problem for ${short.length} chars`,
    )
  }
  // Exact boundary: five fails, six passes.
  assert.notEqual(passwordStrengthProblem('12345'), '')
  assert.equal(passwordStrengthProblem('123456'), '')
})

test('password strength counts UTF-16 code units, matching the input minLength attribute', () => {
  // '\u{1F510}' is two code units, so three of them also satisfy minLength={6} in the
  // browser. Agreeing with the attribute matters more than counting graphemes.
  assert.equal('\u{1F510}\u{1F510}\u{1F510}'.length, 6)
  assert.equal(passwordStrengthProblem('\u{1F510}\u{1F510}\u{1F510}'), '')
  assert.notEqual(passwordStrengthProblem('\u{1F510}\u{1F510}'), '')
  assert.equal('caf\u00e912'.length, 6)
  assert.equal(passwordStrengthProblem('caf\u00e912'), '')
})

test('password strength fails closed when the value is not a string', () => {
  for (const value of [null, undefined, 123456, {}, [], true, NaN, new String('abcdef')]) {
    assert.notEqual(passwordStrengthProblem(value), '')
  }
  assert.notEqual(passwordStrengthProblem(), '')
})

test('the mismatch message is the exact copy the recovery flow already shows', () => {
  assert.equal(PASSWORD_MISMATCH_MESSAGE, 'Passwords do not match.')
})
